"""通用工具：路径与 token 估计。"""

from functools import lru_cache
from pathlib import Path

import tiktoken


@lru_cache(maxsize=1)
def _encoder():
    """tiktoken 编码器缓存单例：get_encoding 每次调用有开销，
    高频估计路径经 lru_cache 复用。"""
    return tiktoken.get_encoding("cl100k_base")


def ensure_dir(path: Path):
    """递归创建目录。

    Args:
        path: 要创建的目录路径。

    Returns:
        创建成功的目录路径；路径已被文件占用时返回 None。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        return None
    return path


def truncate_text_by_tokens(text: str, max_tokens: int):
    """按 token 数截断文本（截断处附加标志后缀）。

    Args:
        text: 原始文本。
        max_tokens: 允许的最大 token 数。

    Returns:
        截断后的文本；不超过预算时原样返回；编码失败时按
        每 token 2 字符的保守估计截断。
    """
    _TRUNCATED_SUFFIX = "\n... (truncated)"
    enc = _encoder()

    if max_tokens <= 0:
        return ""

    try:
        tokens = enc.encode(text)
        if len(tokens) <= max_tokens:
            return text

        suffix_tokens = enc.encode(_TRUNCATED_SUFFIX)
        suffix_count = len(suffix_tokens)

        if max_tokens <= suffix_count:
            return enc.decode(tokens[:max_tokens])
        body_tokens = tokens[: max_tokens - suffix_count]
        return enc.decode(body_tokens) + _TRUNCATED_SUFFIX
    except Exception:
        # 编码失败时退回字符数粗估
        max_char = max_tokens * 2
        suffix_char = len(_TRUNCATED_SUFFIX)
        if suffix_char >= max_char:
            return text[:max_char]
        else:
            return text[: max_char - suffix_char] + _TRUNCATED_SUFFIX


def estimate_text_tokens(text: str):
    """估算文本的 token 数。

    Args:
        text: 待估算文本。

    Returns:
        token 数；编码失败时返回保守估计（每字符 2 token）。
    """
    enc = _encoder()

    try:
        tokens = enc.encode(text)
        return len(tokens)
    except Exception:
        return len(text) * 2
