from wiki_agent.ingestion.chunker import ChunkedFileProperties, Chunker
from wiki_agent.ingestion.converter import Converter, ConvertedFile
from wiki_agent.ingestion.data_loader import RawFileProperties
from wiki_agent.log import get_logger

logger = get_logger("PROCESSOR")


class Processor:
    """将原始文件转为 chunk 的编排器。

    DataLoader → Converter（图像/富文档→文本）→ Chunker → chunks
    """

    def __init__(
        self,
        *,
        converter: Converter | None = None,
        chunker: Chunker | None = None,
    ):
        self._converter = converter
        self._chunker = chunker or Chunker()

    async def abatch_process(
        self, raw_files: list[RawFileProperties],
    ) -> list[ChunkedFileProperties]:
        """异步批量处理：先转换，再 chunk。"""
        if self._converter:
            converted = await self._converter.batch_convert(raw_files)
        else:
            converted = [ConvertedFile.from_raw(f, f.content) for f in raw_files]
        return self._batch_chunk(converted)

    def batch_process(
        self, raw_files: list[RawFileProperties],
    ) -> list[ChunkedFileProperties]:
        """同步批量 chunk（假定所有文件 content 已就绪）。"""
        converted = [ConvertedFile.from_raw(f, f.content) for f in raw_files]
        return self._batch_chunk(converted)

    def _batch_chunk(
        self, files: list[ConvertedFile],
    ) -> list[ChunkedFileProperties]:
        all_chunks: list[ChunkedFileProperties] = []
        for file in files:
            all_chunks.extend(self._chunker.chunk(file))
        return all_chunks
