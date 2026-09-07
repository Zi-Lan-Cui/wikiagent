"""WatchConsumer 测试——删除处理规则 + ingest 分发。

直接运行:  .venv/bin/python test/test_consumer.py
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.watch.consumer import WatchConsumer, _clean_body_links
from wiki_agent.watch.state import WatchState


def _make_wiki(tmp: Path) -> tuple[Path, Path]:
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    records = tmp / "workspace" / "provenance" / "sources"
    records.mkdir(parents=True)
    # 工作区溯源记录引用两个源文件
    (records / "note.md").write_text(
        '---\ntype: source\ntitle: "Note"\nsummary: "s"\ngoal: "g"\n'
        'related: []\nsources: ["note.md", "other.md"]\n'
        "---\n# Note\n\n摘要。\n",
        encoding="utf-8",
    )
    # 正文引用 sources 页别名
    (wiki / "concepts" / "page.md").write_text(
        '---\ntype: concept\ntitle: "Page"\nsummary: "s"\ngoal: "g"\n'
        "related: []\n---\n# Page\n\n正文提到 [[sources/note|Note]] 档案。\n",
        encoding="utf-8",
    )
    return wiki, records


def test_clean_body_links_replaces_aliases():
    """正文中 sources 页引用 → 别名纯文本。"""
    tmp = Path(tempfile.mkdtemp())
    wiki, _ = _make_wiki(tmp)
    changed = _clean_body_links(wiki, "note")
    assert changed == 1
    content = (wiki / "concepts" / "page.md").read_text(encoding="utf-8")
    assert "[[sources/note" not in content
    assert "Note" in content  # 别名保留


def test_process_delete_removes_only_entry():
    """sources 页只剩被删文件 → 页删除。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        wiki, records = _make_wiki(tmp)
        state = WatchState(tmp / "state.json")
        queue: asyncio.Queue = asyncio.Queue()
        consumer = WatchConsumer(queue, None, state, wiki_dir=wiki, source_records_dir=records)
        # 手工触发删除处理（pipeline 为 None——删除路径不碰它）
        consumer._process_delete("other.md")
        content = (records / "note.md").read_text(encoding="utf-8")
        # other 从 sources 列表移除
        assert "other.md" not in content
        assert "note.md" in content

    asyncio.run(run())


def test_process_delete_keeps_page_with_multiple_sources():
    """sources 页还剩其他文件 → 保留页仅移除条目。"""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        wiki, records = _make_wiki(tmp)
        state = WatchState(tmp / "state.json")
        queue: asyncio.Queue = asyncio.Queue()
        consumer = WatchConsumer(queue, None, state, wiki_dir=wiki, source_records_dir=records)
        consumer._process_delete("note.md")  # 删了 note 后 sources 只剩 other——页面删
        # note.md 被删后 sources 列表只剩 other.md——页还在
        assert (records / "note.md").exists()
        content = (records / "note.md").read_text(encoding="utf-8")
        assert "note.md" not in content  # 条目移除

    asyncio.run(run())


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
