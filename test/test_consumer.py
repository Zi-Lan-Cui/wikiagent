"""SyncConsumer 测试——读快照输入的执行规则 + 删除处理 + 快照故障分类。

核心语义（快照是输入）：compile 任务只读提交时定格的副本；执行期间原件
修改/删除都照常完成本批；快照件被篡改或任务缺坐标 = 存储故障，不是业务失败。

直接运行:  .venv/bin/python test/test_consumer.py
"""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.jobs import Job
from wiki_agent.snapshots import SnapshotStore
from wiki_agent.sync.job_consumer import SyncConsumer, clean_body_links
from wiki_agent.sync.state import SyncState, digest_file_text

BATCH = "b_test"


def _job(resource: str, payload: dict[str, object], *, kind: str = "compile") -> Job:
    return Job(
        id="job_test",
        kind=kind,
        resource=resource,
        mode="sync",
        status="running",
        stage="",
        attempts=1,
        error="",
        payload=payload,
        created_at="",
        updated_at="",
    )


def _delete_payload() -> dict[str, object]:
    return {"deleted": True, "digest": "", "batch": BATCH}


class _FakePipeline:
    def __init__(self, raises=None):
        self.calls = 0
        self.seen_raw = []
        self._raises = raises

    async def ingest_one(self, raw_file):
        self.calls += 1
        self.seen_raw.append(raw_file)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(noop=False, pages_written=[], extract=None)


def _compile_env(tmp: Path, content: str = "编译器输入" * 8):
    src = tmp / "src"
    src.mkdir(parents=True, exist_ok=True)
    f = src / "note.md"
    f.write_text(content, encoding="utf-8")
    wiki, records = _make_wiki(tmp)
    state = SyncState(tmp / "state.json")
    snapshots = SnapshotStore(tmp)
    captured = snapshots.capture(BATCH, src, [f])
    payload = {
        "deleted": False,
        "digest": captured[str(f.resolve())],
        "batch": BATCH,
        "rel_path": f.name,
    }
    return f, wiki, records, state, snapshots, payload


def _consumer(state, wiki, records, pipeline, snapshots):
    return SyncConsumer(
        pipeline,
        state,
        wiki_dir=wiki,
        source_records_dir=records,
        snapshots=snapshots,
    )


def test_compile_success_reads_snapshot_and_keeps_original_identity():
    """成功凭证来自快照件；流水线看到的业务路径是原始路径。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, payload = _compile_env(tmp)
        digest, text = digest_file_text(snapshots.staged_path(BATCH, f.name))
        pipeline = _FakePipeline()
        consumer = _consumer(state, wiki, records, pipeline, snapshots)
        result = await consumer.handle_job(_job(str(f.resolve()), payload), lambda s: None)
        assert result.status == "succeeded"
        assert result.detail == {"digest": digest, "text": text}
        assert pipeline.calls == 1
        assert pipeline.seen_raw[0].path == f.resolve(), "业务身份必须是原路径"
        assert state.get(str(f.resolve())).hash == "", "consumer 自己不写账"

    asyncio.run(run())


def test_compile_idempotent_short_circuit():
    """该内容已有完成账 → 短路，不碰 pipeline（崩溃重放的保险）。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, payload = _compile_env(tmp)
        digest, text = digest_file_text(f)
        state.record(str(f.resolve()), digest, text)
        pipeline = _FakePipeline()
        consumer = _consumer(state, wiki, records, pipeline, snapshots)
        result = await consumer.handle_job(_job(str(f.resolve()), payload), lambda s: None)
        assert result.status == "succeeded" and result.detail == {}
        assert pipeline.calls == 0

    asyncio.run(run())


