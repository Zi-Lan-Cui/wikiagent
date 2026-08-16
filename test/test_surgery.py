"""surgery 冲突消解与执行安全测试。

直接运行:  .venv/bin/python test/test_surgery.py
"""

import tempfile
from pathlib import Path

from wiki_agent.compiler.surgery import (
    Conflict,
    Proposal,
    _filter_valid_pages,
    _load_pages,
    resolve_conflicts,
)


def _make_wiki(tmp: Path) -> Path:
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    for slug, body in [
        ("a", "A 的正文内容，足够长。" * 5),
        ("b", "B 的正文内容，足够长。" * 5),
        ("c", "C 的正文内容，足够长。" * 10),
    ]:
        (wiki / "concepts" / f"{slug}.md").write_text(
            f"---\ntype: concept\ntitle: \"{slug.upper()}\"\n"
            f"summary: \"{slug} 摘要\"\ngoal: \"g\"\nrelated: []\n---\n{body}\n",
            encoding="utf-8",
        )
    return wiki


def test_resolve_conflicts_dedup():
    """完全重复的提议去重。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    pages = _load_pages(wiki)
    props = [
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/b"],
                 target="concepts/a"),
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/b"],
                 target="concepts/a"),
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
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/c"],
                 target="concepts/a"),
        Proposal(op="merge_into_second", pages=["concepts/a", "concepts/c"],
                 target="concepts/c"),
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
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/b"],
                 target="concepts/a"),
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
        Proposal(op="merge_into_first", pages=["concepts/a", "concepts/b"],
                 target="concepts/a"),
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
        Proposal(op="merge", pages=["concepts/a", "concepts/ghost"],
                 reason="x"),
        Proposal(op="delete", pages=["concepts/b"], reason="y"),
    ]
    valid = _filter_valid_pages(props, pages)
    assert len(valid) == 1
    assert valid[0].pages == ["concepts/b"]


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
