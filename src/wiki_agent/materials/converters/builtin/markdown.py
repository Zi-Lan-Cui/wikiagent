"""Markdown 直读：已是目标格式，只做图片 caption 与资产化。"""

from __future__ import annotations

import os
from pathlib import Path

from wiki_agent.log import get_logger

from ...loader import RawFileProperties
from ..base import BaseConverter, ConvertedFile
from ..captions import CaptionRunner

logger = get_logger("CONVERTER")

_MD_EXTS = {"md", "markdown"}


class MarkdownConverter(BaseConverter):
    """直读 .md/.markdown；captioner 有效时对正文图片做 VLM caption 与资产化。"""

    def __init__(self, captioner: CaptionRunner | None = None):
        self._captioner = captioner

    def accepts(self, raw_file: RawFileProperties) -> bool:
        return raw_file.ext in _MD_EXTS

    async def convert(self, raw_file: RawFileProperties) -> ConvertedFile:
        text = Path(raw_file.path).read_text(encoding="utf-8")
        if self._captioner is not None and self._captioner.enabled:
            count = self._captioner.count(text)
            logger.info(
                "  读取 Markdown: %s (%d chars%s)",
                raw_file.name,
                len(text),
                f", {count} 张图片待 caption" if count else "",
            )
            if count:
                text = await self._captioner.apply(text, os.path.dirname(str(raw_file.path)))
        else:
            logger.info("  读取 Markdown: %s (%d chars)", raw_file.name, len(text))
        return ConvertedFile.from_raw(raw_file, text)