def test_source_modified_after_submit_still_processes_snapshot():
    """提交后原件被改：本批仍处理定格内容，改动留给下一次点击。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, payload = _compile_env(tmp)
        f.write_text("提交之后才写入的新内容" * 8, encoding="utf-8")
        pipeline = _FakePipeline()
        consumer = _consumer(state, wiki, records, pipeline, snapshots)
        result = await consumer.handle_job(_job(str(f.resolve()), payload), lambda s: None)
        assert result.status == "succeeded"
        assert result.detail["digest"] == payload["digest"], "记的是快照件的内容"
        assert "新内容" not in result.detail["text"]

    asyncio.run(run())


def test_source_deleted_after_submit_still_processes_snapshot():
    """提交后原件被删：compile 照常成功（输入在快照里）；旧账清理归删除分支。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, payload = _compile_env(tmp)
        f.unlink()
        pipeline = _FakePipeline()
        consumer = _consumer(state, wiki, records, pipeline, snapshots)
        result = await consumer.handle_job(_job(str(f.resolve()), payload), lambda s: None)
        assert result.status == "succeeded"
        assert result.detail["digest"] == payload["digest"]
        assert pipeline.calls == 1

    asyncio.run(run())


def test_snapshot_tampered_fails_without_business_accounting():
    """快照件与提交记录不符 = 存储故障：failed、无凭证、pipeline 不启动。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, payload = _compile_env(tmp)
        snapshots.staged_path(BATCH, f.name).write_text("被外部篡改", encoding="utf-8")
        pipeline = _FakePipeline()
        consumer = _consumer(state, wiki, records, pipeline, snapshots)
        result = await consumer.handle_job(_job(str(f.resolve()), payload), lambda s: None)
        assert result.status == "failed" and result.error_type == ""
        assert "snapshot_error" in result.detail["error"]
        assert pipeline.calls == 0
        assert state.get(str(f.resolve())).hash == "", "完成账不动"

    asyncio.run(run())


def test_job_without_snapshot_coordinates_is_snapshot_error():
    """缺 batch/rel_path 的任务不可能出自 submit——按存储故障处理。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, _payload = _compile_env(tmp)
        pipeline = _FakePipeline()
        consumer = _consumer(state, wiki, records, pipeline, snapshots)
        result = await consumer.handle_job(
            _job(str(f.resolve()), {"deleted": False, "digest": "a" * 64}), lambda s: None
        )
        assert result.status == "failed" and "snapshot_error" in result.detail["error"]
        assert pipeline.calls == 0

    asyncio.run(run())


def test_compile_ingest_error_becomes_result_detail():
    """业务失败 → failed/ingest_error，detail 携带 issue draft 所需字段。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, payload = _compile_env(tmp)
        err = IngestError(IngestStage.PLAN, "plan 输出校验失败", source=f.name)
        pipeline = _FakePipeline(raises=err)
        consumer = _consumer(state, wiki, records, pipeline, snapshots)
        result = await consumer.handle_job(_job(str(f.resolve()), payload), lambda s: None)
        assert result.status == "failed" and result.error_type == "ingest_error"
        assert result.detail["stage"] == "plan"
        assert result.detail["source_path"] == str(f.resolve())
        assert result.detail["retry_policy"] == err.retry_policy
        assert "digest" not in result.detail

    asyncio.run(run())


def test_delete_applies_even_if_source_revived():
    """删除决定来自快照差集：执行时原件复活也照常规划清理，复活内容下次点击重排。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, _payload = _compile_env(tmp)
        # f（note.md）仍在磁盘上——快照却判定它该删（removed 差集成立）
        consumer = _consumer(state, wiki, records, None, snapshots)
        result = await consumer.handle_job(
            _job(str(f.resolve()), _delete_payload(), kind="delete"), lambda s: None
        )
        assert result.status == "succeeded"
        ops = result.detail["archive_ops"]
        assert ops and ops[0]["action"] == "rewrite", "按快照清旧账，不看复活"

    asyncio.run(run())


