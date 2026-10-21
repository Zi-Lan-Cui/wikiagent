"""CompileHook 机制验收——事件次序、失败批发 batch_end、钩子异常隔离。

直接运行:  .venv/bin/python test/test_compile_hooks.py
"""

import asyncio
from types import SimpleNamespace

import pytest

from wiki_agent.compiler.hooks import CompileHook
from wiki_agent.compiler.models import (
    AnalysisResult,
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
    SearchResult,
)
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.materials.converters.base import ConvertedFile
from wiki_agent.materials.loader import FileModality, RawFileProperties


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    def on_compile_event(self, event) -> None:
        self.events.append((event.stage, event.error))


def _raw(tmp_path):
    return RawFileProperties(name="中文笔记.md", ext="md", path=tmp_path / "中文笔记.md", modality=FileModality.RICH)


def _pipeline(tmp_path, recorder):
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
            return SearchResult(rel_paths=["concepts/old.md"])

        async def analyze(self, *_):
            return AnalysisResult(source_identity="中文笔记.md", analysis_text="分析")

        async def plan(self, *_args, **_kwargs):
            return IntegrationPlan(
                page_targets=[PageTarget("concepts/example.md", "Example", Disposition.NEW)]
            )

        async def execute(self, *_):
            page = tmp_path / "concepts"
            page.mkdir(exist_ok=True)
            (page / "example.md").write_text("---\ntitle: Example\n---\n正文", encoding="utf-8")
            return [PageTarget("concepts/example.md", "Example", Disposition.NEW)]

    pipeline = CompilePipeline.__new__(CompilePipeline)
    pipeline._converter = Converter()
    pipeline._extractor = Extractor()
    pipeline._chunker = SimpleNamespace(chunk=lambda _: [])
    pipeline._integrator = Integrator()
    pipeline._wiki_dir = tmp_path
    pipeline._on_progress = None
    pipeline._hooks = tuple([recorder] if recorder is not None else [])
    pipeline._append_index = lambda _plan, _written: None
    return pipeline


def test_stage_events_emitted_in_order(tmp_path):
    recorder = Recorder()
    pipeline = _pipeline(tmp_path, recorder)
    asyncio.run(pipeline.ingest_one(_raw(tmp_path)))
    assert [stage for stage, _ in recorder.events] == [
        "extract",
        "batch_start",
        "search",
        "analyze",
        "plan",
        "pre_execute",
        "batch_end",
    ]
    assert all(error is None for _, error in recorder.events)


def test_failed_batch_still_emits_batch_end_with_error(tmp_path):
    class FailingExtractor:
        async def extract(self, _):
            return ExtractResult(source_identity="中文笔记.md", document_summary="  ")

    recorder = Recorder()
    pipeline = _pipeline(tmp_path, recorder)
    pipeline._extractor = FailingExtractor()
    with pytest.raises(Exception):
        asyncio.run(pipeline.ingest_one(_raw(tmp_path)))
    # 空摘要在 extract 闸门即抛：未过闸的阶段不发事件，但 batch_end 必发
    assert [stage for stage, _ in recorder.events] == ["batch_end"]
    assert recorder.events[-1][1] is not None


def test_hook_exception_does_not_break_compilation(tmp_path):
    class Exploding:
        def on_compile_event(self, event) -> None:
            if event.stage == "plan":
                raise RuntimeError("sink 磁盘满")

    pipeline = _pipeline(tmp_path, Exploding())
    outcome = asyncio.run(pipeline.ingest_one(_raw(tmp_path)))
    assert outcome.pages_written == ["concepts/example.md"]


def test_default_no_hooks_pipeline_runs(tmp_path):
    pipeline = _pipeline(tmp_path, None)
    outcome = asyncio.run(pipeline.ingest_one(_raw(tmp_path)))
    assert not outcome.noop


def test_compilehook_runtime_checkable():
    assert isinstance(Recorder(), CompileHook)
