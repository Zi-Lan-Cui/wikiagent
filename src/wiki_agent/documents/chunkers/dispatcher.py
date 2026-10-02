"""Chunker 分发器：按注入顺序取首个能处理该文件的 chunker。"""

from __future__ import annotations

from wiki_agent.documents.chunkers.base import BaseChunker, ChunkedFileProperties
from wiki_agent.documents.chunkers.structured_chunker import StructuredChunker
from wiki_agent.documents.chunkers.text_chunker import TextChunker
from wiki_agent.documents.converters.base import ConvertedFile
from wiki_agent.log import get_logger

logger = get_logger("CHUNKER_DISPATCHER")


class Chunker:
    """按顺序取首个 ``can_process()`` 为 True 的 chunker。

    默认注册 StructuredChunker → TextChunker。
    """

    def __init__(self, chunkers: list[BaseChunker] | None = None):
        self._chunkers = chunkers or [StructuredChunker(), TextChunker()]

    def chunk(self, file: ConvertedFile) -> list[ChunkedFileProperties]:
        """按顺序匹配首个支持的 chunker 并切分。

        Args:
            file: 转换后的文件。

        Returns:
            chunk 列表（无 chunker 支持时返回 []）。
        """
        for ck in self._chunkers:
            if ck.can_process(file):
                return ck.chunk(file)
        logger.warning(f"没有 chunker 支持 {file.name}（ext={file.ext}）")
        return []
