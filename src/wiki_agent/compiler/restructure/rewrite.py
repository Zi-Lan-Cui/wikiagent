"""逐页成文：以分配装配的草稿为材料，LLM 重写成连贯页面。

不整页无中生有——prompt 约束只用给定材料；输出形状（frontmatter 必填字段、
fence 闭合）进 check 重试层，调用方（handler）负责 normalize、骨架兜底与写盘。
"""

from __future__ import annotations

from typing import Any

from wiki_agent.compiler.models import NO_THINKING
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry

from . import prompts


async def rewrite_unit_page(
    llm: Any,
    *,
    slug: str,
    intent: str,
    draft: str,
    old: str,
    siblings: list[str],
) -> str:
    """返回重写后的页面全文（frontmatter 以旧版/草稿为基线，由调用方 normalize）。"""
    response = await async_invoke_with_retry(
        llm,
        [
            Message(role="system", content=prompts.REWRITE_SYSTEM),
            Message(role="user", content=prompts.rewrite_user(slug, intent, draft, old, siblings)),
        ],
        max_tokens=8192,
        check=prompts.check_rewrite_page,
        extra_body=NO_THINKING,
        max_attempts=2,
    )
    body = response.content.strip()
    if body.startswith("```"):
        # 容错：剥掉围栏（与 integration/parse 同规则，但这里只有一处）
        lines = body.splitlines()
        body = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return body
