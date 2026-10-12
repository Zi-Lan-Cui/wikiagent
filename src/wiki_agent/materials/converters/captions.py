"""Markdown 图片 caption 回填：![]() → VLM 描述 →（可选）资产化。

builtin 与 mineru 两条后端共用。caption 时把图片复制进 wiki/assets
（内容 hash 命名去重）并回填相对路径，使页面引用自包含——临时目录
里的原图随后即被清理，复制只能发生在 caption 时刻。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from wiki_agent.conversation import Message
from wiki_agent.log import get_logger

if TYPE_CHECKING:
    from wiki_agent.llm.client import LLMClient

logger = get_logger("CAPTION")

RE_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

DESCRIBE_PROMPT = (
    "用一句中文描述图片。规则:\n"
    "- 图表类: 先判断类型（柱状图/折线图/流程图/架构图），再说「对比了什么」或「展示了什么」\n"
    "- 公式/板书/截图: 描述主题和关键信息\n"
    "- 自然图像: 描述场景和主体\n"
    "- 15-30字，只输出描述，不要「这张图片」「图中」等前缀"
)


class CaptionRunner:
    """对 Markdown 中的图片引用做 VLM caption 与资产复制；无 llm 即整体禁用。"""

    def __init__(self, llm: LLMClient | None, *, assets_dir: str | Path | None = None):
        self._llm = llm
        # 图片资产目录（wiki/assets）：None 表示只 caption、不改写引用路径
        self._assets_dir = Path(assets_dir) if assets_dir else None

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    def count(self, markdown: str) -> int:
        """正文中的图片引用数（日志提示用）。"""
        return len(RE_IMAGE.findall(markdown))

    async def apply(self, markdown: str, images_base_dir: str) -> str:
        """找出所有 ``![]()``，并发 VLM caption 后回填。

        Args:
            markdown: 原始 Markdown。
            images_base_dir: 解析相对路径的基准目录——.md 为源文件所在
                目录，MinerU 解析结果为临时 images/ 目录。

        Returns:
            回填 caption 后的 Markdown；单图失败时该引用原样保留。
        """
        matches = list(RE_IMAGE.finditer(markdown))
        if not matches:
            return markdown

        logger.info("  caption 开始: %d 张图片", len(matches))
        new_parts = await asyncio.gather(*[self._one_match(m, images_base_dir) for m in matches])

        result = markdown
        # 倒序回填：先替换前段会使后段 match 的偏移失效
        for m, new_text in zip(reversed(matches), reversed(new_parts)):
            result = result[: m.start()] + new_text + result[m.end() :]

        captioned = sum(1 for m, t in zip(matches, new_parts) if t != m.group(0))
        logger.info("  caption 完成: %d/%d 张有描述", captioned, len(matches))
        return result

    def apply_sync(self, markdown: str, images_base_dir: str) -> str:
        """同步包装 apply：新建事件循环，须在无运行中 loop 的线程调用。"""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.apply(markdown, images_base_dir))
        finally:
            loop.close()

    async def ask_image(self, image_path: str, prompt: str, *, max_tokens: int = 200) -> str:
        """单图调用 VLM；读取失败、无 llm 或调用异常返回空串。"""
        if self._llm is None:
            return ""
        try:
            with open(image_path, "rb") as fh:
                image_b64 = base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            return ""
        try:
            response = await self._llm.async_invoke(
                [Message(role="user", content=prompt, images=[image_b64])],
                max_tokens=max_tokens,
            )
            return response.content.strip()
        except Exception as exc:
            logger.warning("VLM 失败 %s: %s", os.path.basename(image_path), exc)
            return ""

    def reference_for(self, image_path: str) -> str:
        """单图的自包含 Markdown 引用：资产化后路径可用才有意义，否则返回空。"""
        if self._assets_dir is None:
            return ""
        return f"![{Path(image_path).name}]({self._copy_to_assets(image_path)})"

    async def _one_match(self, match: re.Match, images_base_dir: str) -> str:
        rel_path = match.group(1)
        image_path = self._resolve_image_path(rel_path, images_base_dir)
        if image_path is None:
            return match.group(0)
        description = await self.ask_image(image_path, DESCRIBE_PROMPT, max_tokens=200)
        if not description:
            return match.group(0)
        final_path = rel_path
        if self._assets_dir is not None:
            final_path = self._copy_to_assets(image_path)
        return f"![{description}]({final_path})"

    def _copy_to_assets(self, image_path: str) -> str:
        """复制图片到 assets/，返回相对 wiki 的路径；失败时退回原文件名。"""
        assets_dir = self._assets_dir
        if assets_dir is None:
            return os.path.basename(image_path)
        assets_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = Path(image_path).read_bytes()
        except OSError:
            logger.warning("  图片复制失败（读不到），保留原始引用: %s", image_path)
            return os.path.basename(image_path)
        digest = hashlib.sha256(data).hexdigest()[:16]
        ext = Path(image_path).suffix.lower() or ".png"
        target = assets_dir / f"{digest}{ext}"
        if not target.exists():
            target.write_bytes(data)
        return f"assets/{target.name}"

    @staticmethod
    def _resolve_image_path(rel_path: str, base_dir: str) -> str | None:
        """相对路径 → 绝对路径；回退只取文件名（MinerU images/ 扁平结构）。"""
        candidate = os.path.join(base_dir, rel_path)
        if os.path.exists(candidate):
            return candidate
        candidate = os.path.join(base_dir, os.path.basename(rel_path))
        if os.path.exists(candidate):
            return candidate
        return None
