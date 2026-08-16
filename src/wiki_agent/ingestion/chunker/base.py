"""块基类与输出格式。"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from wiki_agent.ingestion.converter.base import ConvertedFile


# ════════════════════════════════════════════════════════════════
#  ChunkedFileProperties — 统一输出
# ════════════════════════════════════════════════════════════════

class ChunkedFileProperties(BaseModel):
    """单个 chunk——一段完整语义单元。"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    content: str
    chunk_index: int
    metadata: dict = Field(default_factory=dict)


# ════════════════════════════════════════════════════════════════
#  BaseChunker — 抽象接口
# ════════════════════════════════════════════════════════════════

class BaseChunker(ABC):
    """文本 → chunk 的切割器。

    不同实现之间是替代关系——换 chunker 就是换切割策略。
    """

    @abstractmethod
    def chunk(self, file: ConvertedFile) -> list[ChunkedFileProperties]:
        """切割文本为 chunk 列表。"""
        ...

    @abstractmethod
    def can_process(self, file: ConvertedFile) -> bool:
        """是否支持该文件类型。"""
        ...
