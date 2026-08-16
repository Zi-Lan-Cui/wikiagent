"""stages 测试——Executor 页级失败隔离 + 死链兜底。

直接运行:  .venv/bin/python test/test_stages.py
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.compiler.models import (
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
)
from wiki_agent.compiler.stages import Executor
from wiki_agent.compiler.prompts import compile as cp


class _FailLLM:
    """页面生成必失败的 LLM——触发失败隔离路径。"""
    model_id = "mock"

    async def async_invoke(self, messages, tools=None, max_tokens=None,
                           temperature=0.5, extra_body=None):
        from wiki_agent.message import LLMResponse
        return LLMResponse(content="---\n# 只有标题\n", finish_reason="stop")


def _make_env():
    tmp = Path(tempfile.mkdtemp())
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "index.md").write_text(
        "- [[concepts/existing]] — [concept] concepts/existing.md — 已有\n",
        encoding="utf-8",
    )
    (wiki / "concepts" / "existing.md").write_text(
        "---\ntype: concept\ntitle: \"Existing\"\nsummary: \"s\"\n"
        "goal: \"g\"\nrelated: []\n---\n# Existing\n\n已有正文。\n",
        encoding="utf-8",
    )
    return tmp, wiki


def test_executor_page_failure_isolated():
    """单页生成失败不炸批——失败页返回 None，成功页正常落盘。"""
    async def run():
        tmp, wiki = _make_env()
        executor = Executor(_FailLLM(), wiki, cp)
        extract = ExtractResult(source_identity="test.md",
                                document_summary="文档摘要")
        plan = IntegrationPlan(page_targets=[
            PageTarget("concepts/newpage.md", "New", Disposition.NEW, "原因"),
        ])
        results = await executor.execute(plan, extract)
        # 失败隔离: 结果不包含失败页（None 被过滤）
        assert results == []
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
