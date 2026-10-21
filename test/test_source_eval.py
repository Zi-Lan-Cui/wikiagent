"""评测记录（source-eval）端到端——LLM 以 mock 注入，语料用 tmp 合成夹具。

语料数据已 gitignore（dev/test 由 recompile 现生成），故测试不读任何真实语料，
一律在 tmp 里搭 run-evidence 布局。覆盖：eval_source 全阶段、缺失阶段不进分母、
judge_failed、确定性 gate 失败、X-FM 存档页回归、update 前像记录优先与 fail-closed、
事件接线、写盘往返。

直接运行:  .venv/bin/python test/test_source_eval.py
"""

import asyncio
import json
from pathlib import Path

import pytest

from evals.commands import source_eval
from evals.commands.reference_corpus import _sha256
from evals.commands.source_eval import (
    _GRADER_PATH,
    _artifact_files,
    _det_artifact,
    _input_files,
    _prepare_run_dir,
    _update_baselines,
    eval_source,
    write_record,
)
from evals.core.deterministic import run_checks
from evals.core.reference_models import DimensionScore, load_grader

GRADER = load_grader(_GRADER_PATH)
_EVID = "baselines/seeded/run-evidence"
_FM = (
    "---\ntype: concept\ntitle: \"T\"\nsummary: s\ntags: [x]\ngoal: g\n"
    "gaps: none\ncreated: 2026-10-22\nupdated: 2026-10-22\nrelated: []\nsources: [x.md]\n---\n正文\n"
)


def _fm_missing_tags() -> str:
    return _FM.replace("tags: [x]\n", "")


def _index_content(n: int) -> str:
    lines = [f"- [[concepts/p{i}]] — [concept] concepts/p{i}.md — 标题{i} — s — 使命: g{i}" for i in range(n)]
    return "## 已有页面 Index\n" + "\n".join(lines) + "\n"


async def _fake_judge(client, stage, inputs, artifact, rubric):
    return {
        "dimensions": [
            DimensionScore(dimension_id=dimension.id, score=4, rationale="mock")
            for dimension in rubric.dimensions
        ],
        "raw": {},
        "usage": None,
        "model": "mock-judge",
    }


@pytest.fixture()
def mock_judge(monkeypatch):
    monkeypatch.setattr(source_eval, "judge_rubric", _fake_judge)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _source(root: Path, name: str, *, count: int, plan: list[dict], pages: dict[str, str] | None = None,
            archived: list[str] | None = None, recorded: dict[str, str] | None = None) -> None:
    """在 root 下搭一篇 source 的 run-evidence：源文件、index 快照、五阶段产物、页面、targets、update-baselines。"""
    stem = name.removesuffix(".md")
    _write(root, f"baselines/seeded/source/{name}", "# 原文\n内容")
    _write(root, f"{_EVID}/index-snapshots/{stem}.index.json",
           json.dumps({"index_content": _index_content(count)}))
    _write(root, f"{_EVID}/compile-time-trees/{stem}.tree.json", json.dumps({"existing_pages": [], "planned_targets": []}))
    _write(root, f"{_EVID}/artifacts/{name}/extract.txt", "摘要正文")
    _write(root, f"{_EVID}/artifacts/{name}/search.json", json.dumps({"rel_paths": [], "raw": "{}"}))
    _write(root, f"{_EVID}/artifacts/{name}/analyze.json",
           json.dumps({"analysis_text": "分", "raw": "{}", "entities": [], "concepts": [], "relationships": []}))
    _write(root, f"{_EVID}/artifacts/{name}/plan.json", json.dumps({"page_targets": plan, "raw": "{}"}))
    art = f"{_EVID}/artifacts/{name}"
    for fname, fm in (pages or {}).items():
        _write(root, f"{art}/pages/{fname}", "# generated at: 2026-10-22T00:00:00\n\n" + fm)
        _write(root, f"{art}/targets/{fname.removesuffix('.md')}.json",
               json.dumps({"target": {"wiki_path": fname.replace("_", "/"), "disposition": "new"}}))
    for arch in archived or []:
        _write(root, f"{art}/pages/{arch}", "# generated at: x\n\n" + _FM)
    for arch, body in (recorded or {}).items():
        _write(root, f"{art}/update-baselines/{arch}", "# generated at: x\n\n" + body)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("corpus")
    _source(root, "full.md", count=1, plan=[{"wiki_path": "concepts/a.md", "disposition": "new", "page_type": "concept"}],
            pages={"concepts_a.md": _FM})
    # 只有 extract → 其余阶段 missing_artifact
    _write(root, "baselines/seeded/source/only-extract.md", "# 原文\nx")
    _write(root, f"{_EVID}/artifacts/only-extract.md/extract.txt", "摘要")
    # execute 页缺 tags → X-FM gate 失败
    _source(root, "badfm.md", count=1, plan=[{"wiki_path": "concepts/a.md", "disposition": "new", "page_type": "concept"}],
            pages={"concepts_a.md": _fm_missing_tags()})
    # update 前像：记录优先
    _source(root, "recorded.md", count=2, plan=[{"wiki_path": "concepts/x.md", "disposition": "update"}],
            pages={}, recorded={"concepts_x.md": _FM})
    # 干净重建：主题 y 只有唯一更早写入者 w-b（count1），w-clean-cur（count2）无记录 → 取 w-b
    _source(root, "w-b.md", count=1, plan=[{"wiki_path": "concepts/y.md", "disposition": "new"}], archived=["concepts_y.md"])
    _source(root, "w-clean-cur.md", count=2, plan=[{"wiki_path": "concepts/y.md", "disposition": "update"}], pages={})
    # fail-closed：主题 x 有 writer w-a（count1）外，还有不可排序写入者 w-m（同 count1、更新过 x）
    _source(root, "w-a.md", count=1, plan=[{"wiki_path": "concepts/x.md", "disposition": "new"}], archived=["concepts_x.md"])
    _source(root, "w-m.md", count=1, plan=[{"wiki_path": "concepts/x.md", "disposition": "update"}], archived=["concepts_x.md"])
    _source(root, "w-amb-cur.md", count=2, plan=[{"wiki_path": "concepts/x.md", "disposition": "update"}], pages={})
    return root


