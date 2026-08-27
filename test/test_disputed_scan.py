"""scan_wiki Disputed 矛盾标注检测。

直接运行:  .venv/bin/python test/test_disputed_scan.py
"""

import tempfile
from pathlib import Path

from wiki_agent.compiler.wiki.quality import scan_wiki


def _make_wiki(tmp: Path) -> Path:
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    return wiki


def test_scan_detects_disputed_block():
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    (wiki / "concepts" / "x.md").write_text(
        '---\ntype: concept\ntitle: "X"\nsummary: "s"\ngoal: "g"\n'
        "related: []\n---\n# X\n\n正文内容。\n\n"
        "> **Status: Disputed**\n>\n"
        "> - 版本A (已有): 旧说法\n"
        "> - 版本B (新): 新说法\n",
        encoding="utf-8",
    )
    issues = scan_wiki(wiki)
    disputed = [i for i in issues if "Disputed" in i.message]
    assert len(disputed) == 1
    assert disputed[0].level == "warning"
    assert "1 处" in disputed[0].message


def test_scan_counts_multiple_blocks():
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    (wiki / "concepts" / "x.md").write_text(
        '---\ntype: concept\ntitle: "X"\nsummary: "s"\ngoal: "g"\n'
        "related: []\n---\n# X\n\n正文。\n\n"
        "> **Status: Disputed**\n> - 版本A (已有): a1\n> - 版本B (新): b1\n\n"
        "更多正文。\n\n"
        "> **Status: Disputed**\n> - 版本A (已有): a2\n> - 版本B (新): b2\n",
        encoding="utf-8",
    )
    issues = scan_wiki(wiki)
    disputed = [i for i in issues if "Disputed" in i.message]
    assert len(disputed) == 1
    assert "2 处" in disputed[0].message


def test_clean_page_no_disputed_issue():
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    (wiki / "concepts" / "x.md").write_text(
        '---\ntype: concept\ntitle: "X"\nsummary: "s"\ngoal: "g"\n'
        "related: []\n---\n# X\n\n正常正文。\n",
        encoding="utf-8",
    )
    issues = scan_wiki(wiki)
    assert not [i for i in issues if "Disputed" in i.message]


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
