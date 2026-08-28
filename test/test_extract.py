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
        assert "外部知识" in text or "训练记忆" in text
        assert "因果" in text
        assert "不要" in text

    assert "逐句检查" in synthesis
    assert "当前片段没有明确说明" in rolling


def test_prompt_keeps_source_content_in_dynamic_user_part():
    chunk = SourceChunk(
        content="这是一个只应出现在用户段的特殊原文 token GroundedABC。",
        index=0,
        total=1,
        source_name="sample.md",
    )

    assert "GroundedABC" not in prompts.chunk_system()
    assert "GroundedABC" in prompts.chunk_user(chunk)


def test_page_prompts_separate_existing_page_from_new_source():
    new_system = prompts.new_page_system()
    update_system = prompts.update_system()
    assert "内容边界" in new_system
    assert "本次新信息是唯一允许新增事实" in update_system
    assert "已有页面是保留基线" in update_system
    assert "不要根据常识" in update_system


def test_plan_prompt_prefers_updates_and_rejects_stub_pages():
    system = prompts.plan_system()
    assert "默认优先 update 已有页面" in system
    assert "两个独立事实" in system
    assert "不能单独建页" in system
    assert "source 覆盖检查" in system
    assert "new、update 或 skip" in system


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
    assert "保留基线，不是新增证据" in prompt
    assert "唯一新增事实来源" in prompt
    assert "最多重试 3 次" in prompt


def test_analyze_prompt_requires_evidence_and_conservative_relations():
    system = prompts.analyze_system()
    assert "默认关系" in system
    assert "证据不足" in system
    assert "候选页输入中明确出现" in system

    from wiki_agent.compiler.models import ExtractResult

    user = prompts.analyze_user(
        ExtractResult(source_identity="note.md", document_summary="只说明 A。"),
        "- [[concepts/b|B]] — summary: B",
    )
    assert "唯一的 source 事实来源" in user
    assert "不是 source 事实来源" in user
