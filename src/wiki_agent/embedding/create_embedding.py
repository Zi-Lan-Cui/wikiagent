from wiki_agent.embedding.dashscope_embedding import DashScopeEmbedding
from wiki_agent.config import BaseEmbeddingConfig

def create_embedding_model(config:BaseEmbeddingConfig):
    if config.method=="dashscope":
        embedding_model=DashScopeEmbedding()
        embedding_model.initialize(config)
        return embedding_model
    raise ValueError(f"不支持的嵌入方法{config.method}")
    