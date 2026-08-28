"""日志模块——统一命名空间 + 单点配置。

设计:
- 所有 logger 挂在 ``wiki_agent`` 根下（get_logger 自动补前缀）
- 根放行一切（DEBUG），handler 各自过滤:
    - console handler: 默认 WARNING+（INFO 是内部细节，不打扰用户）
    - file handler:   --debug 时全量 DEBUG 写文件
- ``configure_logging`` 是唯一配置入口，CLI 等入口启动时调用一次
"""

import logging
import sys
from logging import Formatter

ROOT_NAME = "wiki_agent"


class ColorFormatter(Formatter):
    COLORS = {
        logging.DEBUG: "\033[90m",  # 灰色
        logging.INFO: "\033[32m",  # 绿色
        logging.WARNING: "\033[33m",  # 黄色
        logging.ERROR: "\033[31m",  # 红色
        logging.CRITICAL: "\033[41;37m",  # 白字红底
    }

    RESET = "\033[0m"

    def __init__(self, *args, use_color: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_color = use_color

    def format(self, record):
        line = super().format(record)
        if self.use_color:
            color = self.COLORS.get(record.levelno)
            if color:
                return color + line + self.RESET
        return line


def configure_logging(
    *,
    console_level: int = logging.WARNING,
    file_path: str | None = None,
    file_level: int = logging.DEBUG,
) -> None:
    """配置根 logger——全局只调用一次（CLI 入口）。

    Args:
        console_level: 终端显示级别。默认 WARNING+，INFO 属于
            内部细节不打扰用户。
        file_path: 指定则全量日志（file_level）写入该文件。
        file_level: 文件日志级别。
    """
    root = logging.getLogger(ROOT_NAME)
    root.setLevel(logging.DEBUG)  # 根放行一切，handler 各自过滤
    root.handlers.clear()
    root.propagate = False

    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

    # 终端 handler（stderr——stdout 留给用户内容）
    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(console_level)
    use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    console.setFormatter(ColorFormatter(fmt, use_color=use_color))
    root.addHandler(console)

    # 文件 handler（--debug）
    if file_path:
        fh = logging.FileHandler(file_path, encoding="utf-8")
        fh.setLevel(file_level)
        fh.setFormatter(Formatter(fmt))
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    """获取挂在 ``wiki_agent`` 命名空间下的 logger。

    用法不变: ``get_logger("LLM_FACTORY")`` → ``wiki_agent.LLM_FACTORY``。
    级别由根配置统一控制，调用方不关心细节。

    Args:
        name: logger 名（自动补 wiki_agent. 前缀）。

    Returns:
        配置好的 logger 实例。
    """
    if not name.startswith(ROOT_NAME + "."):
        name = f"{ROOT_NAME}.{name}"
    return logging.getLogger(name)
