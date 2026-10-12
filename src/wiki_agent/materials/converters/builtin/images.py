"""独立图片 → VLM 转写 Markdown（扫描件式素材的 builtin 通道）。"""

from __future__ import annotations

from wiki_agent.log import get_logger

from ...loader import RawFileProperties
from ..base import BaseConverter, ConvertedFile
from ..captions import CaptionRunner

logger = get_logger("CONVERTER")

# svg 是文本矢量格式，VLM 不收件
_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}

_TRANSCRIBE_PROMPT = (
    "Transcribe the image content into Markdown, keeping the text in its"
    " original language. Rules:\n"
    "- Preserve heading levels, lists and tables (as Markdown tables)\n"
    "- Copy formulas as LaTeX; mark unreadable text with … , never fabricate\n"
    "- Output only the transcription body"
)


class ImageConverter(BaseConverter):
    """独立图片文件 → VLM 转写；有资产目录时首行保留自包含图片引用。"""

    def __init__(self, captioner: CaptionRunner):
        self._captioner = captioner

    def accepts(self, raw_file: RawFileProperties) -> bool:
        return raw_file.ext in _IMAGE_EXTS

    async def convert(self, raw_file: RawFileProperties) -> ConvertedFile:
        transcript = await self._captioner.ask_image(
            str(raw_file.path), _TRANSCRIBE_PROMPT, max_tokens=4000
        )
        parts: list[str] = []
        ref = self._captioner.reference_for(str(raw_file.path))
        if ref:
            parts.append(ref)
        if transcript:
            parts.append(transcript)
        logger.info("  图片转写: %s → %d chars", raw_file.name, len(transcript))
        return ConvertedFile.from_raw(raw_file, "\n\n".join(parts))
