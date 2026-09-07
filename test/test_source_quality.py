from pathlib import Path

from wiki_agent.wiki.quality import scan_source, scan_wiki


def _source_page() -> str:
    return """---
type: source
title: 数据清洗
summary: 原始材料摘要
goal: 源文件档案——保存本文档的提取摘要供溯源
related: []
created: 2026-08-20
updated: 2026-08-20
sources: [\"数据清洗.md\"]
---
# 数据清洗

原文示例：[['column1', 'column2']]
"""


def test_source_text_that_looks_like_python_is_not_a_wikilink_error(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "data-cleaning.md").write_text(_source_page(), encoding="utf-8")

    issues = scan_source(tmp_path, source_name="数据清洗.md", source_records_dir=sources)
    assert not [issue for issue in issues if issue.level == "error"]

    issues = scan_wiki(tmp_path)
    assert not [issue for issue in issues if issue.level == "error"]


def test_scan_source_still_rejects_invalid_generated_page(tmp_path: Path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "data-cleaning.md").write_text(_source_page(), encoding="utf-8")
    (tmp_path / "concepts").mkdir()
    page = tmp_path / "concepts" / "cleaning.md"
    page.write_text(
        """---
type: concept
title: 清洗
summary: 清洗概念
goal: 说明清洗
related: []
---
# 清洗

错误链接 [[not-a-page]]
""",
        encoding="utf-8",
    )

    issues = scan_source(
        tmp_path,
        source_name="数据清洗.md",
        source_records_dir=tmp_path / "sources",
        generated_paths=["concepts/cleaning.md"],
    )
    assert any(issue.level == "error" and "wikilink" in issue.message for issue in issues)
