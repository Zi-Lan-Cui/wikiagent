"""Chunker 分发器。

按注入顺序遍历，首个能处理该文件的 chunker 负责。
"""

from __future__ import annotations

from wiki_agent.documents.chunkers.base import BaseChunker, ChunkedFileProperties
from wiki_agent.documents.chunkers.structured_chunker import StructuredChunker
from wiki_agent.documents.chunkers.text_chunker import TextChunker
from wiki_agent.documents.converters.base import ConvertedFile
from wiki_agent.log import get_logger

logger = get_logger("CHUNKER_DISPATCHER")


class Chunker:
    """文本 → chunk 的分发器。

    chunker 列表按顺序匹配，首个 ``can_process()`` 的获胜。
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

    def batch_chunk(
        self,
        files: list[ConvertedFile],
    ) -> list[list[ChunkedFileProperties]]:
        """批量切分多个文件。

        Args:
            files: 转换后的文件列表。

        Returns:
            每个文件的 chunk 列表（外层列表按入参顺序）。
        """
        return [self.chunk(f) for f in files]
