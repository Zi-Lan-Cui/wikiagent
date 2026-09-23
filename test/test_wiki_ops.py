"""refine/restructure 执行体——per-job git 协议 + 失败不记账。

真 git、真 JobService/Worker；手术 execute 与 refine pipeline 用假件。
断言重点：一页一提交、批尾注可撤、失败只撤残骸不进历史也不进问题账本。

直接运行:  .venv/bin/python test/test_wiki_ops.py
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from wiki_agent.application.wiki_ops import WikiOpsConsumer
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.jobs.service import JobService
from wiki_agent.jobs.worker import JobWorker
from wiki_agent.versioning import WikiGitManager


class _RefinePipeline:
    def __init__(self, wiki: Path, raises: IngestError | None = None):
        self._wiki = wiki
        self._raises = raises
        self.calls: list[str] = []

    async def ingest_one(self, raw_file):
        self.calls.append(raw_file.name)
        if self._raises is not None:
            (self._wiki / "concepts" / "x.md").write_text("半成品\n", encoding="utf-8")
            raise self._raises
        (self._wiki / "concepts" / "x.md").write_text(
            '---\ntype: concept\ntitle: "X"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
            "---\n# x\n\n精炼后的正文内容。\n",
            encoding="utf-8",
        )
        return SimpleNamespace(noop=False, pages_written=["concepts/x.md"], extract=None)


def _env(tmp: Path, *, raises: IngestError | None = None):
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "x.md").write_text(
        '---\ntype: concept\ntitle: "X"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
        "---\n# x\n\n原始正文。\n",
        encoding="utf-8",
    )
    git = WikiGitManager(wiki)
    git.commit_all("wiki: seed")  # 预置内容必须已结算——否则 pre-reset 当残骸清掉
    records = tmp / "workspace" / "provenance" / "sources"
    records.mkdir(parents=True)
    service = JobService(tmp / "workspace", wiki_dir=wiki, source_records_dir=records)
    ops = WikiOpsConsumer(
        _RefinePipeline(wiki, raises),
        wiki_dir=wiki,
        source_records_dir=records,
        git=git,
    )
    worker = JobWorker(service)
    worker.register("refine", ops.handle_refine)
    worker.register("restructure", ops.handle_restructure)
    return wiki, git, service, worker, records


def _pump(worker: JobWorker, rounds: int) -> None:
    import asyncio

    for _ in range(rounds):
        asyncio.run(worker.run_once())


def test_refine_success_commits_page_with_batch_trailer(tmp_path: Path):
    wiki, git, service, worker, _ = _env(tmp_path)
    jobs = service.submit_refine_batch()
    assert len(jobs) == 1 and jobs[0].kind == "refine"
    _pump(worker, 1)

    row = service.store.get(jobs[0].id)
    assert row.status == "succeeded"
    assert git.is_clean()
    assert "精炼后的正文" in (wiki / "concepts" / "x.md").read_text(encoding="utf-8")
    assert len(git.batch_commits(str(jobs[0].payload["batch"]))) == 1
    assert "refine: concepts/x" in " ".join(git.history())
    assert service.issues.list() == [], "批操作结果不进问题账本"


def test_refine_failure_restores_debris_and_records_nothing(tmp_path: Path):
    wiki, git, service, worker, records = _env(
        tmp_path, raises=IngestError(IngestStage.PLAN, "plan 失败", source="x.md")
    )
    baseline = git.head()
    jobs = service.submit_refine_batch()
    _pump(worker, 1)

    row = service.store.get(jobs[0].id)
    assert row.status == "failed" and "plan 失败" in row.error
    assert git.head() == baseline and git.is_clean(), "失败不进历史"
    assert "原始正文" in (wiki / "concepts" / "x.md").read_text(encoding="utf-8")
    debris = records.parent / "debris" / f"{jobs[0].id}.patch"
    assert debris.is_file() and "半成品" in debris.read_text(encoding="utf-8")
    assert service.issues.list() == [], "refine 失败不是用户待办账"
    # 再点一次 /refine 就是重试
    assert len(service.submit_refine_batch()) == 1


def test_refine_page_gone_after_snapshot_is_noop(tmp_path: Path):
    wiki, git, service, worker, _ = _env(tmp_path)
    jobs = service.submit_refine_batch()
    (wiki / "concepts" / "x.md").unlink()
    git.commit_all("sync: delete x")  # 模拟并发删除已结算
    _pump(worker, 1)
    assert service.store.get(jobs[0].id).status == "succeeded"


def test_restructure_success_one_commit(tmp_path: Path, monkeypatch):
    import wiki_agent.application.wiki_ops as wiki_ops

    wiki, git, service, worker, _ = _env(tmp_path)

    def fake_execute(wiki_dir, proposals):
        (Path(wiki_dir) / "concepts" / "merged.md").write_text(
            '---\ntype: concept\ntitle: "Merged"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
            "---\n# Merged\n\n合并后的正文内容。\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            actions=[1] * len(proposals), skipped=[], backed_up=[]
        )

    monkeypatch.setattr(wiki_ops, "execute", fake_execute)
    jobs = service.submit_restructure(
        [{"op": "merge", "pages": ["concepts/x"], "target": "concepts/x", "reason": "t"}]
    )
    _pump(worker, 1)

    job = jobs[0]
    row = service.store.get(job.id)
    assert row.status == "succeeded", row.error
    commits = git.batch_commits(str(job.payload["batch"]))
    assert len(commits) == 1
    assert "restructure: merge concepts/x" in " ".join(git.history())
    assert (wiki / "concepts" / "merged.md").exists()
    assert service.issues.list() == []


def test_restructure_gate_rolls_back_only_its_unit(tmp_path: Path, monkeypatch):
    """单个单元被闸门撤销：只恢复本单元，前面的单元保留在历史里。"""
    import wiki_agent.application.wiki_ops as wiki_ops

    wiki, git, service, worker, _ = _env(tmp_path)

    def per_unit_execute(wiki_dir, proposals):
        reason = proposals[0].reason  # handler 已把 payload dict 还原为 Proposal
        (Path(wiki_dir) / "concepts" / f"{reason}.md").write_text(
            '---\ntype: concept\ntitle: "单元页"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
            f"---\n# {reason}\n\n单元产出的正文内容。\n",
            encoding="utf-8",
        )
        if reason == "bad":
            return SimpleNamespace(actions=[], skipped=["lock conflict"], backed_up=[])
        return SimpleNamespace(actions=["ok"], skipped=[], backed_up=[])

    monkeypatch.setattr(wiki_ops, "execute", per_unit_execute)
    jobs = service.submit_restructure(
        [
            {"op": "merge", "pages": ["concepts/x"], "target": "concepts/x", "reason": "good"},
            {"op": "delete", "pages": ["concepts/x"], "reason": "bad"},
        ]
    )
    assert len(jobs) == 2
    _pump(worker, 2)

    statuses = [service.store.get(job.id).status for job in jobs]
    assert statuses == ["succeeded", "failed"]
    batch = str(jobs[0].payload["batch"])
    assert len(git.batch_commits(batch)) == 1
    assert (wiki / "concepts" / "good.md").exists(), "已提交单元保留"
    assert not (wiki / "concepts" / "bad.md").exists(), "被撤销单元只撤自己"
    assert git.is_clean()
    assert service.issues.list() == []


def test_restructure_dependent_unit_degrades_after_upstream_delete(tmp_path: Path):
    """真实 execute：上游单元把页删掉后，引用同页的单元命中"页面不存在"→自行撤销。"""
    wiki, git, service, worker, _ = _env(tmp_path)
    (wiki / "concepts" / "b.md").write_text(
        '---\ntype: concept\ntitle: "B"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
        "---\n# B\n\nB 页面正文内容。\n",
        encoding="utf-8",
    )
    (wiki / "concepts" / "c.md").write_text(
        '---\ntype: concept\ntitle: "C"\nsummary: "s"\ngoal: "g"\nrelated: []\n'
        "---\n# C\n\nC 页面正文内容。\n",
        encoding="utf-8",
    )
    git.commit_all("wiki: seed extra")

    jobs = service.submit_restructure(
        [
            {"op": "delete", "pages": ["concepts/b"], "reason": "dup"},
            {"op": "delete", "pages": ["concepts/b"], "reason": "second"},
        ]
    )
    _pump(worker, 2)
    statuses = [service.store.get(job.id).status for job in jobs]
    assert statuses[0] == "succeeded"
    assert statuses[1] == "failed", "同页第二个单元被状态复核挡下"
    assert not (wiki / "concepts" / "b.md").exists()
    assert (wiki / "concepts" / "c.md").exists()
    assert git.is_clean()


def test_restructure_batch_mutex(tmp_path: Path):
    """一批未全部到终态时拒绝第二笔提交（保证"撤销这一批"边界清晰）。"""
    from wiki_agent.jobs import RestructureInProgress

    _, _git, service, _worker, _ = _env(tmp_path)
    service.submit_restructure(
        [{"op": "delete", "pages": ["concepts/x"], "reason": "t"}]
    )
    with pytest.raises(RestructureInProgress):
        service.submit_restructure(
            [{"op": "delete", "pages": ["concepts/y"], "reason": "t2"}]
        )


def test_refine_idempotency_single_in_flight_per_page(tmp_path: Path):
    wiki, git, service, worker, _ = _env(tmp_path)
    first = service.submit_refine_batch()
    again = service.submit_refine_batch()
    assert [j.id for j in again] == [j.id for j in first], "同页在途收敛为同一 job"
    assert service.store.count_in_flight() == 1


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    for name, fn in tests.items():
        args = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        if "monkeypatch" in args:
            print(f"  – {name}（依赖 monkeypatch，请用 pytest 运行）")
            continue
        try:
            fn(Path(tempfile.mkdtemp()))
            print(f"  ✓ {name}")
        except Exception:
            failed += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
