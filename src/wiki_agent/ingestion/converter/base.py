"""转换器基类与统一输出格式。

所有 converter 继承 ``BaseConverter``，输入 ``RawFileProperties``，
输出 ``ConvertedFile``。不同 converter 之间是替代关系——换一个就换一套策略。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from wiki_agent.ingestion.data_loader import RawFileProperties


# ════════════════════════════════════════════════════════════════
#  ConvertedFile — 统一输出
# ════════════════════════════════════════════════════════════════

@dataclass
class ConvertedFile:
    """转换完成后的文件——所有格式统一为文本，准备送入 Chunker。"""

    content: str  # 转换后的完整文本（可能是 Markdown）
    name: str
    ext: str
    path: str
    size_bytes: int = 0
    content_hash: str | None = None
    create_time: str = ""
    modality: str = "text"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw_file: RawFileProperties, content: str) -> "ConvertedFile":
        return cls(
            content=content,
            name=raw_file.name,
            ext=raw_file.ext,
            path=str(raw_file.path),
            size_bytes=len(content),
            content_hash=raw_file.content_hash,
            create_time=raw_file.create_time,
        )


# ════════════════════════════════════════════════════════════════
#  BaseConverter — 抽象接口
# ════════════════════════════════════════════════════════════════

class BaseConverter(ABC):
    """文件 → 文本 的转换器。

    不同实现之间是替代关系——换 converter 就是换解析策略。
    """

    @abstractmethod
    async def convert(self, raw_file: RawFileProperties) -> ConvertedFile:
        """转换单个文件。"""
        ...

    @abstractmethod
    def accepts(self, raw_file: RawFileProperties) -> bool:
        """是否支持该文件。"""
        ...
