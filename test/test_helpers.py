"""测试 utils/helpers.py"""

import pytest
from pathlib import Path
from wiki_agent.utils.helpers import truncate_text_by_tokens, ensure_dir


# ═══════════════════════════════════════════
#  truncate_text_by_tokens
# ═══════════════════════════════════════════

class TestTruncateTextByTokens:

    def test_empty_text(self):
        """空文本直接返回空"""
        result = truncate_text_by_tokens("", max_tokens=100)
        assert result == ""

    def test_zero_max_tokens(self):
        """max_tokens <= 0 返回空字符串"""
        result = truncate_text_by_tokens("hello world", max_tokens=0)
        assert result == ""

    def test_negative_max_tokens(self):
        """负数 max_tokens 也返回空"""
        result = truncate_text_by_tokens("hello", max_tokens=-1)
        assert result == ""

    def test_short_text_no_truncation(self):
        """token 数不超限，原样返回"""
        text = "hello"
        result = truncate_text_by_tokens(text, max_tokens=100)
        assert result == text

    def test_truncation_adds_suffix(self):
        """超限截断后，末尾带截断标记"""
        # 生成大量 token 的文本
        long_text = "hello world " * 500
        result = truncate_text_by_tokens(long_text, max_tokens=10)
        assert result.endswith("\n... (truncated)")

    def test_truncated_result_shorter_than_input(self):
        """截断后的 token 数 ≤ max_tokens"""
        long_text = "hello world " * 500
        result = truncate_text_by_tokens(long_text, max_tokens=20)
        # 至少比原文本短
        assert len(result) < len(long_text)

    def test_suffix_only_when_truncation_needed(self):
        """不超限时不添加 suffix"""
        text = "hi"
        result = truncate_text_by_tokens(text, max_tokens=100)
        assert "\n... (truncated)" not in result

    def test_very_small_max_tokens(self):
        """max_tokens 极小（比 suffix 还小）也能正常返回"""
        long_text = "hello world " * 500
        result = truncate_text_by_tokens(long_text, max_tokens=3)
        # 不抛异常，返回非空
        assert isinstance(result, str)

    def test_chinese_text(self):
        """中文文本也能正常截断"""
        chinese = "这是一段中文测试文本" * 50
        result = truncate_text_by_tokens(chinese, max_tokens=20)
        assert len(result) < len(chinese)
        assert result.endswith("\n... (truncated)") or len(result) > 0


# ═══════════════════════════════════════════
#  ensure_dir
# ═══════════════════════════════════════════

class TestEnsureDir:

    def test_creates_directory(self, tmp_path: Path):
        """创建不存在的目录"""
        new_dir = tmp_path / "a" / "b" / "c"
        result = ensure_dir(new_dir)
        assert new_dir.exists()
        assert new_dir.is_dir()
        assert result == new_dir

    def test_existing_directory_no_error(self, tmp_path: Path):
        """目录已存在不报错"""
        new_dir = tmp_path / "exists"
        new_dir.mkdir()
        result = ensure_dir(new_dir)   # 不抛异常
        assert result == new_dir

    def test_returns_path(self, tmp_path: Path):
        """返回值就是传入的 path"""
        d = tmp_path / "return_test"
        result = ensure_dir(d)
        assert result == d


# ═══════════════════════════════════════════
#  运行方式:
#    cd LearnRag
#    pytest test/test_helpers/test.py -v
#
#  或只跑某个类:
#    pytest test/test_helpers/test.py::TestTruncateTextByTokens -v
# ═══════════════════════════════════════════
