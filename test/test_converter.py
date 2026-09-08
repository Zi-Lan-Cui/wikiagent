"""Converter 对 Markdown 的独立支持测试。"""

import asyncio
from pathlib import Path

from wiki_agent.documents.converters.mineru_converter import MinerUConverter
from wiki_agent.documents.loader import FileModality, RawFileProperties


def test_markdown_converted_by_direct_read(tmp_path: Path):
    """Markdown 走直接读取分支，不经 MinerU 解析。

    （mineru 现为必选依赖、顶层导入，"未装 mineru" 已不可模拟；
    此测试守护的不变量是 .md 由 ``_IS_ALREADY_MARKDOWN`` 直读、内容原样返回。）
    """
    path = tmp_path / "中文.md"
    path.write_text("# 标题\n\n正文", encoding="utf-8")
    raw = RawFileProperties(name="中文.md", ext="md", path=path, modality=FileModality.RICH)

    converter = MinerUConverter()
    assert converter.accepts(raw)

    converted = asyncio.run(converter.convert(raw))
    assert converted.content == "# 标题\n\n正文"
