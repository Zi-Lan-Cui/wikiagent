"""builtin 转换器链与后端选择测试：mineru 缺席时 md/图片/xlsx 照常工作。"""

import asyncio
from pathlib import Path

import pytest

from wiki_agent.materials.converters import Converter
from wiki_agent.materials.converters.builtin import ImageConverter, MarkdownConverter, XlsxConverter
from wiki_agent.materials.converters.captions import CaptionRunner
from wiki_agent.materials.converters.dispatcher import build_default_chain
from wiki_agent.materials.loader import FileModality, RawFileProperties


def _raw(path: Path, ext: str) -> RawFileProperties:
    return RawFileProperties(name=path.name, ext=ext, path=path, modality=FileModality.RICH)


def _chain_types(chain):
    return [type(item).__name__ for item in chain]


def test_markdown_direct_read(tmp_path: Path):
    """不变量：.md 由 MarkdownConverter 直读，内容原样返回。"""
    path = tmp_path / "中文.md"
    path.write_text("# 标题\n\n正文", encoding="utf-8")
    raw = _raw(path, "md")
    conv = MarkdownConverter()
    assert conv.accepts(raw)
    converted = asyncio.run(conv.convert(raw))
    assert converted.content == "# 标题\n\n正文"


def test_xlsx_to_markdown_tables(tmp_path: Path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "成绩"
    ws.append(["姓名", "分数|备注"])
    ws.append(["甲", 95])
    ws.append([None, "换\n行"])
    path = tmp_path / "表.xlsx"
    wb.save(path)

    raw = _raw(path, "xlsx")
    assert XlsxConverter().accepts(raw)
    content = asyncio.run(XlsxConverter().convert(raw)).content
    assert "### 成绩" in content
    assert "| 姓名 | 分数\\|备注 |" in content
    assert "|---|---|" in content
    assert "| 甲 | 95 |" in content
    assert "换 行" in content  # 换行压平，不毁表格


def test_image_accepts_raster_only(tmp_path: Path):
    """svg 是文本矢量、VLM 不收件，不在图片通道。"""
    conv = ImageConverter(captioner=CaptionRunner(llm=None))
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG")
    assert conv.accepts(_raw(png, "png"))
    svg = tmp_path / "b.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    assert not conv.accepts(_raw(svg, "svg"))


def test_chain_auto_prefers_builtin(monkeypatch):
    monkeypatch.setattr("wiki_agent.materials.converters.dispatcher.mineru_installed", lambda: True)
    assert _chain_types(build_default_chain(backend="auto")) == [
        "MarkdownConverter",
        "ImageConverter",
        "XlsxConverter",
        "MinerUConverter",
    ]


def test_backend_builtin_excludes_mineru():
    assert _chain_types(build_default_chain(backend="builtin")) == [
        "MarkdownConverter",
        "ImageConverter",
        "XlsxConverter",
    ]


def test_explicit_mineru_requires_dependency(monkeypatch):
    monkeypatch.setattr("wiki_agent.materials.converters.dispatcher.mineru_installed", lambda: False)
    with pytest.raises(RuntimeError, match="uv sync --extra mineru"):
        build_default_chain(backend="mineru")


def test_auto_without_mineru_leaves_pdf_unsupported(tmp_path: Path, monkeypatch):
    """extra 未装、backend=auto：pdf 无人认领，分发器返回空文本并告警。"""
    monkeypatch.setattr("wiki_agent.materials.converters.dispatcher.mineru_installed", lambda: False)
    conv = Converter(backend="auto")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    out = asyncio.run(conv.convert(_raw(pdf, "pdf")))
    assert out.content == ""
