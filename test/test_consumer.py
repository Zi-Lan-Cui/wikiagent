"""WatchConsumer 测试——删除处理规则 + Job handler 核账凭证。

直接运行:  .venv/bin/python test/test_consumer.py
"""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.jobs import Job
from wiki_agent.watch.consumer import WatchConsumer, clean_body_links
from wiki_agent.watch.state import WatchState, digest_file_text


def _job(resource: str, *, kind: str = "compile", digest: str = "") -> Job:
    return Job(
        id="job_test",
        kind=kind,
        resource=resource,
        mode="watch",
        status="running",
        stage="",
        attempts=1,
        error="",
        payload={"deleted": kind == "delete", "digest": digest},
        created_at="",
        updated_at="",
    )


class _FakePipeline:
    def __init__(self, outcome=None, raises=None):
        self.calls = 0
        self._outcome = outcome or SimpleNamespace(noop=False, pages_written=["p"])
        self._raises = raises

    async def ingest_one(self, raw_file):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._outcome


def _compile_env(tmp: Path, content: str = "编译器输入" * 8):
    src = tmp / "src"
    src.mkdir(parents=True, exist_ok=True)
    f = src / "note.md"
    f.write_text(content, encoding="utf-8")
    wiki, records = _make_wiki(tmp)
    state = WatchState(tmp / "state.json")
    return f, wiki, records, state


def test_compile_success_returns_ack_evidence():
    """成功返回经 pre/post 双检的 digest+text 凭证（落账由 outcome 完成）。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state = _compile_env(tmp)
        digest, text = digest_file_text(f)
        pipeline = _FakePipeline()
        consumer = WatchConsumer(pipeline, state, wiki_dir=wiki, source_records_dir=records)
        result = await consumer.handle_job(_job(str(f), digest=digest), lambda s: None)
        assert result.status == "succeeded"
        assert result.detail == {"digest": digest, "text": text}
        assert pipeline.calls == 1
        assert state.get(str(f)).hash == "", "consumer 自己不写账"

    asyncio.run(run())


def test_compile_idempotent_short_circuit():
    """该内容已有完成账 → 短路，不碰 pipeline。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state = _compile_env(tmp)
        digest, text = digest_file_text(f)
        state.record(str(f), digest, text)
        pipeline = _FakePipeline()
        consumer = WatchConsumer(pipeline, state, wiki_dir=wiki, source_records_dir=records)
        result = await consumer.handle_job(_job(str(f), digest=digest), lambda s: None)
        assert result.status == "succeeded" and result.detail == {}
        assert pipeline.calls == 0

    asyncio.run(run())


def test_compile_stale_disk_does_not_ack():
    """job 请求的 digest 与实际读到的不符（磁盘已前进）→ 成功但不给凭证。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state = _compile_env(tmp)
        pipeline = _FakePipeline()
        consumer = WatchConsumer(pipeline, state, wiki_dir=wiki, source_records_dir=records)
        result = await consumer.handle_job(_job(str(f), digest="0" * 64), lambda s: None)
        assert result.status == "succeeded" and "digest" not in result.detail

    asyncio.run(run())


def test_compile_ingest_error_becomes_result_detail():
    """业务失败 → failed/ingest_error，detail 携带 issue draft 所需字段。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state = _compile_env(tmp)
        digest, _ = digest_file_text(f)
        err = IngestError(IngestStage.PLAN, "plan 输出校验失败", source=f.name)
        pipeline = _FakePipeline(raises=err)
        consumer = WatchConsumer(pipeline, state, wiki_dir=wiki, source_records_dir=records)
        result = await consumer.handle_job(_job(str(f), digest=digest), lambda s: None)
        assert result.status == "failed" and result.error_type == "ingest_error"
        assert result.detail["stage"] == "plan"
        assert result.detail["source_path"] == str(f)
        assert result.detail["retry_policy"] == err.retry_policy
        assert "digest" not in result.detail

    asyncio.run(run())


def test_compile_missing_file_is_load_error():
    """文件消失 → ingest_error/stage=load，不炸 worker。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state = _compile_env(tmp)
        f.unlink()
        consumer = WatchConsumer(_FakePipeline(), state, wiki_dir=wiki, source_records_dir=records)
        result = await consumer.handle_job(_job(str(f), digest="a" * 64), lambda s: None)
        assert result.status == "failed" and result.error_type == "ingest_error"
        assert result.detail["stage"] == "load"

    asyncio.run(run())


def test_delete_resurrection_skips_cleanup():
    """源文件复活 → 跳过清理（成功账仍会删条目，复活内容按新文件接入）。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state = _compile_env(tmp)
        consumer = WatchConsumer(None, state, wiki_dir=wiki, source_records_dir=records)
        result = await consumer.handle_job(_job(str(f), kind="delete"), lambda s: None)
        assert result.status == "succeeded"
        assert (records / "note.md").exists(), "复活文件不得触发溯源清理"

    asyncio.run(run())


def test_delete_job_cleans_provenance():
    """文件确实消失 → 清理规则执行（other.md 从 sources 移除）。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        f, wiki, records, state = _compile_env(tmp)
        ghost = tmp / "src" / "other.md"
        ghost.unlink(missing_ok=True)
        consumer = WatchConsumer(None, state, wiki_dir=wiki, source_records_dir=records)
        result = await consumer.handle_job(_job(str(ghost), kind="delete"), lambda s: None)
        assert result.status == "succeeded"
        assert "other.md" not in (records / "note.md").read_text(encoding="utf-8")

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


def test_process_delete_removes_only_entry():
    """sources 页只剩被删文件 → 页删除。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        wiki, records = _make_wiki(tmp)
        state = WatchState(tmp / "state.json")
        consumer = WatchConsumer(None, state, wiki_dir=wiki, source_records_dir=records)
        # 手工触发删除处理（pipeline 为 None——删除路径不碰它）
        consumer._process_delete("other.md")
        content = (records / "note.md").read_text(encoding="utf-8")
        # other 从 sources 列表移除
        assert "other.md" not in content
        assert "note.md" in content

    asyncio.run(run())


def test_process_delete_keeps_page_with_multiple_sources():
    """sources 页还剩其他文件 → 保留页仅移除条目。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        wiki, records = _make_wiki(tmp)
        state = WatchState(tmp / "state.json")
        consumer = WatchConsumer(None, state, wiki_dir=wiki, source_records_dir=records)
        consumer._process_delete("note.md")  # 删了 note 后 sources 只剩 other——页面删
        # note.md 被删后 sources 列表只剩 other.md——页还在
        assert (records / "note.md").exists()
        content = (records / "note.md").read_text(encoding="utf-8")
        assert "note.md" not in content  # 条目移除

    asyncio.run(run())


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
