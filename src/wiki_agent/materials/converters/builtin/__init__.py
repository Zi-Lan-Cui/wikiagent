"""builtin 后端：轻量纯 Python 的转换器集合，不依赖任何重型解析库。"""

from .images import ImageConverter
from .markdown import MarkdownConverter
from .spreadsheets import XlsxConverter

__all__ = ["ImageConverter", "MarkdownConverter", "XlsxConverter"]
