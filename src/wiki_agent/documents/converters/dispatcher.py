"""转换器分发层：按注入顺序取首个 ``accepts()`` 为 True 的 converter。"""

from __future__ import annotations

from wiki_agent.documents.converters.base import BaseConverter, ConvertedFile
from wiki_agent.documents.converters.mineru_converter import MinerUConverter
from wiki_agent.documents.loader import RawFileProperties
from wiki_agent.log import get_logger

logger = get_logger("CONVERTER")


class Converter:
    """文件 → 文本 的分发器。

    默认注册 MinerUConverter；每个 converter 是一套完整的文件→文本实现。
    """

    def __init__(self, converters: list[BaseConverter] | None = None):
        self._converters = converters or [MinerUConverter()]

    async def convert(self, raw_file: RawFileProperties) -> ConvertedFile:
        """按顺序匹配首个支持的 converter 并转换。

        Args:
            raw_file: 原始文件属性。

        Returns:
            转换后的 ConvertedFile（无 converter 支持时返回空文本）。
        """
        for conv in self._converters:
            if conv.accepts(raw_file):
                return await conv.convert(raw_file)
        logger.warning(f"没有 converter 支持 {raw_file.name}（ext={raw_file.ext}）")
        return ConvertedFile.from_raw(raw_file, "")
