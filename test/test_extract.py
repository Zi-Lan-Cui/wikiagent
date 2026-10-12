"""Extract prompt 约束测试。

这些用例不调用真实 LLM；真实模型压缩效果使用配置好的 API 做样本回归。
"""

from wiki_agent.compiler.models import SourceChunk
from wiki_agent.compiler.prompts import compile as prompts


def test_grounding_rules_are_present_in_all_extract_prompts():
    chunk = prompts.chunk_system()
    rolling = prompts.rolling_system()
    synthesis = prompts.synthesis_prompt()

    for text in (chunk, rolling, synthesis):
        assert "training memory" in text
        assert "causal" in text
        assert "never" in text.lower()

    assert "sentence by sentence" in synthesis
    assert "do not add anything the fragment does not state" in rolling


def test_prompt_keeps_source_content_in_dynamic_user_part():
    chunk = SourceChunk(
        content="这是一个只应出现在用户段的特殊原文 token GroundedABC。",
        index=0,
        total=1,
        source_name="sample.md",
    )

    assert "GroundedABC" not in prompts.chunk_system()
    assert "GroundedABC" in prompts.chunk_user(chunk)


def test_ingest_fills_chunk_source_name():
    """_to_source_chunk 必须带源文件名——否则 chunk 摘要 prompt 拼出"from "空标注。"""
    from types import SimpleNamespace

    from wiki_agent.compiler.workflows.ingest import _to_source_chunk

    ck = SimpleNamespace(content="一段原文", chunk_index=0, metadata={"heading_path": "h"})
    sc = _to_source_chunk(ck, 1, "sample.md")
    assert sc.source_name == "sample.md"
    assert "from sample.md" in prompts.chunk_user(sc)


def test_candidate_outlines_include_section_gists():
    from wiki_agent.compiler.integration.analyze import (
        ANALYZE_GIST_SHORT_CHARS,
        render_candidates,
    )
    from wiki_agent.wiki.sections import Section

    long_text = "甲" * 120
    secs = [
        Section(slug="concepts/a", heading="", body=f"# 甲\n\n导语定调整页：{long_text}"),
        Section(slug="concepts/a", heading="机制", body=f"```python\nx = 1\n```\n\n正文首句：{long_text}"),
    ]
    fm = {"title": "甲", "summary": "摘要", "type": "concept"}
    out = render_candidates([("wiki/concepts/a.md", fm, secs)])[0]
    assert "导语定调整页" in out and "正文首句" in out  # 节散文进大纲
    assert "```python" not in out  # 代码块不当摘要

    many = [Section(slug=f"concepts/p{i}", heading="节", body="乙" * 200) for i in range(13)]
    out_short = render_candidates([("wiki/concepts/p0.md", fm, many)])[0]
    gist_lens = [ln.split("—— ")[1] for ln in out_short.splitlines() if "—— " in ln]
    assert all(len(g) <= ANALYZE_GIST_SHORT_CHARS for g in gist_lens)  # 合计超阈值落短档


def test_page_prompts_separate_existing_page_from_new_source():
    new_system = prompts.new_page_system()
    update_system = prompts.update_system()
    assert "Content boundary" in new_system
    assert "only allowed source of new facts" in update_system
    assert "retention baseline" in update_system
    assert "Never add new sections from common sense" in update_system


def test_plan_prompt_prefers_updates_and_rejects_stub_pages():
    system = prompts.plan_system()
    assert "Default to updating existing pages" in system
    assert "two independent facts" in system
    assert "cannot justify a page alone" in system
    assert "Source-coverage check" in system
    assert "new, update or skip" in system


def test_update_prompt_labels_existing_content_as_non_evidence():
    from wiki_agent.compiler.models import Disposition, ExtractResult, PageTarget

    target = PageTarget(
        wiki_path="concepts/retry.md",
        title="重试",
        disposition=Disposition.UPDATE,
        reason="补充最大重试次数",
    )
    prompt = prompts.update_user(
        target,
        "---\ntitle: 重试\n---\n# 重试\n\n已有内容",
        ExtractResult(source_identity="retry.md", document_summary="最多重试 3 次。"),
    )
    assert "retention baseline, not new evidence" in prompt
    assert "only source of new facts" in prompt
    assert "最多重试 3 次" in prompt


def test_analyze_prompt_requires_evidence_and_conservative_relations():
    system = prompts.analyze_system()
    assert "this is the default" in system
    assert "证据不足" in system  # 输出内嵌标记，随分析文本落盘，保持资料语言
    assert "appears literally in the candidate input" in system

    from wiki_agent.compiler.models import ExtractResult

    user = prompts.analyze_user(
        ExtractResult(source_identity="note.md", document_summary="只说明 A。"),
        "- [[concepts/b|B]] — summary: B",
    )
    assert "the only source of source facts" in user
    assert "not a source of facts" in user
