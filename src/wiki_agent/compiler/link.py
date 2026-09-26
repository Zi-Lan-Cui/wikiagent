"""出链维护（link）：LLM 出替换清单，代码校验后逐条应用。

link 只做连通性：不重写内容、不动正文文字本身。每个替换项是对页面
原文的一次字面替换（find → replace），代码校验三条：find 在正文中
恰好出现一次、替换后的目标 slug 存在、不自链。校验不过的项跳过并计数，
全部无效与"无可补"都走空操作。清单生成失败（LLM 校验穷尽）返回 None，
调用方按任务失败处理。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from wiki_agent.compiler.content_pages import all_content_slugs
from wiki_agent.compiler.models import JSON_MODE, NO_THINKING
from wiki_agent.conversation import Message
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.wiki.frontmatter import split_frontmatter

LINK_PLAN_SYSTEM = (
    "你是 wiki 出链维护者。给定一页正文与可链接页面名册，指出正文里哪里该加、该改、该去 wikilink："
    "该加=文字实指名册某页却没有链接；该改=链接指向已不合适；该去=链接干扰阅读。"
    "每个修改是对原文的一次字面替换：find 必须是正文中恰好出现一次的原文片段，"
    "replace 是替换后的文本（去链时给纯文本）。不给修改就输出空数组。"
    '只输出 JSON：{"fixes":[{"find":"...","replace":"..."}]}'
)


def _plan_user(slug: str, body: str, slugs: list[str]) -> str:
    roster = "\n".join(f"- {s}" for s in slugs if s != slug)
    return (
        f"## 页面\n{slug}\n\n## 可链接名册\n{roster}\n\n"
        f"## 正文\n{body[:30000]}\n\n给出替换清单。"
    )


def _check_plan_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"不是合法 JSON: {exc}"
    fixes = data.get("fixes") if isinstance(data, dict) else None
    if not isinstance(fixes, list):
        return False, '缺少 fixes 数组。输出 {{"fixes": [...]}}。'
    for i, item in enumerate(fixes):
        if not isinstance(item, dict) or not item.get("find") or not isinstance(item.get("replace"), str):
            return False, f"fixes[{i}] 需要 find 与非空 replace 字符串。"
    return True, ""


async def plan_link_fixes(llm: Any, wiki_dir: str | Path, slug: str) -> list[dict[str, str]] | None:
    """返回替换清单（可为空=无可补）；None=清单生成失败（调用方按失败处理）。"""
    wiki_dir = Path(wiki_dir)
    path = wiki_dir / f"{slug}.md"
    if not path.is_file():
        return []
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    response = await async_invoke_with_retry(
        llm,
        [
            Message(role="system", content=LINK_PLAN_SYSTEM),
            Message(role="user", content=_plan_user(slug, body, all_content_slugs(wiki_dir))),
        ],
        max_tokens=4096,
        check=_check_plan_json,
        extra_body=NO_THINKING,
        max_attempts=2,
        response_format=JSON_MODE,
    )
    if not response.check_ok:
        return None
    data = json.loads(response.content)
    fixes = data.get("fixes")
    return [
        {"find": str(f["find"]), "replace": str(f["replace"])}
        for f in fixes
        if isinstance(f, dict)
    ]


_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")


def apply_link_fixes(
    content: str,
    replacements: list[dict[str, str]],
    *,
    valid_slugs: set[str],
    self_slug: str,
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """逐条应用：校验通过就地替换。返回 (新内容, 已应用, 跳过[含理由])。"""
    applied: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    working = content
    for fix in replacements:
        find, replace = fix.get("find", ""), fix.get("replace", "")
        if not find or find not in working or working.count(find) > 1:
            skipped.append({**fix, "why": "find 非唯一出现或缺失"})
            continue
        targets = {m.group(1) for m in _LINK_RE.finditer(replace)}
        if any(t == self_slug for t in targets):
            skipped.append({**fix, "why": "自链"})
            continue
        if any(t not in valid_slugs for t in targets):
            skipped.append({**fix, "why": "目标 slug 不在名册"})
            continue
        working = working.replace(find, replace, 1)
        applied.append(fix)
    return working, applied, skipped
