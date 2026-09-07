"""LLM 客户端工厂。

两种工厂函数，各自从配置创建独立的 LLMClient 实例：
- ``create_llm(cfg)``  — 文本对话
- ``create_vlm(cfg)`` — 多模态图片理解

配置类型统一用新设计的 LLMConfig / VLMConfig。
"""

from threading import Lock

from wiki_agent.config import LLMConfig, RetryConfig, VLMConfig
from wiki_agent.llm.llm import LLMClient
from wiki_agent.llm.rate_limit import RequestLimiter
from wiki_agent.log import get_logger

logger = get_logger("LLM_FACTORY")
_limiters_lock = Lock()
_limiters: dict[str, tuple[tuple[int, int, int], RequestLimiter]] = {}


def _process_limiter(scope: str, cfg: LLMConfig) -> RequestLimiter:
    """为同一进程内的 LLM/VLM 客户端复用各自的全局限流器。"""
    signature = (cfg.max_concurrency, cfg.requests_per_minute, cfg.tokens_per_minute)
    with _limiters_lock:
        current = _limiters.get(scope)
        if current is None or current[0] != signature:
            current = (
                signature,
                RequestLimiter(
                    max_concurrency=cfg.max_concurrency,
                    requests_per_minute=cfg.requests_per_minute,
                    tokens_per_minute=cfg.tokens_per_minute,
                ),
            )
            _limiters[scope] = current
        return current[1]


def create_llm(cfg: LLMConfig, retry_config: RetryConfig | None = None) -> LLMClient:
    """从 LLMConfig 创建纯文本 LLM 客户端。

    Args:
        cfg: LLM 配置。

    Returns:
        可用的 LLMClient 实例。
    """
    client = LLMClient(cfg, retry_config, _process_limiter("llm", cfg))
    logger.info("LLM 客户端创建: model=%s, base_url=%s", cfg.model_id, cfg.base_url)
    return client


def create_vlm(cfg: VLMConfig, retry_config: RetryConfig | None = None) -> LLMClient:
    """从 VLMConfig 创建多模态 VLM 客户端。

    返回的是同一个 ``LLMClient`` 类型——
    多模态能力通过 ``Message(images=[...])`` 驱动，客户端本身不区分。

    Args:
        cfg: VLM 配置（继承 LLMConfig）。

    Returns:
        可用的 LLMClient 实例（构造上等价于 LLM 客户端）。
    """
    client = LLMClient(
        cfg,
        retry_config,
        _process_limiter("vlm", cfg),
    )
    logger.info("VLM 客户端创建: model=%s, base_url=%s", cfg.model_id, cfg.base_url)
    return client
