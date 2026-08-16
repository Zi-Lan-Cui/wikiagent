from pydantic import BaseModel
import os


class BaseEmbeddingConfig(BaseModel):
    """嵌入模型基础配置，使用 method 字段区分后端。"""
    method: str = ""


class DashScopeEmbeddingConfig(BaseEmbeddingConfig):
    """阿里云 DashScope 嵌入模型配置。"""
    model_name: str = "text-embedding-v4"
    api_key: str = ""
    base_url: str = ""
    embedding_dim: int = 256

    @classmethod
    def from_env(cls) -> "DashScopeEmbeddingConfig":
        return cls(
            method=os.getenv("EMBEDDING_METHOD") or "dashscope",
            model_name=os.getenv("EMBEDDING_MODEL_NAME") or "text-embedding-v4",
            api_key=os.getenv("EMBEDDING_API_KEY") or "",
            base_url=os.getenv("EMBEDDING_BASE_URL") or "",
            embedding_dim=int(os.getenv("EMBEDDING_DIM") or "256"),
        )
