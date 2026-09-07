"""frontmatter 语义、重复页面和 /scan 命令测试。"""

import asyncio
from pathlib import Path

from wiki_agent.command.commands import CommandContext, CompileCommand, ScanCommand
from wiki_agent.hook import AgentHook
from wiki_agent.session import Session
from wiki_agent.wiki.normalize import inject_title_from_h1
from wiki_agent.wiki.quality import cleanup_exact_duplicates, scan_wiki


def _page(
    title="X", *, page_type="concept", created="2026-08-17", updated="2026-08-17", related="[]"
):
    return (
        "---\n"
        f"type: {page_type}\n"
        f"title: {title}\n"
        "summary: 一个正常摘要\n"
        "goal: 说明这个页面\n"
        f"created: {created}\n"
        f"updated: {updated}\n"
        f"related: {related}\n"
        "---\n"
        f"# {title}\n\n这是页面正文内容。\n"
    )


def test_scan_frontmatter_semantics(tmp_path: Path):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "bad.md").write_text(
        _page(
            page_type="entity",
            created="2026-08-18",
            updated="2026-08-17",
            related='["[[concepts/missing]]", "[[concepts/missing]]"]',
        ),
        encoding="utf-8",
    )

    issues = scan_wiki(wiki)
    messages = [issue.message for issue in issues]
    assert any("type/目录不一致" in message for message in messages)
    assert any("updated 早于 created" in message for message in messages)
    assert any("related 存在重复" in message for message in messages)
    assert any("related 指向不存在" in message for message in messages)


def test_cleanup_exact_duplicates_rewrites_links(tmp_path: Path):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    content = _page()
    (wiki / "concepts" / "keep.md").write_text(content, encoding="utf-8")
    (wiki / "concepts" / "duplicate.md").write_text(content, encoding="utf-8")
    (wiki / "index.md").write_text("- [[concepts/duplicate]] — duplicate\n", encoding="utf-8")

    removed = cleanup_exact_duplicates(wiki)

    assert removed == [("concepts/duplicate", "concepts/keep")]
    # 字典序优先时 duplicate 会被保留，链接仍应指向实际存在的页面。
    assert (wiki / "concepts" / "duplicate.md").exists()
    assert not (wiki / "concepts" / "keep.md").exists()
    assert "[[concepts/duplicate]]" in (wiki / "index.md").read_text(encoding="utf-8")


def test_scan_command_returns_formatted_report(tmp_path: Path):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "x.md").write_text(_page(), encoding="utf-8")

    class _ReadFile:
        _root = wiki

    class _Registry:
        def get(self, name):
            return _ReadFile() if name == "ReadFile" else None

    class _Agent:
        tool_registry = _Registry()

    result = asyncio.run(
        ScanCommand().execute(
            CommandContext(
                raw="/scan",
                key="scan",
                args="",
                session=Session("t"),
                agent=_Agent(),
            )
        )
    )
    assert result.text.startswith("# Wiki 质量扫描报告")


def test_scan_detects_island_pages_without_counting_index(tmp_path: Path):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "sources").mkdir()
    (wiki / "concepts" / "root.md").write_text(
        _page(title="Root").replace(
            "这是页面正文内容。",
            "这是根页面，正文指向 [[concepts/linked|Linked]]。",
        ),
        encoding="utf-8",
    )
    (wiki / "concepts" / "linked.md").write_text(_page(title="Linked"), encoding="utf-8")
    (wiki / "concepts" / "island.md").write_text(_page(title="Island"), encoding="utf-8")
    (wiki / "sources" / "note.md").write_text(
        _page(title="Note", page_type="source"), encoding="utf-8"
    )
    (wiki / "index.md").write_text(
        "- [[concepts/root]]\n- [[concepts/linked]]\n- [[concepts/island]]\n",
        encoding="utf-8",
    )

    issues = scan_wiki(wiki)
    island_paths = {issue.path for issue in issues if "孤岛页面" in issue.message}
    assert "concepts/island.md" in island_paths
    assert "concepts/linked.md" not in island_paths
    assert "concepts/root.md" in island_paths
    assert "sources/note.md" not in island_paths


def test_title_is_normalized_from_first_non_code_h1():
    content = """---
type: concept
title: 模型错误标题
summary: 页面摘要
goal: 页面目标
---
# 正文规范标题

正文内容足够长，用于验证页面规范化会将 frontmatter 标题同步为正文标题。
"""
    result = inject_title_from_h1(content)
    assert 'title: "正文规范标题"' in result
    assert "title: 模型错误标题" not in result


def test_title_ignores_h1_inside_code_block():
    content = """---
type: concept
title: 原标题
summary: 页面摘要
goal: 页面目标
---
```markdown
# 代码中的标题
```

# 正确标题

正文内容足够长，用于验证代码块中的伪标题不会成为页面标题。
"""
    result = inject_title_from_h1(content)
    assert 'title: "正确标题"' in result


def test_compile_command_calls_reusable_compile_entry(tmp_path: Path, monkeypatch):
    import wiki_agent.application.compile_service as compile_module

    source = tmp_path / "sources"
    source.mkdir()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    run_dir = wiki / ".logs" / "runs" / "compile_test"
    called = {}

    async def fake_compile(path, *, project_root, wiki_dir, progress=None):
        called.update(path=path, project_root=project_root, wiki_dir=wiki_dir)
        return run_dir

    monkeypatch.setattr(compile_module, "compile_sources", fake_compile)

    class _ReadFile:
        _root = wiki

    class _Registry:
        def get(self, name):
            return _ReadFile() if name == "ReadFile" else None

    class _Agent:
        tool_registry = _Registry()
        workspace = tmp_path / "workspace"

    result = asyncio.run(
        CompileCommand().execute(
            CommandContext(
                raw=f'/compile "{source}"',
                key="compile",
                args=f'"{source}"',
                session=Session("compile-test"),
                agent=_Agent(),
            )
        )
    )
    assert result.text.startswith("# /compile 完成")
    assert called["path"] == str(source)
    assert called["wiki_dir"] == wiki


def test_command_progress_uses_shared_hook_protocol():
    class Hooks(AgentHook):
        def __init__(self):
            super().__init__()
            self.events = []

        async def on_command_start(self, context, command, task_id):
            self.events.append(("start", command, task_id))

        async def on_command_progress(self, context, progress):
            self.events.append(("progress", progress.stage, progress.message))

        async def on_command_end(self, context, command, task_id, result):
            self.events.append(("end", command, task_id))

    hooks = Hooks()
    reporter = __import__(
        "wiki_agent.command.commands", fromlist=["CommandReporter"]
    ).CommandReporter(hooks, Session("hook-test"), "compile", "compile_1")

    async def scenario():
        await reporter.start()
        await reporter.progress("extract", current=2, total=5, message="提取中")
        await reporter.end(None)

    asyncio.run(scenario())
    assert hooks.events[0] == ("start", "compile", "compile_1")
    assert hooks.events[1] == ("progress", "extract", "提取中")
    assert hooks.events[2] == ("end", "compile", "compile_1")