def _run_sync(corpus: Path, source: str):
    from types import SimpleNamespace

    client = SimpleNamespace(model_id="mock-judge")
    return asyncio.run(eval_source(client, corpus, source, GRADER, "test-run"))


def test_full_record_all_stages_scored(mock_judge, corpus):
    record = _run_sync(corpus, "full.md")
    assert len(record.stages) == 5
    assert all(s.status == "scored" for s in record.stages)
    assert record.judge_model == "mock-judge"
    assert record.source_score == 4.0  # 全维 mock 4，权重归一后仍是 4


def test_missing_stages_excluded_from_denominator(mock_judge, corpus):
    record = _run_sync(corpus, "only-extract.md")
    by_stage = {s.stage: s for s in record.stages}
    assert by_stage["search"].status == "missing_artifact"
    assert by_stage["execute"].status == "missing_artifact"
    assert by_stage["extract"].stage_score == 4.0
    assert record.source_score == 4.0  # 缺席阶段不进分母，总分仍 4


def test_gate_failure_nulls_stage_score(mock_judge, corpus):
    """X-FM 拦下缺 tags 的页 → execute stage_score=None，其余照评。"""
    record = _run_sync(corpus, "badfm.md")
    ex = next(s for s in record.stages if s.stage == "execute")
    assert ex.stage_score is None
    assert any(c.rule_id == "X-FM" and not c.passed for c in ex.deterministic)


def test_judge_failure_marks_judge_failed(monkeypatch, corpus):
    async def explode(client, stage, inputs, artifact, rubric):
        raise RuntimeError("LLM 不可用")

    monkeypatch.setattr(source_eval, "judge_rubric", explode)
    record = _run_sync(corpus, "full.md")
    assert all(s.status == "judge_failed" for s in record.stages)
    assert record.source_score is None
    assert record.verdict == "incomplete"


def test_dry_existing_run_dir_rejected(tmp_path: Path):
    _prepare_run_dir(tmp_path, "run-1")
    with pytest.raises(FileExistsError):
        _prepare_run_dir(tmp_path, "run-1")


def test_single_file_digest_matches_reference(corpus):
    files = _artifact_files(corpus, "full.md")["extract"]
    assert _det_artifact("extract", files) == "摘要正文"
    from evals.commands.source_eval import _artifact_digest

    assert _artifact_digest(files) == _sha256(files[0])


def test_execute_inputs_carry_targets_and_recorded_baseline(corpus):
    """execute 输入面须含 targets 理由与记录的 update 前像。"""
    inputs = _input_files(corpus, "recorded.md")["execute"]
    names = [str(p.relative_to(corpus)) for p in inputs]
    assert any("artifacts/recorded.md/targets/" in n or "update-baselines" in n for n in names), names


def test_update_baseline_recorded_preferred(corpus):
    """存在 update-baselines/ 即直接采信（真记录）。"""
    got = _update_baselines(corpus, "recorded.md")
    assert len(got) == 1 and got[0].name == "concepts_x.md"
    assert "update-baselines" in str(got[0])


def test_update_baseline_reconstructs_single_writer(corpus):
    """无记录时从更早唯一写入者的存档页重建。"""
    got = _update_baselines(corpus, "w-clean-cur.md")
    assert [p.name for p in got] == ["concepts_y.md"]
    assert "w-b.md" in str(got[0])


def test_update_baseline_fail_closed_on_ambiguous_writer(corpus):
    """writer 与本篇之间还有不可排序的写入者 → 基线不给（fail-closed）。"""
    assert _update_baselines(corpus, "w-amb-cur.md") == []


def test_xfm_passes_on_real_shaped_pages():
    """X-FM 对带 `# generated at:` 的合法存档页必须全过（历史回归点）。"""
    det = {"pages": [_page_fm(_FM)]}
    results = run_checks("execute", det, GRADER.deterministic_checks["execute"])
    assert [r.passed for r in results] == [True]


def _page_fm(text: str) -> dict:
    from evals.commands.source_eval import _page_frontmatter

    return _page_frontmatter("# generated at: x\n\n" + text)


def test_stage_events_emitted(monkeypatch, corpus):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(source_eval, "emit_event", lambda name, **kw: events.append((name, kw)))

    async def explode(client, stage, inputs, artifact, rubric):
        raise RuntimeError("LLM 不可用")

    monkeypatch.setattr(source_eval, "judge_rubric", explode)
    _run_sync(corpus, "full.md")
    names = [n for n, _ in events]
    assert "rubric_judge_failed" in names
    assert names.count("stage_judged") == 5
    assert all(kw["source"] == "full.md" for _, kw in events)


def test_write_record_roundtrip(mock_judge, corpus, tmp_path: Path):
    record = _run_sync(corpus, "full.md")
    run_dir = _prepare_run_dir(tmp_path, record.run_id)
    path = write_record(record, run_dir)
    assert path.name == "source-eval-full.md.json"
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "test-run"
