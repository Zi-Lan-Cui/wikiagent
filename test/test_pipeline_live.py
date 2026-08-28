"""真实 LLM 的 compile pipeline 行为试验。

默认跳过，显式运行：

    RUN_LIVE_LLM_TESTS=1 uv run pytest -s -q test/test_pipeline_live.py

这不是 mock 单元测试：extract/search/analyze/plan/execute 都使用真实 LLM。
测试只报告模型决策和实际文件状态，不把模型的自由决策硬编码成断言。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from wiki_agent.compiler import Extractor, compile_integrator
from wiki_agent.compiler.models import SourceChunk, SourceDocument
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    {
        "id": "low-information-draft",
        "name": "低信息内部草稿",
        "source_name": "internal-draft.md",
        "source": (
            "这是一份内部草稿，只说明服务将在下周评估。\n文档没有给出上线日期、负责人或性能指标。"
        ),
        "index": "",
    },
    {
        "id": "single-config-line",
        "name": "孤立配置行",
        "source_name": "config-note.md",
        "source": "timeout=30，retries=2。",
        "index": "",
    },
    {
        "id": "concrete-technical-note",
        "name": "完整技术短文",
        "source_name": "asyncio-note.md",
        "source": (
            "Python asyncio 使用 event loop 调度 coroutine。\n"
            "await 会暂停当前 coroutine，直到等待的异步结果可用。"
        ),
        "index": "",
    },
    {
        "id": "duplicate-existing-page",
        "name": "已有页面重复内容",
        "source_name": "existing-note.md",
        "source": "Python asyncio 使用 event loop 调度 coroutine。",
        "index": "- [[concepts/asyncio]] — [concept] concepts/asyncio.md — asyncio\n",
        "existing_page": (
            "---\n"
            "type: concept\n"
            "title: asyncio\n"
            "summary: Python 异步调度\n"
            "goal: 说明 asyncio 基础机制\n"
            "related: []\n"
            "---\n"
            "# asyncio\n\n"
            "Python asyncio 使用 event loop 调度 coroutine。\n"
        ),
    },
]


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.live
@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_real_llm_pipeline_reports_plan_and_files(case):
    """分别观察不同信息密度/重复关系源文档的真实 pipeline 行为。"""
    if os.getenv("RUN_LIVE_LLM_TESTS") != "1":
        pytest.skip("设置 RUN_LIVE_LLM_TESTS=1 才运行真实 LLM 测试")

    async def run():
        cfg = load_config(project_root=ROOT)
        llm = create_llm(cfg.llm)

        with tempfile.TemporaryDirectory(prefix="wiki-agent-live-") as raw_tmp:
            wiki = Path(raw_tmp) / "wiki"
            wiki.mkdir()
            (wiki / "index.md").write_text(case["index"], encoding="utf-8")
            if case.get("existing_page"):
                page = wiki / "concepts" / "asyncio.md"
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text(case["existing_page"], encoding="utf-8")

            source = SourceDocument(
                name=case["source_name"],
                ext="md",
                path=case["source_name"],
                chunks=[
                    SourceChunk(
                        content=case["source"],
                        index=0,
                        total=1,
                        source_name=case["source_name"],
                    )
                ],
            )
            extractor = Extractor(
                llm,
                wiki_dir=wiki,
                save_source_page=True,
                max_concurrency=1,
            )
            extract = await extractor.extract(source)

            integrator = compile_integrator(llm, wiki_dir=wiki)
            search = await integrator.search(extract, case["index"])
            analysis = await integrator.analyze(extract, search)
            plan = await integrator.plan(
                extract,
                analysis,
                index_content=case["index"],
                schema="",
                purpose="",
            )
            written = await integrator.execute(plan, extract)

            knowledge_files = sorted(
                str(path.relative_to(wiki))
                for directory in ("concepts", "entities", "topics")
                for path in (wiki / directory).glob("**/*.md")
                if path.is_file()
            )
            source_files = (
                sorted(
                    str(path.relative_to(wiki))
                    for path in (wiki / "sources").glob("**/*.md")
                    if path.is_file()
                )
                if (wiki / "sources").exists()
                else []
            )

            print(f"\n=== Real pipeline: {case['name']} ===")
            print(f"SOURCE:\n{case['source']}")
            print(f"EXTRACT SUMMARY:\n{extract.document_summary}")
            print(f"SEARCH CANDIDATES: {search.rel_paths}")
            print(f"PLAN TARGETS: {[t.wiki_path for t in plan.page_targets]}")
            print(f"EXECUTE WRITTEN: {[t.wiki_path for t in written]}")
            print(f"SOURCE FILES: {source_files}")
            print(f"KNOWLEDGE FILES: {knowledge_files}")

    _run(run())
