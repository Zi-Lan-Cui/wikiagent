"""Pipeline 在 LLM 前的 source/summary 空值闸门测试。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiki_agent.compiler.models import (
    AnalysisResult,
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
    SearchResult,
)
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.documents.converters.base import ConvertedFile
from wiki_agent.documents.loader import FileModality, RawFileProperties
from wiki_agent.errors import IngestError, IngestStage


def _raw(tmp_path: Path) -> RawFileProperties:
    return RawFileProperties(
        name="中文笔记.md",
        ext="md",
        path=tmp_path / "中文笔记.md",
        modality=FileModality.RICH,
    )


def _pipeline(converter, extractor, chunker=None):
    pipeline = CompilePipeline.__new__(CompilePipeline)
    pipeline._converter = converter
    pipeline._extractor = extractor
    pipeline._chunker = chunker or SimpleNamespace(chunk=lambda _: [])
    pipeline._mode = "compile"
    return pipeline


def test_empty_converted_content_fails_before_extractor(tmp_path):
    class Converter:
        async def convert(self, _):
            return ConvertedFile(
                content="",
                name="中文笔记.md",
                ext="md",
                path=str(tmp_path / "中文笔记.md"),
                modality="rich",
            )

    class Extractor:
        async def extract(self, _):
            raise AssertionError("空转换结果不应调用 Extractor")

    with pytest.raises(IngestError) as caught:
        asyncio.run(_pipeline(Converter(), Extractor())._ingest_one(_raw(tmp_path)))
    assert caught.value.stage == IngestStage.CONVERT
    assert caught.value.error_code == "empty_converted_content"


def test_empty_extract_summary_fails_before_plan(tmp_path):
    class Converter:
        async def convert(self, _):
            return ConvertedFile(
                content="# 有内容\n正文",
                name="中文笔记.md",
                ext="md",
                path=str(tmp_path / "中文笔记.md"),
                modality="rich",
            )

    class Extractor:
        async def extract(self, _):
            return ExtractResult(source_identity="中文笔记.md", document_summary="  ")

    with pytest.raises(IngestError) as caught:
        asyncio.run(_pipeline(Converter(), Extractor())._ingest_one(_raw(tmp_path)))
    assert caught.value.stage == IngestStage.EXTRACT
    assert caught.value.error_code == "empty_extract_summary"


def test_pipeline_reports_current_stage_before_work_starts(tmp_path):
    stages: list[str] = []

    class Converter:
        async def convert(self, _):
            return ConvertedFile(
                content="# 有内容\n正文",
                name="中文笔记.md",
                ext="md",
                path=str(tmp_path / "中文笔记.md"),
                modality="rich",
            )

    class Extractor:
        async def extract(self, _):
            return ExtractResult(source_identity="中文笔记.md", document_summary="")

    pipeline = _pipeline(Converter(), Extractor())
    pipeline._on_progress = stages.append

    with pytest.raises(IngestError):
        asyncio.run(pipeline._ingest_one(_raw(tmp_path)))

    assert stages == ["convert", "extract"]


def test_pipeline_preserves_structured_execute_error(tmp_path: Path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("", encoding="utf-8")

    class Converter:
        async def convert(self, _):
            return ConvertedFile(
                content="# 有内容\n正文",
                name="中文笔记.md",
                ext="md",
                path=str(tmp_path / "中文笔记.md"),
                modality="rich",
            )

    class Extractor:
        async def extract(self, _):
            return ExtractResult(source_identity="中文笔记.md", document_summary="摘要")

    expected = IngestError(
        IngestStage.EXECUTE,
        "1 个页面生成失败",
        raw='[{"path":"concepts/example.md","error":"bad page"}]',
        error_code="page_generation_failed",
        error_class="transient",
        retry_policy="auto_retry",
    )

    class Integrator:
        async def search(self, *_):
            return SearchResult()

        async def analyze(self, *_):
            return AnalysisResult(source_identity="中文笔记.md")

        async def plan(self, *_args, **_kwargs):
            return IntegrationPlan(
                page_targets=[PageTarget("concepts/example.md", "Example", Disposition.NEW)]
            )

        async def execute(self, *_):
            raise expected

    pipeline = _pipeline(Converter(), Extractor())
    pipeline._wiki_dir = wiki
    pipeline._integrator = Integrator()
    pipeline._index_reader = None
    pipeline._ensure_index = lambda: None
    pipeline._read_optional = lambda _name: ""
    pipeline._current_page = lambda _raw: ""

    with pytest.raises(IngestError) as caught:
        asyncio.run(pipeline._ingest_one(_raw(tmp_path)))

    assert caught.value is expected
    assert caught.value.raw == expected.raw
    assert caught.value.error_code == "page_generation_failed"


def test_empty_search_candidates_still_reach_plan(tmp_path):
    """空候选（冷启动）不得让 pipeline 跳过 analyze→plan。

    回归: 首跑时 index 无候选页，pipeline 曾返回空分析并跳过 plan，
    导致结果依赖文件顺序、首篇总是 noop。修复后 analyze 收到空
    候选也执行，plan 拿到分析文本后自主决策。
    """

    class Converter:
        async def convert(self, _):
            return ConvertedFile(
                content="# 有内容\n正文",
                name="中文笔记.md",
                ext="md",
                path=str(tmp_path / "中文笔记.md"),
                modality="rich",
            )

    class Extractor:
        async def extract(self, _):
            return ExtractResult(source_identity="中文笔记.md", document_summary="摘要")

    class Integrator:
        async def search(self, *_):
            return SearchResult()  # 冷启动: 无候选

        async def analyze(self, *_):
            # 不再早退——即使无候选也应产出分析文本
            return AnalysisResult(source_identity="中文笔记.md", analysis_text="分析文本")

        def __init__(self):
            self.planned = False

        async def plan(self, *_args, **_kwargs):
            self.planned = True
            return IntegrationPlan(
                page_targets=[PageTarget("concepts/example.md", "Example", Disposition.NEW)]
            )

        async def execute(self, *_):
            return [PageTarget("concepts/example.md", "Example", Disposition.NEW)]

    (tmp_path / "index.md").write_text("", encoding="utf-8")
    pipeline = _pipeline(Converter(), Extractor())
    pipeline._wiki_dir = tmp_path
    pipeline._integrator = Integrator()
    pipeline._ensure_index = lambda: None
    pipeline._read_optional = lambda _name: ""
    pipeline._current_page = lambda _raw: ""
    pipeline._append_index = lambda _plan, _written: None
    pipeline._index_reader = None

    asyncio.run(pipeline._ingest_one(_raw(tmp_path)))

    assert pipeline._integrator.planned


def test_partial_execute_failure_still_indexes_written_pages(tmp_path):
    """execute 部分成功：失败 source 的存活页面必须进 index。

    回归: execute 多目标并行生成，部分页面失败会抛 IngestError 并
    跳过 _append_index——成功落盘的页面因此变成幽灵页（磁盘有、
    index 无），对后续 search/analyze 不可见。
    """
    (tmp_path / "index.md").write_text("", encoding="utf-8")
    (tmp_path / "concepts").mkdir()

    class Converter:
        async def convert(self, _):
            return ConvertedFile(
                content="# 有内容\n正文",
                name="中文笔记.md",
                ext="md",
                path=str(tmp_path / "中文笔记.md"),
                modality="rich",
            )

    class Extractor:
        async def extract(self, _):
            return ExtractResult(source_identity="中文笔记.md", document_summary="摘要")

    class Integrator:
        async def search(self, *_):
            return SearchResult()

        async def analyze(self, *_):
            return AnalysisResult(source_identity="中文笔记.md", analysis_text="分析")

        async def plan(self, *_args, **_kwargs):
            return IntegrationPlan(
                page_targets=[PageTarget("concepts/example.md", "Example", Disposition.NEW)]
            )

        async def execute(self, *_):
            # 页面先落盘，随后整体抛错——模拟部分成功
            page = tmp_path / "concepts" / "example.md"
            page.write_text(
                "---\n"
                "type: concept\n"
                "title: Example\n"
                "summary: 示例页\n"
                "goal: 示例\n"
                "sources: [中文笔记.md]\n"
                "---\n# Example\n正文内容\n"
            )
            raise IngestError(IngestStage.EXECUTE, "1 个页面生成失败", source="中文笔记.md")

    pipeline = _pipeline(Converter(), Extractor())
    pipeline._wiki_dir = tmp_path
    pipeline._integrator = Integrator()
    pipeline._ensure_index = lambda: None
    pipeline._read_optional = lambda _name: ""
    pipeline._current_page = lambda _raw: ""
    pipeline._index_reader = None

    with pytest.raises(IngestError):
        asyncio.run(pipeline._ingest_one(_raw(tmp_path)))

    assert "[[concepts/example]]" in (tmp_path / "index.md").read_text(encoding="utf-8")


def test_index_entry_contains_goal(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("", encoding="utf-8")
    page = wiki / "concepts" / "asyncio.md"
    page.parent.mkdir()
    page.write_text(
        "---\n"
        "type: concept\n"
        "title: asyncio\n"
        "summary: 事件循环与协程\n"
        "goal: 解释异步调度模型和使用边界\n"
        "---\n# asyncio\n",
        encoding="utf-8",
    )
    pipeline = object.__new__(CompilePipeline)
    pipeline._wiki_dir = wiki
    plan = IntegrationPlan(
        page_targets=[
            PageTarget(
                wiki_path="concepts/asyncio.md",
                title="asyncio",
                disposition=Disposition.NEW,
            )
        ]
    )
    pipeline._append_index(plan, ["concepts/asyncio.md"])
    index = (wiki / "index.md").read_text(encoding="utf-8")
    assert "事件循环与协程" in index
    assert "goal: 解释异步调度模型和使用边界" in index