def test_delete_job_cleans_provenance():
    """正常删除 → 只规划清理清单：档案改写与账本同点在成功结算时落盘。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state, snapshots, _payload = _compile_env(tmp)
        ghost = tmp / "src" / "other.md"
        ghost.unlink(missing_ok=True)
        consumer = _consumer(state, wiki, records, None, snapshots)
        result = await consumer.handle_job(
            _job(str(ghost.resolve()), _delete_payload(), kind="delete"), lambda s: None
        )
        assert result.status == "succeeded"
        ops = result.detail["archive_ops"]
        assert ops == [
            {
                "action": "rewrite",
                "path": str(records / "note.md"),
                "content": ops[0]["content"],
            }
        ]
        assert "other.md" not in ops[0]["content"]
        assert "other.md" in (records / "note.md").read_text(encoding="utf-8"), "执行中不动档案"

    asyncio.run(run())


def _make_wiki(tmp: Path) -> tuple[Path, Path]:
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    records = tmp / "workspace" / "provenance" / "sources"
    records.mkdir(parents=True)
    # 工作区溯源记录引用两个源文件
    (records / "note.md").write_text(
        '---\ntype: source\ntitle: "Note"\nsummary: "s"\ngoal: "g"\n'
        'related: []\nsources: ["note.md", "other.md"]\n'
        "---\n# Note\n\n摘要。\n",
        encoding="utf-8",
    )
    # 正文引用 sources 页别名
    (wiki / "concepts" / "page.md").write_text(
        '---\ntype: concept\ntitle: "Page"\nsummary: "s"\ngoal: "g"\n'
        "related: []\n---\n# Page\n\n正文提到 [[sources/note|Note]] 档案。\n",
        encoding="utf-8",
    )
    return wiki, records


def test_clean_body_links_replaces_aliases():
    """正文中 sources 页引用 → 别名纯文本。"""
    tmp = Path(tempfile.mkdtemp())
    wiki, _ = _make_wiki(tmp)
    changed = clean_body_links(wiki, "note")
    assert changed == 1
    content = (wiki / "concepts" / "page.md").read_text(encoding="utf-8")
    assert "[[sources/note" not in content
    assert "Note" in content  # 别名保留


def test_plan_archive_cleanup_keeps_page_with_multiple_sources():
    """sources 页还剩其他文件 → 规划 rewrite：仅移除条目，页面文件不动。"""

    tmp = Path(tempfile.mkdtemp())
    wiki, records = _make_wiki(tmp)
    state = SyncState(tmp / "state.json")
    consumer = _consumer(state, wiki, records, None, SnapshotStore(tmp))
    # 删了 note 后 sources 只剩 other——页还在
    ops = consumer._plan_archive_cleanup("note.md")
    assert ops == [
        {"action": "rewrite", "path": str(records / "note.md"), "content": ops[0]["content"]}
    ]
    assert "note.md" not in ops[0]["content"]
    assert "other.md" in ops[0]["content"]
    assert "note.md" in (records / "note.md").read_text(encoding="utf-8"), "规划阶段不落盘"


def test_plan_archive_cleanup_unlinks_page_and_cleans_links_now():
    """sources 只剩被删文件 → unlink 进清单；正文引用清理属 wiki scope，立即执行。"""

    tmp = Path(tempfile.mkdtemp())
    wiki, records = _make_wiki(tmp)
    (records / "solo.md").write_text(
        '---\ntype: source\ntitle: "Solo"\nsummary: "s"\ngoal: "g"\n'
        'related: []\nsources: ["solo.pdf"]\n'
        "---\n# Solo\n\n摘要。\n",
        encoding="utf-8",
    )
    (wiki / "concepts" / "ref.md").write_text(
        "---\n\n# Ref\n\n见 [[sources/solo|Solo 档案]]。\n", encoding="utf-8"
    )
    state = SyncState(tmp / "state.json")
    consumer = _consumer(state, wiki, records, None, SnapshotStore(tmp))

    ops = consumer._plan_archive_cleanup("solo.pdf")

    assert ops == [{"action": "unlink", "path": str(records / "solo.md")}]
    assert (records / "solo.md").exists()  # unlink 等结算
    assert "[[sources/solo" not in (wiki / "concepts" / "ref.md").read_text(encoding="utf-8")
    assert "Solo 档案" in (wiki / "concepts" / "ref.md").read_text(encoding="utf-8")


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
