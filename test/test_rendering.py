"""输入渲染与评判模型选取——截断预算、回退语义。

直接运行:  .venv/bin/python test/test_rendering.py
"""

import pytest

from evals.core.judge_client import resolve_judge_llm
from evals.judges.rendering import PER_FILE_BUDGET, render_blocks
from wiki_agent.config import JudgeConfig, LLMConfig


def test_truncation_at_file_boundaries():
    # 填充符用大写：小写会撞 prompt 固定词表（claim/analyze/index 等）污染计数
    blocks = [
        ("artifacts/a/extract.txt", "K" * 900),
        ("artifacts/a/analyze.json", "M" * 12827),
        ("index-snapshots/a.index.json", "Z" * 7849),
        ("compile-time-trees/a.tree.json", "W" * 9313),
    ]
    content = render_blocks(blocks)
    assert "### compile-time-trees/a.tree.json" in content
    assert content.count("W") == 9313 and content.count("Z") == 7849
    assert content.count("M") == PER_FILE_BUDGET  # 唯一的截断者是超长 analyze
    assert content.count("K") == 900
    assert "本文件截断于" in content
    assert content.count("### ") == 4


def test_short_files_never_compete():
    blocks = [(f"dir{i}/f.txt", "s" * 100) for i in range(5)]
    content = render_blocks(blocks)
    assert content.count("s") == 500
    assert "截断" not in content


def test_str_path_backward_compatible():
    content = render_blocks("Q" * (PER_FILE_BUDGET + 1000))
    assert content.count("Q") == PER_FILE_BUDGET
    assert "材料已截断" in content


def test_judge_config_fallback():
    """JUDGE_* 整组配齐才接管；缺一项即回退主 LLM，防半套配置静默走错端点。"""
    from types import SimpleNamespace

    llm = LLMConfig(api_key="k", base_url="u", model_id="m")
    assert resolve_judge_llm(SimpleNamespace(llm=llm, judge=JudgeConfig())) is llm
    half = JudgeConfig(api_key="j")
    assert resolve_judge_llm(SimpleNamespace(llm=llm, judge=half)) is llm
    full = JudgeConfig(api_key="j", model_id="judge-model", base_url="ju")
    assert resolve_judge_llm(SimpleNamespace(llm=llm, judge=full)) is full


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
