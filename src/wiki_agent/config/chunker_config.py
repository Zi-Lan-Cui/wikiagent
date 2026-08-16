from pydantic import BaseModel
import os


class BaseChunkerConfig(BaseModel):
    """文本切分基础配置。"""
    chunk_size: int = 500
    use_overlap: bool = True
    overlap_rate: float = 0.1


class TextChunkerConfig(BaseChunkerConfig):
    """纯文本切分配置。"""

    @classmethod
    def from_env(cls) -> "TextChunkerConfig":
        return cls(
            chunk_size=int(os.getenv("CHUNK_SIZE") or "500"),
            use_overlap=os.getenv("CHUNK_USE_OVERLAP", "true").lower() != "false",
            overlap_rate=float(os.getenv("CHUNK_OVERLAP_RATE") or "0.1"),
        )
