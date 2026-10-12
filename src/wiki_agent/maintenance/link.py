"""出链维护（link）：LLM 出替换清单，代码校验后逐条应用。

link 只处理连通性，不改写正文。每个替换项是对页面原文的一次字面替换
（find → replace），校验不过的项逐条跳过并附理由：find 无法在正文唯一
定位、目标 slug 不在内容页列表、自链、缺 | 显示文字、骨架变化（除增删/
改链外动了文字）、与已接受修改位置重叠。校验全部判在原始正文上、替换从
后向前落刀，前序替换不会污染后序判定。空清单表示无可补链，不写盘。
清单生成失败（重试后校验仍不过）抛 IngestError，与 compile 各阶段一致。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from wiki_agent.conversation import Message
from wiki_agent.errors import IngestStage
from wiki_agent.llm.checked import invoke_checked
from wiki_agent.llm.params import JSON_MODE, NO_THINKING
from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.pages import all_content_slugs, path_for

LINK_PLAN_SYSTEM = (
    "You are the wiki's outbound-link maintainer. Given one page's body and the"
    " roster of linkable pages, point out where [[wikilinks]] should be added,"
    " changed or removed: add = the text names a roster page but carries no"
    " link; change = an existing link no longer fits; remove = a link hurts"
    " reading. Every link you write must be [[slug|display text]] — a bare"
    " [[slug]] without | display text is forbidden. Each fix is one literal"
    " replacement on the body: find must occur exactly once in the body (when"
    " a term repeats, widen it with adjacent context until unique), and replace"
    " must be character-identical to find except for adding/removing/repointing"
    " links — reworded text is rejected by validation. Output an empty array"
    ' when there is nothing to fix. Output JSON only: {"fixes":[{"find":"...",'
    '"replace":"..."}]}. Keep all body text in the page\'s original language;'
    " never translate."
)


def _plan_user(slug: str, body: str, slugs: list[str]) -> str:
    roster = "\n".join(f"- {s}" for s in slugs if s != slug)
    return (
        f"## Page\n{slug}\n\n## Linkable roster\n{roster}\n\n"
        f"## Body\n{body[:30000]}\n\nProduce the replacement list."
    )


def _check_plan_json(content: str) -> tuple[bool, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    fixes = data.get("fixes") if isinstance(data, dict) else None
    if not isinstance(fixes, list):
        return False, 'missing fixes array — output {"fixes": [...]}.'
    for i, item in enumerate(fixes):
        if not isinstance(item, dict) or not item.get("find") or not isinstance(item.get("replace"), str):
            return False, f"fixes[{i}] needs find and a non-empty replace string"
    return True, ""


async def plan_link_fixes(llm: Any, wiki_dir: str | Path, slug: str) -> list[dict[str, str]]:
    """返回替换清单（可为空=无可补）；清单生成失败抛 IngestError。"""
    wiki_dir = Path(wiki_dir)
    path = path_for(wiki_dir, slug)
    if not path.is_file():
        return []
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    response = await invoke_checked(
        llm,
        stage=IngestStage.PLAN,
        action="link 清单",
        source=f"link:{slug}",
        retry_policy="manual",
        messages=[
            Message(role="system", content=LINK_PLAN_SYSTEM),
            Message(role="user", content=_plan_user(slug, body, all_content_slugs(wiki_dir))),
        ],
        max_tokens=4096,
        check=_check_plan_json,
        extra_body=NO_THINKING,
        max_attempts=2,
        response_format=JSON_MODE,
    )
    data = json.loads(response.content)
    fixes = data.get("fixes")
    return [
        {"find": str(f["find"]), "replace": str(f["replace"])}
        for f in fixes
        if isinstance(f, dict)
    ]


_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")


def _link_skeleton(text: str) -> str:
    """剥掉链接外壳的正文骨架：[[slug|显示]]→显示、[[slug]]→slug。"""
    return _LINK_RE.sub(lambda m: m.group(2) or m.group(1), text)


def apply_link_fixes(
    body: str,
    replacements: list[dict[str, str]],
    *,
    valid_slugs: set[str],
    self_slug: str,
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """两遍式应用：全部 fix 先在原始正文上定位与校验，再从后向前落刀。

    返回 (新正文, 已应用, 跳过[含理由])。校验判在原始正文上，前序替换
    不影响后序判定；replace 里仍留着 find 文本，逐条在演进文本上匹配
    会让同处二改绕过唯一性检查、产出嵌套链接——区间重叠检查从结构上
    封死。骨架不变量兑现"只处理连通性"：加链、去链、换目标合法，
    夹带改写正文一律条目级跳过。

    入参与替换范围都是不含 frontmatter 的正文——与 prompt 的"正文中恰好
    出现一次"契约和 plan 侧喂给模型的取材一致；作用全文会让 find 撞上
    元数据里的同字面串，把 wikilink 写进 frontmatter。
    """
    applied: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    planned: list[tuple[int, int, str]] = []
    for fix in replacements:
        find, replace = fix.get("find", ""), fix.get("replace", "")
        if not find or body.count(find) != 1:
            skipped.append({**fix, "why": "find 无法在正文唯一定位（需带上下文，缺失或重复）"})
            continue
        matches = list(_LINK_RE.finditer(replace))
        targets = {m.group(1) for m in matches}
        if any(t == self_slug for t in targets):
            skipped.append({**fix, "why": "自链"})
            continue
        if any(t not in valid_slugs for t in targets):
            skipped.append({**fix, "why": "目标 slug 不在名册"})
            continue
        if any(not m.group(2) for m in matches):
            # 质量规则要求 [[slug|显示文字]]；条目级跳过，
            # 避免一条不合规导致整页修改被规范化检查撤销
            skipped.append({**fix, "why": "链接缺 | 显示文字"})
            continue
        if _link_skeleton(find) != _link_skeleton(replace):
            skipped.append({**fix, "why": "除增删/改链外改动了正文文字"})
            continue
        start = body.find(find)
        end = start + len(find)
        if any(prev_start < end and start < prev_end for prev_start, prev_end, _ in planned):
            skipped.append({**fix, "why": "与已接受的修改位置重叠"})
            continue
        planned.append((start, end, replace))
        applied.append(fix)
    new_body = body
    for start, end, replace in sorted(planned, reverse=True):  # 从后向前，先前落刀不移动后文坐标
        new_body = new_body[:start] + replace + new_body[end:]
    return new_body, applied, skipped
