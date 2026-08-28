"""LLM 客户端工厂。

两种工厂函数，各自从配置创建独立的 LLMClient 实例：
- ``create_llm(cfg)``  — 文本对话
- ``create_vlm(cfg)`` — 多模态图片理解

配置类型统一用新设计的 LLMConfig / VLMConfig。
"""

from wiki_agent.config import LLMConfig, RetryConfig, VLMConfig
from wiki_agent.llm.llm import LLMClient
from wiki_agent.log import get_logger

logger = get_logger("LLM_FACTORY")


def create_llm(cfg: LLMConfig, retry_config: RetryConfig | None = None) -> LLMClient:
    """从 LLMConfig 创建纯文本 LLM 客户端。

    Args:
        cfg: LLM 配置。

    Returns:
        可用的 LLMClient 实例。
    """
    client = LLMClient(cfg, retry_config)
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
    client = LLMClient(cfg, retry_config)  # VLMConfig 继承 LLMConfig——结构化子类型
    logger.info("VLM 客户端创建: model=%s, base_url=%s", cfg.model_id, cfg.base_url)
    return client
