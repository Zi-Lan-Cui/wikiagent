"""质检模块——全库体检 + 报告。

审计路径的完整职责: 查问题、汇总、出报告。
- 页面级: check_page_quality（定稿兜底）、check_dead_links（链接有效性）
- 全库级: scan_wiki（编译结束后调用）、format_scan_report（报告格式化）

分层（依赖单向）:
    checks.py   检测原子 + check 回调——判定逻辑唯一所在
    quality.py  体检（Issue 组装 + scan_wiki 扫描 + 报告）
    normalize.py 修内容（fix/inject）→ 最后调 check_page_quality 兜底
    normalize → quality → checks

判定不重复: check_page_quality 直接调用 checks._check_page_output
（与生成闸门同一判定），拿到 (ok, reason) 后组装 Issue——
同一现象生成时 retry 修正、落盘后 scan 报告，一份判定两种语境。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from wiki_agent.compiler.checks import (
    _body_without_title,
    _check_page_output,
    _extract_body,
    _WIKILINK_RE,
    iter_text_outside_code,
)
from wiki_agent.log import get_logger

logger = get_logger("QUALITY")

# 正文少于该字符数视为"只有一句话"的可疑页面
_MIN_BODY_CHARS = 80

# 内容页面所在的子目录（scan_wiki 扫描范围）
_CONTENT_DIRS = ("concepts", "entities", "topics", "sources")


@dataclass
class Issue:
    """一条检测结果。"""

    level: str          # "error" / "warning"
    path: str           # 页面相对路径
    message: str

    def __str__(self) -> str:
        icon = "✗" if self.level == "error" else "⚠"
        return f"  {icon} [{self.level.upper()}] {self.path}: {self.message}"


# ════════════════════════════════════════════════════════════
#  页面级检测——判定复用 checks 的 check 回调，这里只组装 Issue
# ════════════════════════════════════════════════════════════

def check_page_quality(content: str, *, path: str) -> list[Issue]:
    """单页质量检测——判定与生成闸门（_check_page_output）完全同源。

    Args:
        content: 页面内容
        path: 页面相对路径（如 concepts/lambda.md，用于报错定位）

    闸门不过 → Issue(error)（frontmatter 必填 goal 在内 / 正文存在 /
    wikilink 格式 / fence 闭合）；闸门过了 → 补体检独有的 warning
    观察（正文过短）。
    """
    issues: list[Issue] = []

    if not content or not content.strip():
        return [Issue("error", path, "内容为空")]

    # 判定唯一来源: 生成闸门。落盘后不过 = 页面损伤（error 报告）。
    ok, reason = _check_page_output(content)
    if not ok:
        issues.append(Issue("error", path, reason))
        return issues

    # 闸门是二元判定，不查长度——体检独有的 warning 观察在此补充
    if len(_body_without_title(content)) < _MIN_BODY_CHARS:
        issues.append(Issue(
            "warning", path,
            f"正文过短（{len(_body_without_title(content))} 字符），可能是提取失败的一话页",
        ))

    return issues


def check_dead_links(content: str, *, path: str, valid_slugs: set[str]) -> list[Issue]:
    """死链检测——正文中的 [[wikilink]] 指向不存在的页面。

    跳过代码块（审计 I2）——代码里的 ``[[1, 2, 3]]`` 不是链接。
    """
    if not valid_slugs:
        return []

    issues: list[Issue] = []
    body = _extract_body(content)
    text = "\n".join(iter_text_outside_code(body))
    for m in _WIKILINK_RE.finditer(text):
        slug = m.group(1).strip().replace(".md", "")
        if slug not in valid_slugs:
            issues.append(Issue(
                "warning", path,
                f"死链: [[{m.group(1)}]] 指向不存在的页面",
            ))
    return issues


# ════════════════════════════════════════════════════════════
#  全库体检（编译结束后调用）
# ════════════════════════════════════════════════════════════

def scan_wiki(wiki_dir: str | Path) -> list[Issue]:
    """全库扫描: 质量检测 + 死链检测。

    Args:
        wiki_dir: wiki 根目录

    Returns:
        全部 Issue（error + warning）
    """
    wiki = Path(wiki_dir)
    all_issues: list[Issue] = []
    valid_slugs: set[str] = set()

    pages: list[Path] = []
    for sub in _CONTENT_DIRS:
        d = wiki / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))

    # 第一遍: 收集 slug + 质量检测 + related 完整性 + 矛盾标注
    for page in pages:
        rel = str(page.relative_to(wiki))
        valid_slugs.add(rel.replace(".md", ""))
        try:
            content = page.read_text(encoding="utf-8")
        except Exception as exc:
            all_issues.append(Issue("error", rel, f"读取失败: {exc}"))
            continue
        all_issues.extend(check_page_quality(content, path=rel))
        # sources 档案页 related 恒空（设计内: 档案不参与交叉引用），跳过
        if rel.startswith("sources/"):
            continue
        # related 由 normalize 定稿链注入——缺失/空说明页面没走完整流水线
        m = re.search(r"(?m)^\s*related\s*:\s*(.*)$", content)
        if not m:
            all_issues.append(Issue(
                "warning", rel, "frontmatter 缺少 related 字段（未走 normalize 定稿链）"))
        elif m.group(1).strip() in ("", "[]"):
            all_issues.append(Issue(
                "warning", rel, "related 为空——页面无交叉引用"))
        # 矛盾标注——update 阶段留下的 Disputed 块（contradicts 的落盘形态）。
        # 无自动消费端，裁决是人的事——报告出来让用户处置（原标记永远挂着）
        disputed_count = len(re.findall(
            r"(?m)^\s*>\s*\*\*Status:\s*Disputed\*\*", content))
        if disputed_count:
            all_issues.append(Issue(
                "warning", rel,
                f"页面含 {disputed_count} 处 Disputed 矛盾标注——需人工裁决"
                f"（版本A=已有表述 / 版本B=新表述）"))

    # 第二遍: 死链检测（需要完整 slug 集合）
    for page in pages:
        rel = str(page.relative_to(wiki))
        try:
            content = page.read_text(encoding="utf-8")
        except Exception:
            continue
        all_issues.extend(
            check_dead_links(content, path=rel, valid_slugs=valid_slugs)
        )

    # 第三遍: 根目录垃圾文件 + index 幽灵条目
    index_path = wiki / "index.md"
    try:
        index_content = index_path.read_text(encoding="utf-8")
    except OSError:
        index_content = ""

    # 3a. 根目录 .md——内容页面应全在 _CONTENT_DIRS 下，根目录的 .md
    #     只有 index/purpose/schema 等系统文件（垃圾页审计：wiki/.md）
    system_files = {"index.md", "purpose.md", "schema.md"}
    for f in sorted(wiki.glob("*.md")):
        if f.name in system_files:
            continue
        all_issues.append(Issue(
            "warning", f.name, "根目录多余 .md 文件——内容页应在 concepts/entities/topics/sources 下"))

    # 3b. 幽灵条目——index 有、磁盘无（search 会返回不存在的页面）
    for slug in re.findall(r"\[\[([^\]]+)\]\]", index_content):
        if not (wiki / f"{slug}.md").exists():
            all_issues.append(Issue(
                "error", "index.md", f"幽灵条目: [[{slug}]] 指向不存在的页面"))

    # 3c. 非标准内容目录——LLM 路由违规产物（实测 languages/ tools/）。
    #     四个内容目录之外的 .md 子目录在扫描与 search 中完全隐形
    known_dirs = set(_CONTENT_DIRS) | {".logs", ".watch"}
    for sub in sorted(wiki.iterdir()):
        if sub.is_dir() and sub.name not in known_dirs:
            mds = list(sub.rglob("*.md"))
            if mds:
                all_issues.append(Issue(
                    "warning", f"{sub.name}/",
                    f"非标准目录含 {len(mds)} 个页面——应归入 "
                    f"{'/'.join(_CONTENT_DIRS)}（LLM 路由违规）"))

    return all_issues


def format_scan_report(issues: list[Issue]) -> str:
    """格式化扫描报告。"""
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
