"""结构化事件日志——JSON lines 落文件，机器可消费。

设计边界:
- 只记录事件（谁、何时、做了什么、结果、耗时），不存业务数据
- 不阻塞主流程
- 可被 jq 等 JSONL 工具直接消费

事件格式::

    {"ts": "...", "trace_id": "...", "span": "llm_call", "dur_ms": 1234,
     "model": "deepseek-v4-flash", "tokens": {"prompt": 1000, "completion": 200},
     "status": "ok"}
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from wiki_agent.log.logger import get_logger
from wiki_agent.log.tracer import current_trace_id

logger = get_logger("EVENTS")

# 模块级单例——进程内唯一事件日志
_event_log: EventLog | None = None


class EventLog:
    """JSON lines 事件日志。"""

    def __init__(self, path: Path):
        """初始化事件日志。

        Args:
            path: events.jsonl 文件路径（父目录自动创建）。
        """
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.dropped = 0
        """写盘失败的计数——机器通道丢事件的可观测性（终端 run 后
        日志最后一行会打 WARNING 汇总，静默丢失变成可见信号）。"""

    def emit(self, event: str, **fields: Any) -> None:
        """记录一条事件。

        trace_id 自动从 contextvars 取；写盘失败不抛异常，只计数
        （日志永远不能让主流程崩）。

        Args:
            event: 事件名（如 "llm_call"）。
            **fields: 附加字段（模型/耗时/token 等，随记录写入）。
        """
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "trace_id": current_trace_id() or "-",
            "event": event,
        }
        record.update(fields)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # 日志永远不能让主流程崩——静默失败但计数：
            # 机器通道丢事件必须可见（run 结束有 WARNING 汇总）
            self.dropped += 1
            if self.dropped % 10 == 1:  # 每 10 条警告一次，不刷屏
                logger.warning("事件写盘失败（已丢 %d 条）: %s", self.dropped, self._path)


def setup_event_log(path: str | Path | None) -> None:
    """启用结构化事件日志。

    Args:
        path: events.jsonl 路径；None 表示关闭事件记录。
    """
    global _event_log
    _event_log = EventLog(Path(path)) if path else None


def emit_event(event: str, **fields: Any) -> None:
    """记录结构化事件（未 setup 时 no-op）。

    Args:
        event: 事件名。
        **fields: 附加字段。
    """
    if _event_log is not None:
        _event_log.emit(event, **fields)
