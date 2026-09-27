"""二次确认：逐单元判"这个重组此刻成不成立"（LLM，一组一次）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki_agent.compiler.models import JSON_MODE
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import get_logger
from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.sections import page_sections

from . import prompts
from .models import Unit
from .propose import read_wiki_const

logger = get_logger("RESTRUCTURE")


async def recheck_units(
    llm: Any, wiki_dir: str | Path, units: list[Unit]
) -> tuple[list[Unit], list[tuple[Unit, str]]]:
    """返回 (成立单元, [(放弃单元, 理由)])。判定失败按放弃处理——存疑不动结构。"""
    wiki_dir = Path(wiki_dir)
    confirmed: list[Unit] = []
    rejected: list[tuple[Unit, str]] = []

    def section_entries(slugs: list[str]) -> list[tuple[str, str, list[str]]]:
        entries = []
        for slug in dict.fromkeys(slugs):
            path = wiki_dir / f"{slug}.md"
            if not path.is_file():
                continue
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            entries.append((
                slug,
                str(fm.get("title") or ""),
                [sec.heading for sec in page_sections(path, slug) if sec.heading],
            ))
        return entries
    for unit in units:
        response = await async_invoke_with_retry(
            llm,
            [
                Message(role="system", content=prompts.RECHECK_SYSTEM),
                Message(
                    role="user",
                    content=prompts.recheck_user(
                        unit,
                        prompts.section_outline(section_entries(unit.in_pages + unit.out_slugs)),
                        read_wiki_const(wiki_dir, "schema.md"),
                    ),
                ),
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
