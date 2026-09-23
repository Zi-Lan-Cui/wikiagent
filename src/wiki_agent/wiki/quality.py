"""质检模块——全库体检 + 报告（审计路径）。

- 页面级: 定稿兜底检查、死链检查
- 全库级: 编译结束后的整体扫描与报告格式化

判定与生成时闸门共用同一份定义（检测原子在 rules）——
同一现象，生成时 retry 修正、落盘后 scan 报告，一份判定两种语境。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from wiki_agent.log import emit_event, get_logger
from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.rules import (
    WIKILINK_RE,
    body_without_title,
    check_page_body,
    check_page_frontmatter,
    check_page_output,
    extract_body,
    iter_text_outside_code,
)

logger = get_logger("QUALITY")

# 正文少于该字符数视为"只有一句话"的可疑页面
_MIN_BODY_CHARS = 80
_MIN_TITLE_CHARS = 2
_MIN_META_CHARS = 4
_TYPE_DIRS = {
    "concept": "concepts",
    "entity": "entities",
    "topic": "topics",
    "source": "sources",
}
_PLACEHOLDER_VALUES = {
    "todo",
    "tbd",
    "n/a",
    "na",
    "unknown",
    "placeholder",
    "待补充",
    "暂无",
    "未填写",
    "占位",
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 内容页面所在的子目录（scan_wiki 扫描范围）
CONTENT_DIRS = ("concepts", "entities", "topics")


@dataclass
class Issue:
    """一条检测结果。"""

    level: str  # "error" / "warning"
    path: str  # 页面相对路径
    message: str

    def __str__(self) -> str:
        icon = "✗" if self.level == "error" else "⚠"
        return f"  {icon} [{self.level.upper()}] {self.path}: {self.message}"


# 页面级检测——判定复用 checks 的 check 回调，这里只组装 Issue


def _semantic_frontmatter_issues(
    content: str,
    *,
    path: str,
    valid_slugs: set[str] | None = None,
) -> list[Issue]:
    """检查 frontmatter 语义；结构性错误与建议分开报告。"""
    fm, body = split_frontmatter(content)
    if not fm:
        return []  # 必填/格式错误由 check_page_output 统一报告

    issues: list[Issue] = []
    rel_dir = Path(path).parts[0] if Path(path).parts else ""
    page_type = str(fm.get("type", "")).strip().lower()
    if page_type not in _TYPE_DIRS:
        issues.append(
            Issue(
                "error", path, f"type 非法: {page_type or '为空'}；允许 concept/entity/topic/source"
            )
        )
    elif rel_dir != _TYPE_DIRS[page_type]:
        issues.append(
            Issue(
                "error",
                path,
                f"type/目录不一致: type={page_type} 应位于 {_TYPE_DIRS[page_type]}/",
            )
        )

    created = str(fm.get("created", "")).strip()
    updated = str(fm.get("updated", "")).strip()
    for field, value in (("created", created), ("updated", updated)):
        if value and not _DATE_RE.fullmatch(value):
            issues.append(Issue("error", path, f"{field} 日期格式非法: {value}（应为 YYYY-MM-DD）"))
    if created and updated and _DATE_RE.fullmatch(created) and _DATE_RE.fullmatch(updated):
        if updated < created:
            issues.append(Issue("error", path, "updated 早于 created"))

    title = str(fm.get("title", "")).strip()
    summary = str(fm.get("summary", "")).strip()
    goal = str(fm.get("goal", "")).strip()
    for field, value, minimum in (
        ("title", title, _MIN_TITLE_CHARS),
        ("summary", summary, _MIN_META_CHARS),
        ("goal", goal, _MIN_META_CHARS),
    ):
        if len(value) < minimum:
            issues.append(Issue("warning", path, f"{field} 过短（{len(value)} 字符）"))
        if value.lower() in _PLACEHOLDER_VALUES:
            issues.append(Issue("warning", path, f"{field} 使用占位文本: {value}"))

    h1 = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    if h1 and title and h1.group(1).strip() != title:
        issues.append(
            Issue("warning", path, f"title 与首个 H1 不一致: {title!r} / {h1.group(1).strip()!r}")
        )

    # sources 档案页按设计不参与 related 语义检查。
    if page_type != "source" and "related" in fm:
        raw_related = str(fm.get("related", "")).strip()
        try:
            related = json.loads(raw_related)
        except json.JSONDecodeError:
            related = None
        if not isinstance(related, list):
            issues.append(Issue("error", path, "related 必须是数组"))
        else:
            slugs: list[str] = []
            for item in related:
                if not isinstance(item, str) or not item.strip():
                    issues.append(Issue("error", path, "related 数组元素必须是非空字符串"))
                    continue
                slug = item.strip().replace("[[", "").replace("]]", "").replace(".md", "")
                slugs.append(slug)
            if len(slugs) != len(set(slugs)):
                issues.append(Issue("warning", path, "related 存在重复 slug"))
            if valid_slugs is not None:
                for slug in sorted(set(slugs) - valid_slugs):
                    issues.append(Issue("error", path, f"related 指向不存在页面: {slug}"))
    return issues


def check_page_quality(
    content: str,
    *,
    path: str,
    valid_slugs: set[str] | None = None,
) -> list[Issue]:
    """单页质量检测——判定与生成闸门（check_page_output）完全同源。

    Args:
        content: 页面内容
        path: 页面相对路径（如 concepts/lambda.md，用于报错定位）

    闸门不过 → Issue(error)（frontmatter 必填 goal 在内 / 正文存在 /
    wikilink 格式 / fence 闭合）；闸门过了 → 补体检独有的 warning
    观察（正文过短）。

    Returns:
        Issue 列表（error/warning）。
    """
    issues: list[Issue] = []

    if not content or not content.strip():
        return [Issue("error", path, "内容为空")]

    # 判定唯一来源: 生成闸门。落盘后不过 = 页面损伤（error 报告）。
    ok, reason = check_page_output(content)
    if not ok:
        issues.append(Issue("error", path, reason))
        return issues

    issues.extend(
        _semantic_frontmatter_issues(
            content,
            path=path,
            valid_slugs=valid_slugs,
        )
    )

    # 闸门是二元判定，不查长度——体检独有的 warning 观察在此补充
    if len(body_without_title(content)) < _MIN_BODY_CHARS:
        issues.append(
            Issue(
                "warning",
                path,
                f"正文过短（{len(body_without_title(content))} 字符），可能是提取失败的一话页",
            )
        )

    return issues


def check_source_output(content: str, *, path: str) -> list[Issue]:
    """检查单个 ``sources/`` 档案页。

    source 页保存的是原始材料的摘要/保真档案，不是知识页；正文中的
    ``[[...]]`` 可能是原文语法（例如 Python 列表），不能套用知识页的
    wikilink 闸门。仍保留内容非空、frontmatter 和代码块闭合检查。
    """
    issues: list[Issue] = []
    if not content or not content.strip():
        return [Issue("error", path, "内容为空")]
    ok, reason = check_page_frontmatter(content)
    if not ok:
        return [Issue("error", path, reason)]
    ok, reason = check_page_body(content)
    if not ok:
        return [Issue("error", path, reason)]
    issues.extend(_semantic_frontmatter_issues(content, path=path))
    if len(body_without_title(content)) < _MIN_BODY_CHARS:
        issues.append(
            Issue(
                "warning",
                path,
                f"正文过短（{len(body_without_title(content))} 字符），可能是提取失败的一话页",
            )
        )
    return issues


def scan_source(
    wiki_dir: str | Path,
    *,
    source_name: str,
    source_records_dir: str | Path | None = None,
    generated_paths: list[str] | None = None,
    source_page: tuple[str, str] | None = None,
) -> list[Issue]:
    """只检查一个 source 本轮产生的溯源存档和知识页。

    ``scan_wiki`` 负责批次收尾的全库关系检查；此方法用于 source 完成
    后的局部闸门，错误可以归属到 source 队列，不影响其他 source。

    ``source_page=(slug, content)`` 检查内存中的档案页——sync 的档案在
    成功结算前不落盘（scope 外写入统一由 outcome 在结算时执行），闸门
    检查的是"本轮构造的产出"而非磁盘；未传 source_page 才回退磁盘扫描。
    """
    wiki = Path(wiki_dir)
    issues: list[Issue] = []
    if source_page is not None:
        slug, content = source_page
        issues.extend(check_source_output(content, path=f"sources/{slug}.md"))
    else:
        source_dir = Path(source_records_dir) if source_records_dir is not None else None
        if source_dir is not None and source_dir.is_dir():
            for page in sorted(source_dir.glob("*.md")):
                try:
                    content = page.read_text(encoding="utf-8")
                except OSError:
                    continue
                if source_name not in content:
                    continue
                issues.extend(check_source_output(content, path=f"sources/{page.name}"))

    for rel in generated_paths or []:
        page = wiki / rel
        if not page.is_file():
            issues.append(Issue("error", rel, "生成页面不存在"))
            continue
        try:
            content = page.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(Issue("error", rel, f"读取失败: {exc}"))
            continue
        issues.extend(
            check_page_quality(
                content,
                path=rel,
                valid_slugs={
                    str(p.relative_to(wiki)).removesuffix(".md")
                    for sub in CONTENT_DIRS
                    for p in (wiki / sub).rglob("*.md")
                    if (wiki / sub).is_dir()
                },
            )
        )
    return issues


def check_dead_links(content: str, *, path: str, valid_slugs: set[str]) -> list[Issue]:
    """死链检测——正文中的 [[wikilink]] 指向不存在的页面。

    跳过代码块——代码里的 ``[[1, 2, 3]]`` 不是链接。

    Args:
        content: 页面内容。
        path: 页面相对路径。
        valid_slugs: 有效页面 slug 集合。

    Returns:
        死链 Issue 列表。
    """
    if not valid_slugs:
        return []

    issues: list[Issue] = []
    body = extract_body(content)
    text = "\n".join(iter_text_outside_code(body))
    for m in WIKILINK_RE.finditer(text):
        slug = m.group(1).strip().replace(".md", "")
        if slug not in valid_slugs:
            issues.append(
                Issue(
                    "warning",
                    path,
                    f"死链: [[{m.group(1)}]] 指向不存在的页面",
                )
            )
    return issues


# 全库体检（编译结束后调用）


def scan_wiki(wiki_dir: str | Path) -> list[Issue]:
    """全库扫描: 质量检测 + 死链检测。

    Args:
        wiki_dir: wiki 根目录

    Returns:
        全部 Issue（error + warning）。
    """
    wiki = Path(wiki_dir)
    all_issues: list[Issue] = []

    pages: list[Path] = []
    for sub in CONTENT_DIRS:
        d = wiki / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    valid_slugs = {str(page.relative_to(wiki)).replace(".md", "") for page in pages}

    # 第一遍: 收集 slug + 质量检测 + related 完整性 + 矛盾标注
    for page in pages:
        rel = str(page.relative_to(wiki))
        try:
            content = page.read_text(encoding="utf-8")
        except Exception as exc:
            all_issues.append(Issue("error", rel, f"读取失败: {exc}"))
            continue
        all_issues.extend(
            check_page_quality(
                content,
                path=rel,
                valid_slugs=valid_slugs,
            )
        )
        # related 由 normalize 定稿链注入——缺失/空说明页面没走完整流水线
        m = re.search(r"(?m)^\s*related\s*:\s*(.*)$", content)
        if not m:
            all_issues.append(
                Issue("warning", rel, "frontmatter 缺少 related 字段（未走 normalize 定稿链）")
            )
        elif m.group(1).strip() in ("", "[]"):
            all_issues.append(Issue("warning", rel, "related 为空——页面无交叉引用"))
        # 矛盾标注——update 阶段留下的 Disputed 块（contradicts 的落盘形态）。
        # 无自动消费端，裁决是人的事——报告出来让用户处置（原标记永远挂着）
        disputed_count = len(re.findall(r"(?m)^\s*>\s*\*\*Status:\s*Disputed\*\*", content))
        if disputed_count:
            all_issues.append(
                Issue(
                    "warning",
                    rel,
                    f"页面含 {disputed_count} 处 Disputed 矛盾标注——需人工裁决"
                    f"（版本A=已有表述 / 版本B=新表述）",
                )
            )

    # 孤岛检测：index 是导航入口，不算语义入链。
    incoming: dict[str, set[str]] = {slug: set() for slug in valid_slugs}
    for page in pages:
        rel = str(page.relative_to(wiki))
        try:
            content = page.read_text(encoding="utf-8")
        except OSError:
            continue
        current_slug = rel.removesuffix(".md")
        body = "\n".join(iter_text_outside_code(extract_body(content)))
        linked: set[str] = set()
        for match in WIKILINK_RE.finditer(body):
            linked.add(match.group(1).strip().replace(".md", ""))

        # related 是 frontmatter 中的结构化关系，也应计入入链；解析失败
        # 已由语义校验报告，这里只提取其中形如 [[slug]] 的值。
        fm, _ = split_frontmatter(content)
        raw_related = str(fm.get("related", ""))
        for match in WIKILINK_RE.finditer(raw_related):
            linked.add(match.group(1).strip().replace(".md", ""))
        for target in linked & valid_slugs:
            if target != current_slug:
                incoming[target].add(current_slug)

    for page in pages:
        rel = str(page.relative_to(wiki))
        slug = rel.removesuffix(".md")
        if not incoming[slug]:
            all_issues.append(
                Issue(
                    "warning",
                    rel,
                    "孤岛页面：没有其他内容页通过 wikilink 或 related 指向它",
                )
            )

    # 第二遍: 死链检测（需要完整 slug 集合）
    for page in pages:
        rel = str(page.relative_to(wiki))
        try:
            content = page.read_text(encoding="utf-8")
        except Exception:
            continue
        all_issues.extend(check_dead_links(content, path=rel, valid_slugs=valid_slugs))

    # 第三遍: 根目录垃圾文件 + index 幽灵条目
    index_path = wiki / "index.md"
    try:
        index_content = index_path.read_text(encoding="utf-8")
    except OSError:
        index_content = ""

    # 3a. 根目录 .md——内容页面应全在 CONTENT_DIRS 下，根目录的 .md
    #     只有 index/purpose/schema 等系统文件（垃圾页审计：wiki/.md）
    system_files = {"index.md", "purpose.md", "schema.md"}
    for f in sorted(wiki.glob("*.md")):
        if f.name in system_files:
            continue
        all_issues.append(
            Issue(
                "warning",
                f.name,
                "根目录多余 .md 文件——内容页应在 concepts/entities/topics 下",
            )
        )

    # 3b. 幽灵条目——index 有、磁盘无（search 会返回不存在的页面）
    for slug in re.findall(r"\[\[([^\]]+)\]\]", index_content):
        if not (wiki / f"{slug}.md").exists():
            all_issues.append(Issue("error", "index.md", f"幽灵条目: [[{slug}]] 指向不存在的页面"))

    # 3c. 非标准内容目录——LLM 路由违规产物（实测 languages/ tools/）。
    #     三个内容目录之外的 .md 子目录在扫描与 search 中完全隐形。
    known_dirs = set(CONTENT_DIRS)
    for sub in sorted(wiki.iterdir()):
        if sub.is_dir() and sub.name not in known_dirs:
            mds = list(sub.rglob("*.md"))
            if mds:
                all_issues.append(
                    Issue(
                        "warning",
                        f"{sub.name}/",
                        f"非标准目录含 {len(mds)} 个页面——应归入 "
                        f"{'/'.join(CONTENT_DIRS)}（LLM 路由违规）",
                    )
                )

    all_issues.extend(_find_duplicate_issues(wiki, pages))

    return all_issues


def _find_duplicate_issues(wiki: Path, pages: list[Path]) -> list[Issue]:
    """找内容完全相同的知识页；sources 不参与自动去重。"""
    groups: dict[str, list[Path]] = {}
    for page in pages:
        if page.relative_to(wiki).parts[0] == "sources":
            continue
        try:
            digest = hashlib.sha256(page.read_bytes()).hexdigest()
        except OSError:
            continue
        groups.setdefault(digest, []).append(page)
    issues: list[Issue] = []
    for paths in groups.values():
        if len(paths) < 2:
            continue
        names = sorted(str(p.relative_to(wiki)) for p in paths)
        issues.append(
            Issue(
                "warning",
                names[0],
                "存在完全重复页面: " + ", ".join(names),
            )
        )
    return issues


def cleanup_exact_duplicates(wiki_dir: str | Path) -> list[tuple[str, str]]:
    """删除完全相同的知识页，返回 ``(保留页, 删除页)``。

    只处理 concepts/entities/topics；工作区溯源记录不在扫描范围内。
    保留路径按目录优先级（concepts→entities→topics）再按字典序决定。
    删除前把正文中的 wikilink 指向保留页，避免制造新的死链。
    """
    wiki = Path(wiki_dir)
    pages = [
        page
        for sub in ("concepts", "entities", "topics")
        for page in sorted((wiki / sub).rglob("*.md"))
        if (wiki / sub).is_dir()
    ]
    priority = {"concepts": 0, "entities": 1, "topics": 2}
    groups: dict[str, list[Path]] = {}
    for page in pages:
        try:
            digest = hashlib.sha256(page.read_bytes()).hexdigest()
        except OSError:
            continue
        groups.setdefault(digest, []).append(page)

    replacements: dict[str, str] = {}
    removed: list[tuple[str, str]] = []
    for paths in groups.values():
        if len(paths) < 2:
            continue
        paths.sort(key=lambda p: (priority.get(p.relative_to(wiki).parts[0], 99), str(p)))
        keep = paths[0]
        keep_slug = str(keep.relative_to(wiki)).replace(".md", "")
        for duplicate in paths[1:]:
            duplicate_slug = str(duplicate.relative_to(wiki)).replace(".md", "")
            duplicate.unlink()
            replacements[duplicate_slug] = keep_slug
            removed.append((keep_slug, duplicate_slug))
            emit_event("duplicate_page_removed", keep=keep_slug, removed=duplicate_slug)

    if replacements:
        link_re = re.compile(
            r"\[\[(" + "|".join(re.escape(slug) for slug in replacements) + r")(\|[^\]]+)?\]\]"
        )
        for page in [wiki / "index.md", *pages]:
            if not page.exists():
                continue
            try:
                content = page.read_text(encoding="utf-8")
            except OSError:
                continue
            fixed = link_re.sub(
                lambda m: f"[[{replacements[m.group(1)]}{m.group(2) or ''}]]",
                content,
            )
            if fixed != content:
                page.write_text(fixed, encoding="utf-8")
    return removed


def format_scan_report(issues: list[Issue]) -> str:
    """格式化扫描报告。

    Args:
        issues: 扫描得到的 Issue 列表。

    Returns:
        Markdown 报告文本。
    """
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]

    lines = ["# Wiki 质量扫描报告", ""]
    if not issues:
        lines.append("✅ 全部通过——没有发现问题。")
        return "\n".join(lines)

    lines.append(f"✗ **{len(errors)}** 个错误  ⚠ **{len(warnings)}** 个警告\n")
    if errors:
        lines.append("## 错误")
        lines.extend(str(i) for i in errors)
        lines.append("")
    if warnings:
        lines.append("## 警告")
        lines.extend(str(i) for i in warnings)

    return "\n".join(lines)
