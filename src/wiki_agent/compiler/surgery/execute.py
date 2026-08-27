"""结构手术的原子执行（无 LLM）——内存算最终状态后按依赖序列落盘。

复用 resolve（依赖序列 + 被吸收页判定）、rewrite（链接改写）、
transaction（备份/回滚）、common（页面表）、wiki（related/normalize）。
同 group_id 的 create+trim 是一个事务，失败整组回滚。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from wiki_agent.compiler.surgery.common import _load_pages
from wiki_agent.compiler.surgery.models import Proposal, SurgeryResult
from wiki_agent.compiler.surgery.resolve import _src_of, _validate_operation_sequence
from wiki_agent.compiler.surgery.rewrite import _plain_source_links, _rewrite_source_links
from wiki_agent.compiler.surgery.transaction import _backup, _restore_group, _snapshot_group
from wiki_agent.compiler.wiki.frontmatter import split_frontmatter
from wiki_agent.compiler.wiki.normalize import extract_related, normalize_page
from wiki_agent.errors import translate_generic_error
from wiki_agent.log import emit_event, get_logger

logger = get_logger("SURGERY")


def _remove_index_entry(wiki: Path, slug: str) -> None:
    index_path = wiki / "index.md"
    if not index_path.exists():
        return
    lines = [
        line
        for line in index_path.read_text(encoding="utf-8").split("\n")
        if f"[[{slug}]]" not in line
    ]
    index_path.write_text("\n".join(lines), encoding="utf-8")


def execute_merge(wiki: Path, prop: Proposal) -> None:
    """合并吸收——原子: B 正文拼接进 A + 链接重写 + index 清理 + 删 B。

    三类引用三种处理（bug 教训——顺序和分类都是领域知识）:
    - 源页正文内的自引用 → [[target]]（内容搬家，链接跟着搬）
    - 目标页原正文对源页的引用 → 纯文本别名（合并后自链无意义）
    - 其他页对源页的引用 → [[target]]（保留别名）
    吸收章节标题用纯文本（不带 wikilink）——重写器碰不到它。

    Args:
        wiki: wiki 根目录。
        prop: merge 提议（含方向与 target）。
    """
    source_slug = prop.pages[1] if prop.op == "merge_into_first" else prop.pages[0]
    target_slug = prop.target
    pages = _load_pages(wiki)
    target = pages[target_slug]
    source = pages[source_slug]

    # 1. 拼接（三处各自处理引用，然后写盘）
    source_body = _rewrite_source_links(source["body"], source_slug, target_slug)
    target_orig = target["path"].read_text(encoding="utf-8").rstrip()
    target_orig = _plain_source_links(target_orig, source_slug, source["title"])
    merged = f"{target_orig}\n\n## 合并自 {source['title']} 的内容\n\n{source_body}\n"
    # related 由代码重推（extract_related 与 normalize 同源逻辑）
    valid = {s for s in pages if s != source_slug}
    merged = extract_related(merged, valid_slugs=valid)
    target["path"].write_text(merged, encoding="utf-8")
    logger.info("  ✓ 合并: %s 吸收 %s", target_slug, source_slug)

    # 2. 其他页链接重写 → [[target]]
    for slug, page in pages.items():
        if slug in (target_slug, source_slug):
            continue
        p = page["path"]
        content = p.read_text(encoding="utf-8")
        new_content = _rewrite_source_links(content, source_slug, target_slug)
        if new_content != content:
            p.write_text(new_content, encoding="utf-8")
            logger.info("  链接重写: %s 中 [[%s]] → [[%s]]", p.name, source_slug, target_slug)

    # 3. index 清理 + 删除源页
    _remove_index_entry(wiki, source_slug)
    source["path"].unlink()
    logger.info("  ✓ 删除: %s（已合并进 %s）", source_slug, target_slug)


def execute_delete(wiki: Path, prop: Proposal) -> None:
    """纯删除——文件 + index + 引用它的链接转纯文本。

    Args:
        wiki: wiki 根目录。
        prop: delete 提议。
    """
    slug = prop.pages[0]
    pages = _load_pages(wiki)
    # 引用者链接转纯文本（死链防线同款语义）
    pattern = re.compile(rf"\[\[{re.escape(slug)}(?:\|([^\]]+?))?\]\]")
    for page in pages.values():
        p = page["path"]
        content = p.read_text(encoding="utf-8")
        new_content = pattern.sub(
            lambda m: m.group(1) if m.group(1) else slug.rsplit("/", 1)[-1],
            content,
        )
        if new_content != content:
            p.write_text(new_content, encoding="utf-8")
    _remove_index_entry(wiki, slug)
    (wiki / f"{slug}.md").unlink()
    logger.info("  ✓ 删除: %s", slug)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _section_ranges(body: str, names: list[str]) -> list[tuple[int, int]]:
    """返回指定 Markdown 章节的字符范围（含标题，不跨越同级父标题）。"""
    wanted = {name.strip().lstrip("#").strip() for name in names}
    lines = body.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []
    offset = 0
    for line in lines:
        match = _HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            headings.append((offset, len(match.group(1)), match.group(2).strip()))
        offset += len(line)
    ranges: list[tuple[int, int]] = []
    for start, level, title in headings:
        if title not in wanted:
            continue
        end = len(body)
        for next_start, next_level, _ in headings:
            if next_start > start and next_level <= level:
                end = next_start
                break
        ranges.append((start, end))
    if len(ranges) != len(wanted):
        found = {body[a:b].split("\n", 1)[0].lstrip("#").strip() for a, b in ranges}
        missing = sorted(wanted - found)
        raise ValueError(f"找不到指定章节: {missing}")
    return sorted(ranges)


def _extract_sections(body: str, names: list[str]) -> str:
    return "\n\n".join(body[start:end].strip() for start, end in _section_ranges(body, names))


def _trim_sections(body: str, names: list[str]) -> str:
    ranges = _section_ranges(body, names)
    result = body
    for start, end in reversed(ranges):
        result = result[:start] + result[end:]
    return result.strip()


def _page_type_for_dir(directory: str) -> str:
    return {"concepts": "concept", "entities": "entity", "topics": "topic"}.get(directory, "topic")


def _create_page_content(source: dict, prop: Proposal, target_slug: str, *, valid: set[str]) -> str:
    """由源页元数据和代码切出的章节构造新页，不让 LLM 直接提供正文。"""
    directory, _, basename = target_slug.partition("/")
    title = prop.title.strip() or basename.replace("-", " ").replace("_", " ").title()
    summary = prop.summary.strip() or source["summary"]
    goal = prop.goal.strip() or source["frontmatter"].get("goal", "")
    fm = source["frontmatter"]
    content = (
        "---\n"
        f"type: {_page_type_for_dir(directory)}\n"
        f'title: "{title}"\n'
        f'summary: "{summary}"\n'
        f'goal: "{goal}"\n'
        "related: []\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{_extract_sections(source['body'], prop.sections)}\n"
    )
    normalized, issues = normalize_page(
        content,
        path=f"{target_slug}.md",
        valid_slugs=valid,
        source_identity="",
        today=date.today().isoformat(),
        existing=fm,
    )
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        raise ValueError("新页面质检失败: " + "; ".join(i.message for i in errors))
    return normalized


def _append_index_entry(wiki: Path, slug: str, content: str) -> None:
    index = wiki / "index.md"
    existing = index.read_text(encoding="utf-8") if index.exists() else ""
    if f"[[{slug}]]" in existing:
        return
    fm, _ = split_frontmatter(content)
    line = f"- [[{slug}]] — [{fm.get('type', '')}] {slug}.md — {fm.get('title', slug)}"
    if fm.get("summary"):
        line += f" — {fm['summary']}"
    index.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")


def execute_create(wiki: Path, prop: Proposal) -> None:
    """create 原子：代码切章节、生成规范 frontmatter、写页、登记 index。"""
    source_slug = prop.pages[0]
    pages = _load_pages(wiki)
    if source_slug not in pages:
        raise FileNotFoundError(source_slug)
    if not prop.target or prop.target in pages or (wiki / f"{prop.target}.md").exists():
        raise FileExistsError(prop.target)
    target = wiki / f"{prop.target}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    valid = set(pages) | {prop.target}
    content = _create_page_content(pages[source_slug], prop, prop.target, valid=valid)
    target.write_text(content, encoding="utf-8")
    _append_index_entry(wiki, prop.target, content)
    logger.info("  ✓ 创建: %s（来自 %s）", prop.target, source_slug)


def execute_trim(wiki: Path, prop: Proposal) -> None:
    """trim 原子：按章节标题移除正文，并重新计算 related，禁止空页落盘。"""
    slug = prop.pages[0]
    pages = _load_pages(wiki)
    if slug not in pages:
        raise FileNotFoundError(slug)
    source = pages[slug]
    body = _trim_sections(source["body"], prop.sections)
    if not body.strip():
        raise ValueError(f"trim 后页面为空: {slug}")
    original = source["path"].read_text(encoding="utf-8")
    fm, _ = split_frontmatter(original)
    content = "---\n" + original.split("\n---\n", 1)[0][4:] + "\n---\n\n" + body + "\n"
    normalized, issues = normalize_page(
        content,
        path=f"{slug}.md",
        valid_slugs=set(pages),
        source_identity="",
        today=date.today().isoformat(),
        existing=fm,
    )
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        raise ValueError("裁剪后页面质检失败: " + "; ".join(i.message for i in errors))
    source["path"].write_text(normalized, encoding="utf-8")
    logger.info("  ✓ 裁剪: %s（移除 %s）", slug, prop.sections)


def execute(
    wiki_dir: str | Path,
    proposals: list[Proposal],
    *,
    backup_dir: str | Path | None = None,
) -> SurgeryResult:
    """执行全部复判通过的提议——先备份，再逐个原子动作。

    - 状态复核: 执行前重读页面表，页面已不存在的动作跳过（记 skipped），
      不 KeyError 不误删
    - 备份: backup_dir 提供时复制受影响文件 + index（破坏性操作可回滚）
    - 结果结构化: SurgeryResult（actions/skipped/backed_up）——机器可消费

    Args:
        wiki_dir: wiki 根目录。
        proposals: 复判通过的提议。
        backup_dir: 备份目录（None 跳过备份）。

    Returns:
        结构化执行结果。
    """
    wiki = Path(wiki_dir)
    pages = _load_pages(wiki)
    result = SurgeryResult(actions=[], skipped=[], backed_up=[])

    proposals, invalid = _validate_operation_sequence(proposals, pages)
    for conflict in invalid:
        result.skipped.append(f"{conflict.kind}: {conflict.detail}")
        emit_event(
            "surgery_skipped",
            op=conflict.kind,
            pages=[p.pages for p in conflict.proposals],
            reason=conflict.detail,
        )

    # 受影响页面集合 → 备份
    touched = {s for p in proposals for s in p.pages if s in pages}
    if backup_dir is not None:
        result.backed_up = _backup(wiki, pages, touched, Path(backup_dir))

    # 同 group_id 的 create+trim 是一个事务；没有 group_id 的操作各自成组。
    groups: dict[str, list[Proposal]] = {}
    for i, prop in enumerate(proposals):
        groups.setdefault(prop.group_id or f"__single_{i}", []).append(prop)
    completed: set[str] = set()
    failed: set[str] = set()

    for group in groups.values():
        snapshot = (
            _snapshot_group(wiki, group) if any(p.op in ("create", "trim") for p in group) else None
        )
        group_action_count = len(result.actions)
        group_actions: list[str] = []
        group_failed = False
        for prop in group:
            if prop.op in ("keep_both", "keep"):
                logger.info("  - 保留: %s（%s）", prop.pages, prop.reason)
                continue
            if any(dep not in completed for dep in prop.depends_on):
                reason = f"依赖未完成: {prop.depends_on}"
                result.skipped.append(f"{prop.op} {prop.pages}（{reason}）")
                failed.add(prop.id)
                group_failed = True
                continue
            # 状态复核——页面必须还在（前面的动作可能已删掉它）
            missing = [s for s in prop.pages if s not in pages]
            if missing:
                logger.warning("  ✗ 跳过 %s: 页面已不存在 %s", prop.pages, missing)
                result.skipped.append(f"{prop.op} {prop.pages}（页面不存在）")
                emit_event("surgery_skipped", op=prop.op, pages=prop.pages, reason="page_missing")
                failed.add(prop.id)
                group_failed = True
                continue
            try:
                if prop.op.startswith("merge"):
                    execute_merge(wiki, prop)
                    result.actions.append(f"merge {prop.pages} -> {prop.target}")
                    # 更新内存页面表——被吸收页从表里移除，后续动作复核用
                    src = _src_of(prop)
                    del pages[src]
                    emit_event("surgery_merged", pages=prop.pages, target=prop.target)
                elif prop.op == "delete":
                    execute_delete(wiki, prop)
                    result.actions.append(f"delete {prop.pages[0]}")
                    del pages[prop.pages[0]]
                    emit_event("surgery_deleted", pages=prop.pages)
                elif prop.op == "create":
                    execute_create(wiki, prop)
                    result.actions.append(f"create {prop.target} <- {prop.pages[0]}")
                    emit_event(
                        "surgery_created",
                        source=prop.pages[0],
                        target=prop.target,
                        sections=prop.sections,
                    )
                elif prop.op == "trim":
                    execute_trim(wiki, prop)
                    result.actions.append(f"trim {prop.pages[0]}: {prop.sections}")
                    emit_event("surgery_trimmed", pages=prop.pages, sections=prop.sections)
                else:
                    raise ValueError(f"未知原子操作: {prop.op}")
                completed.add(prop.id)
                pages = _load_pages(wiki)
                group_actions.append(prop.id)
            except Exception as e:
                err = translate_generic_error(e, context="surgery execute")
                logger.error("  ✗ 执行失败 %s: %s", prop.pages, str(err)[:200])
                result.skipped.append(f"{prop.op} {prop.pages}（执行失败: {err}）")
                emit_event(
                    "surgery_execute_failed",
                    op=prop.op,
                    pages=prop.pages,
                    error=str(err),
                    cause=type(e).__name__,
                )
                failed.add(prop.id)
                group_failed = True

        if group_failed and snapshot is not None:
            _restore_group(snapshot)
            # 该组此前成功的动作也已回滚，不得被后续依赖使用。
            for op_id in group_actions:
                completed.discard(op_id)
                failed.add(op_id)
            result.actions = result.actions[:group_action_count]
            result.skipped.append(f"group {group[0].group_id or group[0].id}（事务回滚）")
            pages = _load_pages(wiki)

    return result
