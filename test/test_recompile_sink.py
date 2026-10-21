"""run-evidence 落盘钩子验收——文件形状、update 前像真记录、index 行解析。

直接运行:  .venv/bin/python test/test_recompile_sink.py
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.commands.recompile import EvidenceSink, parse_index_lines
from wiki_agent.compiler.hooks import CompileEvent
from wiki_agent.compiler.models import (
    AnalysisResult,
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageRelationship,
    PageTarget,
    SearchResult,
)
from wiki_agent.wiki.pages import index_line


def _event(stage: str, tmp_path: Path, outcome=None, index_content=None, error=None) -> CompileEvent:
    return CompileEvent(
        source="note-x.md",
        stage=stage,
        wiki_dir=tmp_path / "wiki",
        outcome=outcome or SimpleNamespace(extract=None, search=None, analysis=None, plan=None),
        index_content=index_content,
        error=error,
    )


def test_index_line_roundtrip():
    line = index_line("concepts/lease", "concept", "租约模型", "有时间限制的契约", "系统阐述租约语义")
    pages = parse_index_lines("前言行\n" + line + "\n")
    assert pages == [
        {
            "path": "concepts/lease.md",
            "type": "concept",
            "title": "租约模型",
            "summary": "有时间限制的契约",
            "goal": "系统阐述租约语义",
        }
    ]


def test_index_line_without_summary_and_goal(tmp_path):
    pages = parse_index_lines("- [[entities/redis]] — [entity] entities/redis.md — Redis")
    assert pages[0]["title"] == "Redis"
    assert pages[0]["summary"] == ""
    assert pages[0]["goal"] == ""


def test_stage_files_written(tmp_path):
    evidence = tmp_path / "evidence"
    sink = EvidenceSink(evidence)
    outcome = SimpleNamespace(
        extract=ExtractResult(source_identity="note-x.md", document_summary="摘要正文"),
        search=SearchResult(rel_paths=["concepts/a.md"], raw="rawjson"),
        analysis=AnalysisResult(
            source_identity="note-x.md",
            analysis_text="分析",
            raw_analysis="raw",
            entities=[{"name": "e"}],
            concepts=[],
            relationships=[PageRelationship(to_page="concepts/a.md", relation="extends", detail="d")],
        ),
        plan=IntegrationPlan(
            page_targets=[PageTarget("concepts/b.md", "B", Disposition.NEW, reason="两个事实", page_type="concept")],
            raw="planraw",
        ),
        pages_written=[],
    )
    sink.on_compile_event(_event("extract", tmp_path, outcome))
    sink.on_compile_event(_event("batch_start", tmp_path, outcome, index_content="- [[concepts/a]] — [concept] concepts/a.md — A"))
    sink.on_compile_event(_event("search", tmp_path, outcome))
    sink.on_compile_event(_event("analyze", tmp_path, outcome))
    sink.on_compile_event(_event("plan", tmp_path, outcome))
    sink.on_compile_event(_event("batch_end", tmp_path, outcome))

    art = evidence / "artifacts" / "note-x.md"
    assert (art / "extract.txt").read_text(encoding="utf-8") == "摘要正文"
    search = json.loads((art / "search.json").read_text(encoding="utf-8"))
    assert search == {"source": "note-x.md", "rel_paths": ["concepts/a.md"], "raw": "rawjson"}
    analyze = json.loads((art / "analyze.json").read_text(encoding="utf-8"))
    assert analyze["relationships"][0]["relation"] == "extends"
    plan = json.loads((art / "plan.json").read_text(encoding="utf-8"))
    assert plan["page_targets"][0]["page_type"] == "concept"
    snapshot = json.loads((evidence / "index-snapshots" / "note-x.index.json").read_text(encoding="utf-8"))
    assert snapshot["source"] == "note-x.md"
    tree = json.loads((evidence / "compile-time-trees" / "note-x.tree.json").read_text(encoding="utf-8"))
    assert tree["schema"] == "compile-time-tree-v1"
    assert tree["existing_pages"][0]["path"] == "concepts/a.md"
    assert tree["planned_targets"][0]["goal"] == "两个事实"


def test_update_baseline_recorded_at_pre_execute(tmp_path):
    """前像只在 pre_execute 窗口可读——execute 后旧页已被覆盖。"""
    wiki = tmp_path / "wiki"
    old_page = wiki / "concepts" / "b.md"
    old_page.parent.mkdir(parents=True)
    old_page.write_text("---\ntitle: B\n---\n旧内容", encoding="utf-8")
    evidence = tmp_path / "evidence"
    sink = EvidenceSink(evidence)
    outcome = SimpleNamespace(
        plan=IntegrationPlan(
            page_targets=[PageTarget("concepts/b.md", "B", Disposition.UPDATE, reason="补充两点")],
            raw="",
        )
    )
    sink.on_compile_event(_event("pre_execute", tmp_path, outcome))
    archived = evidence / "artifacts" / "note-x.md" / "update-baselines" / "concepts_b.md"
    assert "# generated at:" in archived.read_text(encoding="utf-8")
    assert "旧内容" in archived.read_text(encoding="utf-8")


def test_batch_end_flushes_pages_targets_and_error(tmp_path):
    """pages 只采本轮写出的页；失败批磁盘上的 update 旧页不作假冒产物拷入。"""
    wiki = tmp_path / "wiki"
    page = wiki / "concepts" / "b.md"
    page.parent.mkdir(parents=True)
    evidence = tmp_path / "evidence"
    sink = EvidenceSink(evidence)
    target = PageTarget("concepts/b.md", "B", Disposition.UPDATE, reason="补充两点", page_type="concept")
    plan = IntegrationPlan(page_targets=[target], raw="")
    art = evidence / "artifacts" / "note-x.md"

    # update 失败：旧页仍在磁盘，但本批 pages_written 为空 → targets 照记，pages 不产
    page.write_text("---\ntitle: B\n---\n旧内容", encoding="utf-8")
    sink.on_compile_event(_event("batch_end", tmp_path, SimpleNamespace(plan=plan, pages_written=[]), error=RuntimeError("boom")))
    assert json.loads((art / "targets" / "concepts_b.json").read_text(encoding="utf-8"))["target"]["disposition"] == "update"
    assert not (art / "pages" / "concepts_b.md").exists(), "失败批不得把磁盘旧页当本轮产物"
    assert json.loads((art / "error.json").read_text(encoding="utf-8"))["error"] == "boom"

    # 本轮写出该页：带标记拷贝
    page.write_text("---\ntitle: B\n---\n新内容", encoding="utf-8")
    sink.on_compile_event(_event("batch_end", tmp_path, SimpleNamespace(plan=plan, pages_written=["concepts/b.md"])))
    copied = (art / "pages" / "concepts_b.md").read_text(encoding="utf-8")
    assert "# generated at:" in copied and "新内容" in copied


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
