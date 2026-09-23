"""frontmatter 语义、重复页面和 /scan 命令测试。"""

import asyncio
from pathlib import Path

from wiki_agent.agent.commands import CommandContext, CompileCommand, ScanCommand
from wiki_agent.conversation import Session
from wiki_agent.events import AgentHook
from wiki_agent.issues import IssueService, IssueStore
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
        root = wiki  # 命令层经公开 root 属性取 wiki 根

    class _Registry:
        def get(self, name):
            return _ReadFile() if name == "ReadFile" else None

    class _Agent:
        tool_registry = _Registry()
        # /scan 收尾把发现写进问题账本——issue_service 是命令层的必备能力
        issue_service = IssueService(IssueStore(tmp_path / "workspace"))

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


def test_compile_command_enqueues_one_snapshot_sync(tmp_path: Path):
    """批编译入口已并入 sync：/compile = submit_sync，写 wiki 只剩队列一条路。"""
    source = tmp_path / "sources"
    source.mkdir()
    called = {}

    class _Service:
        def submit_sync(self, target):
            called["target"] = target
            return [object(), object()]

    class _Agent:
        job_service = _Service()
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
    assert result.text.startswith("# /compile 已入队")
    assert called["target"] == source.resolve()
    assert "2 个" in result.text


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
        "wiki_agent.agent.commands", fromlist=["CommandReporter"]
    ).CommandReporter(hooks, Session("hook-test"), "compile", "compile_1")

    async def scenario():
        await reporter.start()
        await reporter.progress("extract", current=2, total=5, message="提取中")
        await reporter.end(None)

    asyncio.run(scenario())
    assert hooks.events[0] == ("start", "compile", "compile_1")
    assert hooks.events[1] == ("progress", "extract", "提取中")
    assert hooks.events[2] == ("end", "compile", "compile_1")
