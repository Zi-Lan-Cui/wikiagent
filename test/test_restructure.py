"""restructure 冲突消解与执行安全测试。

直接运行:  .venv/bin/python test/test_restructure.py
"""

import tempfile
from pathlib import Path

from wiki_agent.compiler.restructure import (
    Proposal,
    _filter_valid_pages,
    _load_pages,
    execute,
    resolve_conflicts,
)
from wiki_agent.wiki.quality import scan_wiki


def _make_wiki(tmp: Path) -> Path:
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    for slug, body in [
        ("a", "A 的正文内容，足够长。" * 5),
        ("b", "B 的正文内容，足够长。" * 5),
        ("c", "C 的正文内容，足够长。" * 10),
    ]:
        (wiki / "concepts" / f"{slug}.md").write_text(
            f'---\ntype: concept\ntitle: "{slug.upper()}"\n'
            f'summary: "{slug} 摘要"\ngoal: "g"\nrelated: []\n---\n{body}\n',
            encoding="utf-8",
        )
    return wiki


def test_resolve_conflicts_dedup():
    """完全重复的提议去重。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    pages = _load_pages(wiki)
    props = [
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/b"], target="concepts/a"),
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/b"], target="concepts/a"),
    ]
    clean, conflicts = resolve_conflicts(props, pages)
    assert len(clean) == 1
    assert not conflicts


def test_resolve_conflicts_bidirectional_merge():
    """双向 merge → 质量高的一页做吸收方，另一条丢弃。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    pages = _load_pages(wiki)
    props = [
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/c"], target="concepts/a"),
        Proposal(op="merge_into_second", pages=["concepts/a", "concepts/c"], target="concepts/c"),
    ]
    clean, conflicts = resolve_conflicts(props, pages)
    assert len(clean) == 1
    # c 正文更长（10x vs 5x）——质量分高，是吸收方
    assert clean[0].target == "concepts/c"


def test_resolve_conflicts_delete_absorbed_merge_wins():
    """delete 被吸收页 → merge 赢（merge 保留信息，delete 只减不增）。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    pages = _load_pages(wiki)
    props = [
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/b"], target="concepts/a"),
        Proposal(op="delete", pages=["concepts/b"]),
    ]
    clean, conflicts = resolve_conflicts(props, pages)
    # delete b 被 merge 覆盖——只剩 merge
    assert [p.op for p in clean] == ["merge_into_first"]
    assert not conflicts


def test_resolve_conflicts_delete_target_conflict():
    """delete 吸收方 → Conflict（吸收方即将消失，需人工/复裁）。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    pages = _load_pages(wiki)
    props = [
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/b"], target="concepts/a"),
        Proposal(op="delete", pages=["concepts/a"]),
    ]
    clean, conflicts = resolve_conflicts(props, pages)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "delete_vs_merge"


def test_filter_valid_pages_drops_hallucinated():
    """幻觉 slug 提议被丢弃（不 KeyError）。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    pages = _load_pages(wiki)
    props = [
        Proposal(op="merge", pages=["concepts/a", "concepts/ghost"], reason="x"),
        Proposal(op="delete", pages=["concepts/b"], reason="y"),
    ]
    valid = _filter_valid_pages(props, pages)
    assert len(valid) == 1
    assert valid[0].pages == ["concepts/b"]


def _make_split_page(tmp: Path) -> Path:
    wiki = tmp / "split-wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "index.md").write_text(
        "- [[concepts/source]] — [concept] concepts/source.md — Source\n",
        encoding="utf-8",
    )
    (wiki / "concepts/source.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "Source"\n'
        'summary: "A source page with enough metadata"\n'
        'goal: "Explain the source topic clearly"\n'
        "created: 2026-08-01\n"
        "updated: 2026-08-01\n"
        'sources: ["notes.md"]\n'
        "related: []\n"
        "---\n\n"
        "# Source\n\n"
        "## Keep\n\n"
        "This section remains on the original page and contains enough detail to preserve its topic.\n\n"
        "## Move\n\n"
        "This section contains the material that belongs on the newly created page, with enough detail for validation.\n",
        encoding="utf-8",
    )
    return wiki


def test_create_and_trim_are_dependency_ordered_and_indexed():
    """拆分是 create -> trim；新页保留 sources，原页保留未迁移章节。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_split_page(tmp)
    result = execute(
        wiki,
        [
            Proposal(
                op="trim",
                pages=["concepts/source"],
                sections=["Move"],
                id="trim_1",
                group_id="split_1",
                depends_on=["create_1"],
            ),
            Proposal(
                op="create",
                pages=["concepts/source"],
                target="concepts/moved",
                sections=["Move"],
                id="create_1",
                group_id="split_1",
                title="Moved",
                summary="Moved material",
                goal="Explain moved material",
            ),
        ],
    )
    assert result.skipped == []
    assert (wiki / "concepts/moved.md").exists()
    assert "## Move" in (wiki / "concepts/moved.md").read_text(encoding="utf-8")
    original = (wiki / "concepts/source.md").read_text(encoding="utf-8")
    assert "## Move" not in original
    assert "## Keep" in original
    assert '"notes.md"' in (wiki / "concepts/moved.md").read_text(encoding="utf-8")
    assert "[[concepts/moved]]" in (wiki / "index.md").read_text(encoding="utf-8")


