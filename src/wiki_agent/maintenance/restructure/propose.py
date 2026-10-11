"""提议：全库大纲 → 重组单元清单（LLM）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki_agent.compiler.checked_call import invoke_checked
from wiki_agent.compiler.models import JSON_MODE
from wiki_agent.conversation import Message
from wiki_agent.errors import IngestStage
from wiki_agent.log import get_logger
from wiki_agent.wiki.pages import all_content_slugs

from . import prompts
from .models import OutPage, Unit

logger = get_logger("RESTRUCTURE")


def read_wiki_const(wiki_dir: Path, name: str) -> str:
    """读 wiki 根下的系统文件（schema.md/purpose.md）；缺失返回空串。"""
    try:
        return (wiki_dir / name).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


async def propose_units(llm: Any, wiki_dir: str | Path) -> list[Unit]:
    """全库结构分析一次，产出通过形状与 slug 校验的单元清单。

    本阶段不关闭 thinking：NO_THINKING 下模型对明显重合的页面簇
    也倾向提空单元。

    Raises:
        IngestError: 输出校验重试后仍失败。
    """
    wiki_dir = Path(wiki_dir)
    index_path = wiki_dir / "index.md"
    index = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    slugs = all_content_slugs(wiki_dir)
    if not slugs:
        return []
    response = await invoke_checked(
        llm,
        stage=IngestStage.PLAN,
        action="重组粗提",
        source="restructure_propose",
        retry_policy="manual",
        messages=[
            Message(role="system", content=prompts.PROPOSE_SYSTEM),
            Message(
                role="user",
                content=prompts.propose_user(
                    index,
                    read_wiki_const(wiki_dir, "schema.md"),
                    read_wiki_const(wiki_dir, "purpose.md"),
                ),
            ),
        ],
        max_tokens=8192,
        check=prompts.check_propose_json,
        max_attempts=2,
        response_format=JSON_MODE,
    )
    known = set(slugs)
    units: list[Unit] = []
    for raw in prompts.json_of(response.content)["units"]:
        in_pages = [str(s) for s in raw["in_pages"]]
        # 畸形 out 条目整单元丢弃：静默过滤会把"合并"悄悄变成 out=[] 的删除声明，
        # 真删除是模型显式给空清单，不该由解析失败冒充
        malformed = [
            p for p in raw.get("out") or [] if not isinstance(p, dict) or not str(p.get("slug") or "")
        ]
        if malformed:
            logger.info("  粗提含损坏的输出页声明，丢弃单元: %s", malformed[:3])
            continue
        out = [OutPage.from_dict(p) for p in raw.get("out") or []]
        unknown = [s for s in in_pages if s not in known]
        if unknown:
            logger.info("  粗提含未知页，丢弃单元: %s", unknown)
            continue
        units.append(Unit(in_pages=in_pages, out=out, reason=str(raw.get("reason") or "")))
    return units
