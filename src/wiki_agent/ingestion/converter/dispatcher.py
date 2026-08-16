"""转换器分发层。

按注入顺序遍历 converter，首个 ``accepts()`` 返回 True 的负责处理。
每个 ``BaseConverter`` 子类是一套完整的文件→文本实现，内部自己处理各模态。
"""

from __future__ import annotations

from wiki_agent.ingestion.converter.base import BaseConverter, ConvertedFile
from wiki_agent.ingestion.converter.mineru_converter import MinerUConverter
from wiki_agent.ingestion.data_loader import RawFileProperties
from wiki_agent.log import get_logger

logger = get_logger("CONVERTER")


class Converter:
    """文件 → 文本 的分发器。

    注入 converter 列表，按顺序匹配，首个 ``accepts()`` 返回 True 的获胜。
    默认注册 MinerUConverter。
    """

    def __init__(self, converters: list[BaseConverter] | None = None):
        self._converters = converters or [MinerUConverter()]

    async def convert(self, raw_file: RawFileProperties) -> ConvertedFile:
        for conv in self._converters:
            if conv.accepts(raw_file):
                return await conv.convert(raw_file)
        logger.warning(f"没有 converter 支持 {raw_file.name}（ext={raw_file.ext}）")
        return ConvertedFile.from_raw(raw_file, "")

    async def batch_convert(
        self, raw_files: list[RawFileProperties],
    ) -> list[ConvertedFile]:
        collected: list[tuple[int, ConvertedFile]] = []
        for index, raw_file in enumerate(raw_files):
            collected.append((index, await self.convert(raw_file)))
        collected.sort(key=lambda entry: entry[0])
        return [conv for _, conv in collected]
