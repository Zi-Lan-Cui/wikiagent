"""配置包——统一入口。

    from wiki_agent.config import load_config
    cfg = load_config(project_root=...)
    cfg.llm / cfg.vlm / cfg.agent / cfg.logging / cfg.paths / cfg.mcp
"""

from wiki_agent.config.root import (
    AgentConfig,
    LLMConfig,
    LoggingConfig,
    McpConfig,
    McpServerConfig,
    PathsConfig,
    RootConfig,
    SseMcpTransport,
    StdioMcpTransport,
    StreamableHttpTransport,
    VLMConfig,
    load_config,
)
from wiki_agent.config.embedding_config import BaseEmbeddingConfig, DashScopeEmbeddingConfig
from wiki_agent.config.storage_config import BaseStorageConfig, QdrantStorageConfig
from wiki_agent.config.chunker_config import BaseChunkerConfig, TextChunkerConfig
