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
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.ingestion.converter.base import ConvertedFile
from wiki_agent.ingestion.data_loader import FileModality, RawFileProperties


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
