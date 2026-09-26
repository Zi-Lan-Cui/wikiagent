"""二次确认：逐单元判"这个重组此刻成不成立"（LLM，一组一次）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki_agent.compiler.models import JSON_MODE
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import get_logger

from . import prompts
from .models import Unit
from .propose import outline_meta

logger = get_logger("RESTRUCTURE")


async def recheck_units(
    llm: Any, wiki_dir: str | Path, units: list[Unit]
) -> tuple[list[Unit], list[tuple[Unit, str]]]:
    """返回 (成立单元, [(放弃单元, 理由)])。判定失败按放弃处理——存疑不动结构。"""
    wiki_dir = Path(wiki_dir)
    confirmed: list[Unit] = []
    rejected: list[tuple[Unit, str]] = []
    touched = sorted({s for u in units for s in u.in_pages} | {p.slug for u in units for p in u.out})
    meta = outline_meta(wiki_dir, touched)
    for unit in units:
        response = await async_invoke_with_retry(
            llm,
            [
                Message(role="system", content=prompts.RECHECK_SYSTEM),
                Message(role="user", content=prompts.recheck_user(unit, prompts.outline(unit.in_pages + unit.out_slugs, meta))),
            ],
            # 复核开思考：思考段计入 max_tokens，1024 会被吃光致输出为空
            max_tokens=4096,
            check=prompts.check_recheck_json,
            max_attempts=2,
            response_format=JSON_MODE,
        )
        if not response.check_ok:
            rejected.append((unit, f"复核校验失败: {response.check_reason}"))
            continue
        data = prompts.json_of(response.content)
        if data["keep"]:
            confirmed.append(unit)
        else:
            rejected.append((unit, str(data.get("reason") or "复核放弃")))
    return confirmed, rejected
