import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiki_agent.wiki import (
    WikiPageNotFound,
    read_authorized_source,
    read_page,
    read_source,
    search_pages,
)


def test_read_page_normalizes_path_and_blocks_internal_files(tmp_path: Path):
    (tmp_path / "concepts").mkdir()
    (tmp_path / "concepts" / "decorators.md").write_text(
        "---\ntype: concept\ntags: [python, meta]\n---\n# Decorators", encoding="utf-8"
    )
    (tmp_path / ".logs").mkdir()
    (tmp_path / ".logs" / "run.md").write_text("secret", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "internal.md").write_text("git internals", encoding="utf-8")

    page = read_page(tmp_path, "wiki/concepts/decorators")
    assert page.path == "concepts/decorators.md"
    assert page.content.endswith("# Decorators")
    assert page.metadata == {"type": "concept", "tags": ["python", "meta"]}
    with pytest.raises(WikiPageNotFound):
        read_page(tmp_path, ".logs/run.md")
    with pytest.raises(WikiPageNotFound):
        read_page(tmp_path, ".git/internal.md")
    with pytest.raises(WikiPageNotFound):
        read_page(tmp_path, "../secret.md")


def test_search_pages_matches_path_and_content(tmp_path: Path):
    (tmp_path / "concepts").mkdir()
    (tmp_path / "concepts" / "decorators.md").write_text("metadata transparency", encoding="utf-8")
    assert [page.path for page in search_pages(tmp_path, "transparency")] == [
        "concepts/decorators.md"
    ]


def test_source_reader_has_a_separate_read_only_boundary(tmp_path: Path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "reference.md").write_text("# Original reference", encoding="utf-8")

    with pytest.raises(WikiPageNotFound):
        read_page(tmp_path, "sources/reference.md")

    source = read_source(source_dir, "reference.md")
    assert source.path == "sources/reference.md"
    assert source.content == "# Original reference"

    with pytest.raises(WikiPageNotFound):
        read_source(source_dir, "../reference.md")


def test_authorized_source_uses_public_label_without_exposing_path(tmp_path: Path):
    private = tmp_path / "outside" / "private-name.md"
    private.parent.mkdir()
    private.write_text("# Private source", encoding="utf-8")

    source = read_authorized_source(private, label="source-001.md")

    assert source.path == "sources/source-001.md"
    assert source.content == "# Private source"


def test_wiki_command_open_and_search(tmp_path: Path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "decorators.md").write_text("# Decorators\n\nmetadata", encoding="utf-8")

    class Registry:
        def get(self, name):
            return SimpleNamespace(_root=wiki) if name == "ReadFile" else None

    from wiki_agent.agent.commands import CommandContext, WikiCommand
    from wiki_agent.conversation import Session

    context = CommandContext(
        raw="/wiki open decorators.md",
        key="wiki",
        args="open decorators.md",
        session=Session("test"),
        agent=SimpleNamespace(tool_registry=Registry()),
    )
    opened = asyncio.run(WikiCommand().execute(context))
    assert opened.text == "# decorators.md\n\n# Decorators\n\nmetadata"

    context.args = "search metadata"
    found = asyncio.run(WikiCommand().execute(context))
    assert "`decorators.md`" in found.text
