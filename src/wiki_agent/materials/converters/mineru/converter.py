"""MinerU 文档转换器——将 PDF/DOCX/PPTX 等转为结构化 Markdown。

两阶段管线:
  1. 生成 Markdown: MinerU 解析（PDF/DOCX/PPTX/XLSX）或直接读取（.md）
  2. 图片 caption: 提取 ``![]()`` → VLM 描述 → 回填 alt text
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.log import get_logger

from ...loader import RawFileProperties
from ..base import BaseConverter, ConvertedFile
from ..captions import CaptionRunner

if TYPE_CHECKING:
    from wiki_agent.llm.client import LLMClient

logger = get_logger("MINERU_CONVERTER")

# 需要 MinerU 解析的格式
_NEEDS_PARSING = {
    "pdf",
    "docx",
    "doc",
    "pptx",
    "ppt",
    "xlsx",
    "xls",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "bmp",
    "webp",
}

_IS_ALREADY_MARKDOWN = {"md", "markdown"}


class MinerUConverter(BaseConverter):
    """文件 → Markdown + 图片 caption。"""

    def __init__(
        self,
        *,
        backend: str = "pipeline",
        lang: str = "en",
        formula_enable: bool = True,
        table_enable: bool = True,
        llm: LLMClient | None = None,
        caption_images: bool = False,
        assets_dir: str | Path | None = None,
    ):
        self._backend = backend
        self._lang = lang
        self._formula_enable = formula_enable
        self._table_enable = table_enable
        self._captioner = CaptionRunner(llm if caption_images else None, assets_dir=assets_dir)

    def accepts(self, raw_file: RawFileProperties) -> bool:
        """扩展名在支持表内。"""
        # Markdown 路径不触发 mineru import，未装 extra 也可用
        return raw_file.ext in _IS_ALREADY_MARKDOWN or raw_file.ext in _NEEDS_PARSING

    async def convert(self, raw_file: RawFileProperties) -> ConvertedFile:
        """转换单个文件——已有内容直接用，否则按类型转换。"""
        if raw_file.content:
            return ConvertedFile.from_raw(raw_file, raw_file.content)

        file_path = str(raw_file.path)
        logger.debug("convert 开始: %s (ext=%s)", raw_file.name, raw_file.ext)

        if raw_file.ext in _IS_ALREADY_MARKDOWN:
            # builtin 优先链里到不了这里；保留为 backend=builtin 之外的一致性兜底
            markdown = self._read_markdown_file(file_path)
            if self._captioner.enabled:
                markdown = await self._captioner.apply(markdown, os.path.dirname(file_path))
            logger.info("  读取 Markdown: %s (%d chars)", raw_file.name, len(markdown))
            return ConvertedFile.from_raw(raw_file, markdown)

        logger.info("  MinerU 解析: %s (backend=%s)", raw_file.name, self._backend)
        markdown = await self._mineru_parse(file_path)
        logger.info("  MinerU 完成: %s → %d chars", raw_file.name, len(markdown))
        return ConvertedFile.from_raw(raw_file, markdown)

    @staticmethod
    def _read_markdown_file(file_path: str) -> str:
        """读取 .md、.markdown 全文。"""
        with open(file_path, encoding="utf-8") as fh:
            return fh.read()

    async def _mineru_parse(self, file_path: str) -> str:
        """把 _mineru_parse_sync 投递到 executor 线程：MinerU 的临时图片
        必须在 caption 完成前不被事件循环侧清理。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._mineru_parse_sync, file_path)

    def _mineru_parse_sync(self, file_path: str) -> str:
        """同步主体（executor 线程内执行）：do_parse → 读结果 →
        caption（图片在 tmpdir 里）→ tmpdir 退出即销毁。"""
        # 延迟 import：materials 包被导入时不应连带加载 mineru
        from mineru.cli.common import do_parse, read_fn

        pdf_bytes = read_fn(file_path)
        file_name = os.path.basename(file_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            do_parse(
                output_dir=tmpdir,
                pdf_file_names=[file_name],
                pdf_bytes_list=[pdf_bytes],
                p_lang_list=[self._lang],
                backend=self._backend,
                parse_method="auto",
                formula_enable=self._formula_enable,
                table_enable=self._table_enable,
            )

            markdown, images_dir = self._find_result_in_dir(tmpdir)

            if self._captioner.enabled and images_dir:
                markdown = self._captioner.apply_sync(markdown, images_dir)

            return markdown

    @staticmethod
    def _find_result_in_dir(tmpdir: str) -> tuple[str, str]:
        """遍历 tmpdir，返回 (markdown_text, images_dir_path)。"""
        markdown = ""
        images_dir = ""
        for root, _, filenames in os.walk(tmpdir):
            for fname in filenames:
                if fname.endswith(".md"):
                    path = os.path.join(root, fname)
                    with open(path, encoding="utf-8") as fh:
                        markdown = fh.read()
            if os.path.basename(root) == "images":
                images_dir = root
        return markdown, images_dir
