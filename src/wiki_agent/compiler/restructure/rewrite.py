"""逐页成文：以分配装配的草稿为材料，LLM 重写成连贯页面。

prompt 约束模型只使用给定材料，不引入新事实；输出形状（frontmatter
必填字段、fence 闭合）由 check 重试层校验，normalize、骨架补全与写盘
由调用方（handler）负责。
"""

from __future__ import annotations

from typing import Any

from wiki_agent.compiler.checked_call import invoke_checked
from wiki_agent.compiler.models import NO_THINKING
from wiki_agent.conversation import Message

from . import prompts
from .models import RewriteError


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
    # 重试后仍不过即抛 RewriteError 计入单元失败，保留现场（哪个页、
    # 缺什么），不让残缺输出静默落盘
    response = await invoke_checked(
        llm,
        action="成文",
        error=RewriteError,
        messages=[
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
        # 容错：剥掉围栏，规则与 integration/parse 一致
        lines = body.splitlines()
        body = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return body
