"""日志层公开接口：logger 配置、trace 传播、结构化事件。"""

from wiki_agent.log.events import emit_event, setup_event_log
from wiki_agent.log.logger import configure_logging, get_logger
from wiki_agent.log.tracer import begin_trace, current_span_id, current_trace_id, span

__all__ = [
    "configure_logging",
    "get_logger",
    "begin_trace",
    "current_span_id",
    "span",
    "emit_event",
    "setup_event_log",
]