def test_split_end_to_end_scan_has_no_structural_errors():
    """完整验证拆分后的页面、索引、链接和 source 关系仍自洽。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "sources").mkdir()

    def page(slug: str, title: str, body: str, related: str = "[]") -> None:
        (wiki / f"{slug}.md").write_text(
            "---\n"
            "type: concept\n"
            f'title: "{title}"\n'
            f'summary: "{title} 摘要内容"\n'
            f'goal: "说明 {title} 的核心概念和使用边界"\n'
            "created: 2026-08-19\n"
            "updated: 2026-08-19\n"
            'sources: ["notes.md"]\n'
            f"related: {related}\n"
            "---\n\n"
            f"# {title}\n\n{body}\n",
            encoding="utf-8",
        )

    page(
        "concepts/source",
        "Source",
        "## Keep\n\n"
        "Python 的基础页面仍保留通用背景，并引用 [[concepts/event-loop|event loop]]。\n\n"
        "## Move\n\n"
        "asyncio 使用 event loop 调度 coroutine；这一节属于独立的异步主题，"
        "并引用 [[concepts/event-loop|event loop]]。\n",
        related='["[[concepts/event-loop|event loop]]"]',
    )
    page(
        "concepts/event-loop",
        "Event Loop",
        "事件循环负责调度异步任务，正文足够完整。",
    )
    page(
        "concepts/reader",
        "Reader",
        "其他页面仍然引用原页面 [[concepts/source|Source]]，拆分不应破坏该链接。",
    )
    (wiki / "sources" / "notes.md").write_text(
        "---\n"
        "type: source\n"
        "title: Notes\n"
        "summary: 原始笔记来源\n"
        "goal: 保存拆分测试使用的原始来源\n"
        "---\n\n"
        "# Notes\n\n这是拆分页面使用的原始来源材料。\n",
        encoding="utf-8",
    )
    (wiki / "index.md").write_text(
        "- [[concepts/source]] — [concept] concepts/source.md — Source\n"
        "- [[concepts/event-loop]] — [concept] concepts/event-loop.md — Event Loop\n"
        "- [[concepts/reader]] — [concept] concepts/reader.md — Reader\n",
        encoding="utf-8",
    )

    proposals = [
        Proposal(
            op="trim",
            pages=["concepts/source"],
            sections=["Move"],
            id="trim_move",
            group_id="split_asyncio",
            depends_on=["create_asyncio"],
            reason="拆出独立异步主题",
        ),
        Proposal(
            op="create",
            pages=["concepts/source"],
            target="concepts/asyncio",
            sections=["Move"],
            id="create_asyncio",
            group_id="split_asyncio",
            title="Asyncio",
            summary="Asyncio 调度机制",
            goal="说明 Asyncio 的基本调度机制",
            reason="拆出独立异步主题",
        ),
    ]
    clean, conflicts = resolve_conflicts(proposals, _load_pages(wiki))
    assert conflicts == []
    result = execute(wiki, clean)
    assert result.skipped == []

    new_page = (wiki / "concepts/asyncio.md").read_text(encoding="utf-8")
    old_page = (wiki / "concepts/source.md").read_text(encoding="utf-8")
    index = (wiki / "index.md").read_text(encoding="utf-8")
    assert "## Move" in new_page
    assert '"notes.md"' in new_page
    assert "[[concepts/event-loop|event loop]]" in new_page
    assert 'related: ["[[concepts/event-loop]]"]' in new_page
    assert "## Move" not in old_page
    assert "## Keep" in old_page
    assert "[[concepts/asyncio]]" in index
    assert "[[concepts/source|Source]]" in (wiki / "concepts/reader.md").read_text(encoding="utf-8")

    issues = scan_wiki(wiki)
    errors = [issue for issue in issues if issue.level == "error"]
    assert errors == []
    assert not any("死链" in issue.message or "幽灵条目" in issue.message for issue in issues)


def test_create_trim_group_rolls_back_when_trim_fails():
    """create 成功但 trim 失败时，新页和 index 一并回滚。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_split_page(tmp)
    result = execute(
        wiki,
        [
            Proposal(
                op="create",
                pages=["concepts/source"],
                target="concepts/moved",
                sections=["Move"],
                id="create_1",
                group_id="split_1",
                title="Moved",
                summary="Moved material",
                goal="Explain moved material",
            ),
            Proposal(
                op="trim",
                pages=["concepts/source"],
                sections=["Missing"],
                id="trim_1",
                group_id="split_1",
                depends_on=["create_1"],
            ),
        ],
    )
    assert not (wiki / "concepts/moved.md").exists()
    assert "[[concepts/moved]]" not in (wiki / "index.md").read_text(encoding="utf-8")
    assert "## Move" in (wiki / "concepts/source.md").read_text(encoding="utf-8")
    assert any("事务回滚" in item for item in result.skipped)


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
