"""MinerU 文档转换器——将 PDF/DOCX/PPTX 等转为结构化 Markdown。

两阶段管线:
  1. 生成 Markdown: MinerU 解析（PDF/DOCX/PPTX/XLSX）或直接读取（.md）
  2. 图片 caption: 提取 ``![]()`` → VLM 描述 → 回填 alt text

已完成:
  ✅ pipeline 后端（CPU/GPU 均可）
  ✅ .md 文件直接读取 + 相对路径图片 caption
  ✅ PDF/DOCX 的 MinerU 解析 + tmp images caption（用完即清）
  ✅ 并发 caption（asyncio.gather）

未完成:
  - caption 缓存: 相同图片（SHA256）跨文档复用
  - caption 降级: LLM 不可用时保留空 alt text
  - 表格图片 caption: 描述表格内容而非"这是一张表格"
  - vlm-engine 后端: CUDA 版本不匹配时自动回退 pipeline
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from wiki_agent.ingestion.converter.base import BaseConverter, ConvertedFile
from wiki_agent.ingestion.data_loader import RawFileProperties
from wiki_agent.llm.llm import LLMClient
from wiki_agent.message import Message
from wiki_agent.log import get_logger

logger = get_logger("MINERU_CONVERTER")

# ── 可选依赖 ──────────────────────────────────────────────

try:
    from mineru.cli.common import do_parse as _mineru_do_parse
    from mineru.cli.common import read_fn as _mineru_read_fn
    _HAS_MINERU = True
except ImportError:
    _HAS_MINERU = False

# ── 常量 ──────────────────────────────────────────────────

# 需要 MinerU 解析的格式（非 Markdown、非纯文本）
_NEEDS_PARSING = {"pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls",
                  "png", "jpg", "jpeg", "gif", "bmp", "webp"}

# 已是 Markdown 格式——直接读文件，不需要 MinerU
_IS_ALREADY_MARKDOWN = {"md", "markdown"}

# 所有图片引用: ![任意alt](path)
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


# ════════════════════════════════════════════════════════════
#  MinerUConverter
# ════════════════════════════════════════════════════════════

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
        self._llm = llm
        self._caption_enabled = caption_images and llm is not None
        # 图片资产目录（wiki/assets）——caption 时复制图片并回填路径，
        # wiki 页面里的 ![](assets/x.png) 自包含可渲染（TODO 图片路径修正）。
        # None = 不回填（只 caption 不改路径）。
        self._assets_dir = Path(assets_dir) if assets_dir else None

    # ── BaseConverter ─────────────────────────────────────

    def accepts(self, raw_file: RawFileProperties) -> bool:
        """是否支持该文件（MinerU 可用且扩展名在支持表内）。

        Args:
            raw_file: 原始文件属性。

        Returns:
            True 表示支持处理。
        """
        return _HAS_MINERU and (
            raw_file.ext in _NEEDS_PARSING or
            raw_file.ext in _IS_ALREADY_MARKDOWN
        )

    async def convert(self, raw_file: RawFileProperties) -> ConvertedFile:
        """转换单个文件——已有内容直接用，否则按类型转换。

        Args:
            raw_file: 原始文件属性。

        Returns:
            转换后的 ConvertedFile。
        """
        if raw_file.content:
            return ConvertedFile.from_raw(raw_file, raw_file.content)

        file_path = str(raw_file.path)
        logger.debug("convert 开始: %s (ext=%s)", raw_file.name, raw_file.ext)

        if raw_file.ext in _IS_ALREADY_MARKDOWN:
            markdown = self._read_markdown_file(file_path)
            img_hint = ""
            if self._caption_enabled:
                img_count = len(_RE_IMAGE.findall(markdown))
                if img_count:
                    img_hint = f", {img_count} 张图片待 caption"
            logger.info("  读取 Markdown: %s (%d chars%s)", raw_file.name, len(markdown), img_hint)
            if self._caption_enabled:
                base_dir = os.path.dirname(file_path)
                markdown = await self._caption_async(markdown, base_dir)
            return ConvertedFile.from_raw(raw_file, markdown)

        # PDF/DOCX/图片 → MinerU 解析（内部完成 caption）
        logger.info("  MinerU 解析: %s (backend=%s)", raw_file.name, self._backend)
        markdown = await self._mineru_parse(file_path)
        logger.info("  MinerU 完成: %s → %d chars", raw_file.name, len(markdown))
        return ConvertedFile.from_raw(raw_file, markdown)

    # ════════════════════════════════════════════════════════
    #  步骤 1: 生成 Markdown
    # ════════════════════════════════════════════════════════

    # ── 直接读取 .md ──────────────────────────────────────

    @staticmethod
    def _read_markdown_file(file_path: str) -> str:
        """读取 .md / .markdown 文件。

        Args:
            file_path: 文件路径。

        Returns:
            文件内容。
        """
        with open(file_path, encoding="utf-8") as fh:
            return fh.read()

    # ── MinerU 解析 ───────────────────────────────────────

    async def _mineru_parse(self, file_path: str) -> str:
        """MinerU 解析 → 同步 caption → 返回 Markdown。

        在子线程运行，确保 MinerU 的临时图片在 caption 完成前不被清理。

        Args:
            file_path: 文件路径。

        Returns:
            转换后的 Markdown。
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._mineru_parse_sync, file_path)

    def _mineru_parse_sync(self, file_path: str) -> str:
        """MinerU 解析（同步，在子线程运行）。

        do_parse → 读结果 → caption（图片在 tmpdir 里）→ tmpdir 销毁。

        Args:
            file_path: 文件路径。

        Returns:
            转换后的 Markdown。
        """
        pdf_bytes = _mineru_read_fn(file_path)
        file_name = os.path.basename(file_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            _mineru_do_parse(
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

            if self._caption_enabled and images_dir:
                markdown = self._caption_sync(markdown, images_dir)

            return markdown

    @staticmethod
    def _find_result_in_dir(tmpdir: str) -> tuple[str, str]:
        """遍历 tmpdir，返回 (markdown_text, images_dir_path)。

        Args:
            tmpdir: MinerU 输出目录。

        Returns:
            (Markdown 文本, images 目录路径)。
        """
        markdown = ""
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

    # ════════════════════════════════════════════════════════
    #  步骤 2: Caption（给 ![]() 填 alt text）
    # ════════════════════════════════════════════════════════

    # ── 异步入口（主 event loop） ─────────────────────────

    async def _caption_async(self, markdown: str, images_base_dir: str) -> str:
        """从 ``images_base_dir`` 解析 ``![]()`` 中的路径，并发 caption。

        Args:
            markdown: 原始 Markdown。
            images_base_dir: 图片基准目录。

        Returns:
            回填 caption 后的 Markdown。
        """
        return await self._apply_captions(markdown, images_base_dir)

    # ── 同步入口（子线程） ────────────────────────────────

    def _caption_sync(self, markdown: str, images_base_dir: str) -> str:
        """在子线程中运行异步 caption（new_event_loop）。

        Args:
            markdown: 原始 Markdown。
            images_base_dir: 图片基准目录。

        Returns:
            回填 caption 后的 Markdown。
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._apply_captions(markdown, images_base_dir)
            )
        finally:
            loop.close()

    # ── 核心: 并发 caption + 回填 ─────────────────────────

    async def _apply_captions(self, markdown: str, images_base_dir: str) -> str:
        """找出所有 ``![]()``，并发 VLM caption，回填到 Markdown。

        ``images_base_dir`` 是解析相对路径时的基准目录。
        - 对于 .md : 源文件所在目录
        - 对于 PDF : MinerU 临时 images/ 目录

        Args:
            markdown: 原始 Markdown。
            images_base_dir: 图片基准目录。

        Returns:
            回填 caption 后的 Markdown。
        """
        matches = list(_RE_IMAGE.finditer(markdown))
        if not matches:
            return markdown

        logger.info("  caption 开始: %d 张图片", len(matches))

        # 并发为所有图片生成 caption
        new_parts = await asyncio.gather(*[
            self._caption_one_match(m, images_base_dir)
            for m in matches
        ])

        # 从后往前替换，避免偏移
        result = markdown
        for m, new_text in reversed([(m, t) for m, t in zip(matches, new_parts)]):
            result = result[:m.start()] + new_text + result[m.end():]

        captioned = sum(1 for t in new_parts if not t.startswith("!["))
        logger.info("  caption 完成: %d/%d 张有描述", captioned, len(matches))
        return result

    async def _caption_one_match(
        self, match: re.Match, images_base_dir: str,
    ) -> str:
        """处理单个 ``![]()`` 匹配——解析路径 → VLM → 返回替换文本。

        assets_dir 配置时: 图片复制到 wiki/assets/（内容 hash 命名去重），
        回填 assets 相对路径——wiki 页面自包含可渲染（TODO 图片路径修正）。

        Args:
            match: 单个 ``![]()`` 匹配。
            images_base_dir: 图片基准目录。

        Returns:
            替换后的文本（VLM 失败/路径无法解析时原样保留）。
        """
        rel_path = match.group(1)

        # 解析实际文件路径
        image_path = self._resolve_image_path(rel_path, images_base_dir)
        if image_path is None:
            return match.group(0)

        # VLM 描述
        description = await self._vlm_describe_image(image_path)
        if not description:
            return match.group(0)

        # 图片资产化——caption 时复制（MinerU 临时目录用完即清，这是唯一时机）
        final_path = rel_path
        if self._assets_dir is not None:
            final_path = self._copy_to_assets(image_path)
        return f"![{description}]({final_path})"

    def _copy_to_assets(self, image_path: str) -> str:
        """复制图片到 assets/，返回相对 wiki 的路径。

        内容 hash 命名——天然去重。复制失败返回原始相对路径
        （资产化失败 ≠ caption 失败——页面仍可用原始引用，图片
        位置不变时能解析）。代价是页面不再自包含（图片依赖源目录），
        scan 的 assets 检查会发现。

        Args:
            image_path: 源图片路径。

        Returns:
            wiki 相对路径（assets/<hash>.<ext>）。
        """
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = Path(image_path).read_bytes()
        except OSError:
            logger.warning("  图片复制失败（读不到），保留原始引用: %s", image_path)
            return os.path.basename(image_path)
        digest = hashlib.sha256(data).hexdigest()[:16]
        ext = Path(image_path).suffix.lower() or ".png"
        target = self._assets_dir / f"{digest}{ext}"
        if not target.exists():
            target.write_bytes(data)
        # 路径相对 wiki 根（assets 在 wiki/ 下）
        return f"assets/{target.name}"

    @staticmethod
    def _resolve_image_path(rel_path: str, base_dir: str) -> str | None:
        """解析相对路径为绝对路径。

        回退: 只取文件名（MinerU 的 images/ 扁平结构）。

        Args:
            rel_path: 相对路径。
            base_dir: 基准目录。

        Returns:
            存在的绝对路径；无法解析返回 None。
        """
        candidate = os.path.join(base_dir, rel_path)
        candidate = os.path.join(base_dir, rel_path)
        if os.path.exists(candidate):
            return candidate
        candidate = os.path.join(base_dir, os.path.basename(rel_path))
        if os.path.exists(candidate):
            return candidate
        return None

    # ── VLM 调用 ──────────────────────────────────────────

    async def _vlm_describe_image(self, image_path: str) -> str:
        """加载图片 → base64 → VLM → 返回描述文本。

        Args:
            image_path: 图片路径。

        Returns:
            描述文本（读图失败/VLM 失败返回空串）。
        """
        try:
            with open(image_path, "rb") as fh:
                image_b64 = base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            return ""

        try:
            response = await self._llm.async_invoke(
                [Message(
                    role="user",
                    content=(
                        "用一句中文描述图片。规则:\n"
                        "- 图表类: 先判断类型（柱状图/折线图/流程图/架构图），再说「对比了什么」或「展示了什么」\n"
                        "- 公式/板书/截图: 描述主题和关键信息\n"
                        "- 自然图像: 描述场景和主体\n"
                        "- 15-30字，只输出描述，不要「这张图片」「图中」等前缀"
                    ),
                    images=[image_b64],
                )],
                max_tokens=200,
            )
            return response.content.strip()
        except Exception as exc:
            logger.warning("VLM 失败 %s: %s", os.path.basename(image_path), exc)
            return ""
