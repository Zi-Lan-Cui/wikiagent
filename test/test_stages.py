"""stages 测试——Executor 页级失败隔离 + 死链兜底。

直接运行:  .venv/bin/python test/test_stages.py
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.compiler.integration.execute import Executor
from wiki_agent.compiler.integration.search import _filter_search_paths
from wiki_agent.compiler.models import (
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
)
from wiki_agent.compiler.prompts import compile as cp
from wiki_agent.errors import IngestError


class _FailLLM:
    """页面生成必失败的 LLM——触发失败隔离路径。"""

    model_id = "mock"

    async def async_invoke(
        self, messages, tools=None, max_tokens=None, temperature=0.5, extra_body=None
    ):
        from wiki_agent.message import LLMResponse

        return LLMResponse(content="---\n# 只有标题\n", finish_reason="stop")


class _GoodLLM:
    """页面生成合格的 LLM——用于验证 plan target 会继续落盘。"""

    model_id = "mock"

    async def async_invoke(
        self, messages, tools=None, max_tokens=None, temperature=0.5, extra_body=None
    ):
        from wiki_agent.message import LLMResponse

        return LLMResponse(
            content=(
                "---\n"
                "type: concept\n"
                "title: New\n"
                "summary: 摘要\n"
                "goal: 解释主题\n"
                "---\n"
                "# New\n\n正文内容。\n"
            ),
            finish_reason="stop",
        )


def _make_env():
    tmp = Path(tempfile.mkdtemp())
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "index.md").write_text(
        "- [[concepts/existing]] — [concept] concepts/existing.md — 已有\n",
        encoding="utf-8",
    )
    (wiki / "concepts" / "existing.md").write_text(
        '---\ntype: concept\ntitle: "Existing"\nsummary: "s"\n'
        'goal: "g"\nrelated: []\n---\n# Existing\n\n已有正文。\n',
        encoding="utf-8",
    )
    return tmp, wiki


def test_search_postprocess_filters_ghost_and_invalid_paths(tmp_path: Path):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "known.md").write_text("# known\n", encoding="utf-8")

    valid, invalid = _filter_search_paths(
        [
            "concepts/known.md",
            "concepts/missing.md",
            "sources/raw.md",
            "../outside.md",
        ],
        wiki,
    )

    assert valid == ["concepts/known.md"]
    assert invalid == [
        "concepts/missing.md",
        "sources/raw.md",
        "../outside.md",
    ]


def test_executor_page_failure_isolated():
    """单页生成失败升级为 source 级错误，失败页不落盘。"""

    async def run():
        tmp, wiki = _make_env()
        executor = Executor(_FailLLM(), wiki, cp)
        extract = ExtractResult(source_identity="test.md", document_summary="文档摘要")
        plan = IntegrationPlan(
            page_targets=[
                PageTarget("concepts/newpage.md", "New", Disposition.NEW, "原因"),
            ]
        )
        try:
            await executor.execute(plan, extract)
        except IngestError as error:
            assert error.stage.value == "execute"
            assert "newpage.md" in error.raw
        else:
            raise AssertionError("页面失败必须升级为 IngestError")
        # 失败页未落盘
        assert not (wiki / "concepts" / "newpage.md").exists()

    asyncio.run(run())


def test_executor_empty_plan_returns_empty():
    async def run():
        tmp, wiki = _make_env()
        executor = Executor(_FailLLM(), wiki, cp)
        extract = ExtractResult(source_identity="t", document_summary="d")
        results = await executor.execute(IntegrationPlan(page_targets=[]), extract)
        assert results == []

    asyncio.run(run())


def test_low_quality_extract_does_not_block_plan_target():
    """当前没有 extract 质量闸门：plan 已决定创建时仍会落盘。"""

    async def run():
        tmp, wiki = _make_env()
        executor = Executor(_GoodLLM(), wiki, cp)
        extract = ExtractResult(
            source_identity="bad-summary.md",
            document_summary="无关且质量很低的摘要",
        )
        plan = IntegrationPlan(
            page_targets=[
                PageTarget("concepts/newpage.md", "New", Disposition.NEW, "plan 决定创建"),
            ]
        )

        results = await executor.execute(plan, extract)

        assert [t.wiki_path for t in results] == ["concepts/newpage.md"]
        assert (wiki / "concepts" / "newpage.md").exists()

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
