"""git 融入 sync 的全链路——per-job 协议（pre-reset/成功 commit/失败 restore+残骸）、
结算面（档案页与账本同点落盘）、批尾注与撤销批。

装配 = 真实 JobService + SyncConsumer(带 WikiGitManager) + JobWorker 泵，
pipeline 用假件（写页面/制造失败），LLM 不出场。

直接运行:  .venv/bin/python test/test_sync_git.py
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.issues import IssueKind
from wiki_agent.jobs.service import JobService
from wiki_agent.jobs.worker import JobWorker
from wiki_agent.sync.job_consumer import SyncConsumer
from wiki_agent.sync.state import SyncState
from wiki_agent.versioning import WikiGitManager


class _FakePipeline:
    """假 ingest：成功写一个概念页并构造档案页；失败先留半页残骸再抛；
    bad 名单写出缺 frontmatter 的坏页（触发消费端单 source 质量闸门）。"""

    def __init__(self, wiki: Path, fail_names: tuple[str, ...] = (), bad_names: tuple[str, ...] = ()):
        self._wiki = wiki
        self._fail = fail_names
        self._bad = bad_names
        self.calls: list[str] = []

    async def ingest_one(self, raw_file):
        self.calls.append(raw_file.name)
        stem = Path(raw_file.name).stem
        rel = f"concepts/{stem}.md"
        (self._wiki / "concepts").mkdir(parents=True, exist_ok=True)
        if raw_file.name in self._fail:
            (self._wiki / rel).write_text("半成品残骸\n", encoding="utf-8")
            raise IngestError(IngestStage.EXECUTE, "mock 失败", source=raw_file.name)
        if raw_file.name in self._bad:
            (self._wiki / rel).write_text("没有 frontmatter 的坏页\n", encoding="utf-8")
        else:
            (self._wiki / rel).write_text(
                '---\ntype: concept\ntitle: "页"\nsummary: "s"\ngoal: "g"\n'
                f"related: []\n---\n# {stem}\n\n正文内容，长度足够通过检查。\n",
                encoding="utf-8",
            )
        page = SimpleNamespace(
            slug=stem,
            content='---\ntype: source\ntitle: "档案"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
            f'sources: ["{raw_file.name}"]\n---\n# {stem}\n\n档案摘要内容足够长一些。\n',
        )
        return SimpleNamespace(
            noop=False,
            pages_written=[rel],
            extract=SimpleNamespace(source_page=page),
        )


def _env(tmp: Path, *, fail_names: tuple[str, ...] = (), bad_names: tuple[str, ...] = ()):
    src = tmp / "materials"
    src.mkdir(parents=True, exist_ok=True)
    wiki = tmp / "wiki"
    wiki.mkdir(exist_ok=True)
    records = tmp / "provenance"
    records.mkdir(exist_ok=True)
    state = SyncState(tmp / "watch" / "state.json")
    git = WikiGitManager(wiki)  # wiki 机器管理：初始化自带空仓库
    service = JobService(
        tmp,
        wiki_dir=wiki,
        sync_state=state,
        source_records_dir=records,
    )
    pipeline = _FakePipeline(wiki, fail_names, bad_names)
    consumer = SyncConsumer(
        pipeline,
        state,
        wiki_dir=wiki,
        source_records_dir=records,
        git=git,
    )
    worker = JobWorker(service)
    worker.register("compile", consumer.handle_job)
    worker.register("delete", consumer.handle_job)
    return src, wiki, records, git, state, service, worker


def _pump(worker: JobWorker, rounds: int) -> None:
    for _ in range(rounds):
        asyncio.run(worker.run_once())


def test_success_commits_per_job_and_settles_archive(tmp_path: Path):
    src, wiki, records, git, state, service, worker = _env(tmp_path)
    (src / "a.md").write_text("甲文件内容" * 10, encoding="utf-8")
    (src / "b.md").write_text("乙文件内容" * 10, encoding="utf-8")
    jobs = service.submit_sync(src)
    batch = str(jobs[0].payload["batch"])
    _pump(worker, 2)

    # 逐 job 提交：两个文件 = 两个 commit，尾注同源；HEAD 干净
    assert len(git.batch_commits(batch)) == 2
    joined = " ".join(git.history())
    assert "sync: a.md" in joined and "sync: b.md" in joined
    assert git.is_clean()
    assert (wiki / "concepts" / "a.md").exists() and (wiki / "concepts" / "b.md").exists()
    # 结算面：档案页与账本同点落盘
    assert (records / "a.md").read_text(encoding="utf-8").startswith("---")
    assert state.get(str((src / "a.md").resolve())).hash
    assert service.submit_sync(src) == [], "成功即干净"


def test_failure_restores_wiki_keeps_ledger_dirty_and_saves_debris(tmp_path: Path):
    src, wiki, records, git, state, service, worker = _env(tmp_path, fail_names=("a.md",))
    baseline = git.head()
    (src / "a.md").write_text("会失败的内容" * 10, encoding="utf-8")
    jobs = service.submit_sync(src)
    _pump(worker, 1)

    assert service.store.get(jobs[0].id).status == "failed"
    # 残骸撤干净：不留下半成品，HEAD 不动（失败不进历史）
    assert git.is_clean()
    assert not (wiki / "concepts" / "a.md").exists()
    assert git.head() == baseline
    # 失败证据：残骸 patch 落盘、账本保持脏（再 sync 即重试）、issue 有账
    debris = records.parent / "debris" / f"{jobs[0].id}.patch"
    assert debris.is_file() and "半成品残骸" in debris.read_text(encoding="utf-8")
    assert state.get(str((src / "a.md").resolve())).hash == ""
    assert service.issues.list(kinds={IssueKind.INGESTION_FAILURE}), "业务失败进问题账本"
    # 档案页缺席：失败不结算
    assert list(records.glob("*.md")) == []
    assert service.submit_sync(src) != [], "失败即脏：再 sync 重新入队"


def test_pre_reset_clears_previous_job_debris(tmp_path: Path):
    src, wiki, records, git, state, service, worker = _env(tmp_path)
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "ghost.md").write_text("上一进程留下的残骸\n", encoding="utf-8")
    (src / "ok.md").write_text("正常内容" * 10, encoding="utf-8")
    service.submit_sync(src)
    _pump(worker, 1)

    assert not (wiki / "concepts" / "ghost.md").exists(), "pre-reset 收编崩溃残骸"
    assert (wiki / "concepts" / "ok.md").exists()
    assert git.is_clean()


def test_delete_settles_archive_unlink_and_commits_wiki(tmp_path: Path):
    src, wiki, records, git, state, service, worker = _env(tmp_path)
    f = src / "gone.md"
    f.write_text("将被删除" * 10, encoding="utf-8")
    service.sync_state.record(str(f.resolve()), "deadbeef", "将被删除" * 10)
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "p.md").write_text(
        "# P\n\n见 [[sources/gone|已删档案]]。\n", encoding="utf-8"
    )
    archive = records / "gone.md"
    archive.write_text(
        '---\ntype: source\ntitle: "Gone"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
        'sources: ["gone.md"]\n---\n# Gone\n',
        encoding="utf-8",
    )
    f.unlink()
    # 预置 wiki 内容必须已结算入 HEAD——否则 job 的 pre-reset 会当残骸清掉
    git.commit_all("wiki: seed")
    assert git.is_clean()

    jobs = service.submit_sync(src)
    assert [j.kind for j in jobs] == ["delete"]
    _pump(worker, 1)

    # wiki 引用清理随 delete commit 入历史；档案 unlink 与账本 drop 在结算生效
    assert git.is_clean()
    assert "已删档案" in (wiki / "concepts" / "p.md").read_text(encoding="utf-8")
    assert "[[sources/gone" not in (wiki / "concepts" / "p.md").read_text(encoding="utf-8")
    assert not archive.exists()
    assert service.sync_state.get(str(f.resolve())) is None or (
        service.sync_state.get(str(f.resolve())).hash == ""
    )
    assert service.submit_sync(src) == []


def test_quality_gate_failure_restores_and_records(tmp_path: Path):
    """单 source 局部质量闸门（批壳迁入）：坏产出 = 业务失败，
    残骸 restore、不进 commit/档案，保持脏并记账等人。"""
    src, wiki, records, git, state, service, worker = _env(tmp_path, bad_names=("a.md",))
    baseline = git.head()
    (src / "a.md").write_text("坏页输入" * 10, encoding="utf-8")
    jobs = service.submit_sync(src)
    _pump(worker, 1)

    row = service.store.get(jobs[0].id)
    assert row.status == "failed" and "质量" in row.error
    assert git.is_clean() and git.head() == baseline, "坏页不得入历史"
    assert not (wiki / "concepts" / "a.md").exists()
    assert list(records.glob("*.md")) == []
    assert state.get(str((src / "a.md").resolve())).hash == ""
    assert service.issues.list(kinds={IssueKind.INGESTION_FAILURE})


def test_revert_batch_undoes_a_sync_batch(tmp_path: Path):
    src, wiki, records, git, state, service, worker = _env(tmp_path)
    (src / "a.md").write_text("甲" * 20, encoding="utf-8")
    (src / "b.md").write_text("乙" * 20, encoding="utf-8")
    batch = service.submit_sync(src)[0].payload["batch"]
    _pump(worker, 2)
    assert (wiki / "concepts" / "a.md").exists()

    git.revert_batch(str(batch))

    assert not (wiki / "concepts" / "a.md").exists()
    assert not (wiki / "concepts" / "b.md").exists()
    assert git.is_clean()
    # 纯历史操作：账本不回退（撤销后仍"已同步"是既定语义）
    assert service.submit_sync(src) == []


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    for name, fn in tests.items():
        try:
            fn(Path(tempfile.mkdtemp()))
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
