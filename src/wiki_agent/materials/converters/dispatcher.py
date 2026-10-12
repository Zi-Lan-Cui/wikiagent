"""转换器分发层：按注入顺序取首个 ``accepts()`` 为 True 的 converter。

默认链由 builtin 实现（Markdown/图片/xlsx）打头，MinerU 只在
``parse_backend`` 允许且依赖已装时兜底 pdf/docx/pptx 的版面解析；
纯文本类即便装了 MinerU 也继续走 builtin。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.log import get_logger

from ..loader import RawFileProperties
from .base import BaseConverter, ConvertedFile
from .builtin import ImageConverter, MarkdownConverter, XlsxConverter
from .captions import CaptionRunner
from .mineru import MinerUConverter

if TYPE_CHECKING:
    from wiki_agent.llm.client import LLMClient

logger = get_logger("CONVERTER")


def mineru_installed() -> bool:
    """mineru 包是否可 import（不触发加载）。"""
    return importlib.util.find_spec("mineru") is not None


def build_default_chain(
    *,
    vlm: LLMClient | None = None,
    assets_dir: str | Path | None = None,
    backend: str = "auto",
) -> list[BaseConverter]:
    """按后端选择组装转换器链；backend ∈ auto|mineru|builtin。"""
    captioner = CaptionRunner(vlm, assets_dir=assets_dir)
    chain: list[BaseConverter] = [MarkdownConverter(captioner), ImageConverter(captioner), XlsxConverter()]
    if backend in ("auto", "mineru"):
        if mineru_installed():
            chain.append(MinerUConverter(llm=vlm, caption_images=vlm is not None, assets_dir=assets_dir))
        elif backend == "mineru":
            raise RuntimeError("parse_backend=mineru 需要安装可选依赖：uv sync --extra mineru")
    return chain


class Converter:
    """文件 → 文本 的分发器。"""

    def __init__(
        self,
        converters: list[BaseConverter] | None = None,
        *,
        vlm: LLMClient | None = None,
        assets_dir: str | Path | None = None,
        backend: str = "auto",
    ):
        self._converters = converters or build_default_chain(
            vlm=vlm, assets_dir=assets_dir, backend=backend
        )

    async def convert(self, raw_file: RawFileProperties) -> ConvertedFile:
        """按顺序匹配首个支持的 converter 并转换。

        Returns:
            转换结果；无 converter 支持时为空文本并告警。
        """
        for conv in self._converters:
            if conv.accepts(raw_file):
                return await conv.convert(raw_file)
        logger.warning(
            "没有 converter 支持 %s（ext=%s）；PDF/DOCX 等版面格式需装 mineru：uv sync --extra mineru",
            raw_file.name,
            raw_file.ext,
        )
        return ConvertedFile.from_raw(raw_file, "")
