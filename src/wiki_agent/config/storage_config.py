from pydantic import BaseModel, Field
from typing import List
import os


class BaseStorageConfig(BaseModel):
    """向量存储基础配置，使用 backend_name 字段区分后端。"""
    backend_name: str = ""


class QdrantStorageConfig(BaseStorageConfig):
    """Qdrant 向量数据库配置。"""
    base_url: str = ""
    api_key: str = ""
    embedding_dim: int = 384
    vector_distance_method: str = "Cosine"
    collections: List[str] = Field(default_factory=list)

    @classmethod
    def from_env(cls) -> "QdrantStorageConfig":
        collections_raw = os.getenv("QDRANT_COLLECTION") or ""
        collections = [c.strip() for c in collections_raw.split(",") if c.strip()]

        return cls(
            backend_name=os.getenv("STORAGE_BACKEND") or "qdrant",
            base_url=os.getenv("STORAGE_BASE_URL") or "",
            api_key=os.getenv("STORAGE_API_KEY") or "",
            embedding_dim=int(os.getenv("QDRANT_VECTOR_SIZE") or "384"),
            vector_distance_method=os.getenv("VECTOR_DISTANCE_METHOD") or "Cosine",
            collections=collections,
        )
