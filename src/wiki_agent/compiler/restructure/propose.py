"""粗提：全库大纲 → 重组单元清单（LLM）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki_agent.compiler.content_pages import all_content_slugs
from wiki_agent.compiler.models import JSON_MODE
from wiki_agent.conversation import Message
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import get_logger
from wiki_agent.wiki.frontmatter import split_frontmatter

from . import prompts
from .models import OutPage, Unit

logger = get_logger("RESTRUCTURE")


def outline_meta(wiki_dir: Path, slugs: list[str]) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for slug in slugs:
        path = wiki_dir / f"{slug}.md"
        if not path.is_file():
            continue
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        meta[slug] = {
            "title": str(fm.get("title") or ""),
            "goal": str(fm.get("goal") or ""),
            "summary": str(fm.get("summary") or ""),
        }
    return meta


async def propose_units(llm: Any, wiki_dir: str | Path) -> list[Unit]:
    """跑一遍全库结构分析，产出通过形状与 slug 校验的单元清单。

    开思考：真实模型在 NO_THINKING 下对明显重合的页面簇也提空单元。
    """
    wiki_dir = Path(wiki_dir)
    index_path = wiki_dir / "index.md"
    index = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    slugs = all_content_slugs(wiki_dir)
    if not slugs:
        return []
    response = await async_invoke_with_retry(
        llm,
        [
            Message(role="system", content=prompts.PROPOSE_SYSTEM),
            Message(role="user", content=prompts.propose_user(index, prompts.outline(slugs, outline_meta(wiki_dir, slugs)))),
        ],
        max_tokens=8192,
        check=prompts.check_propose_json,
        max_attempts=2,
        response_format=JSON_MODE,
    )
    if not response.check_ok:
        raise IngestError(
            IngestStage.PLAN,
            f"重组粗提校验失败: {response.check_reason}",
            source="restructure_propose",
            raw=response.content,
            error_code="output_validation",
            error_class="transient",
            retry_policy="manual",
        )
    known = set(slugs)
    units: list[Unit] = []
    for raw in prompts.json_of(response.content)["units"]:
        in_pages = [str(s) for s in raw["in_pages"]]
        out = [
            OutPage.from_dict(p)
            for p in raw["out"]
            if isinstance(p, dict) and str(p.get("slug") or "")
        ]
        unknown = [s for s in in_pages if s not in known]
        if unknown:
            logger.info("  粗提含未知页，丢弃单元: %s", unknown)
            continue
        units.append(Unit(in_pages=in_pages, out=out, reason=str(raw.get("reason") or "")))
    return units
