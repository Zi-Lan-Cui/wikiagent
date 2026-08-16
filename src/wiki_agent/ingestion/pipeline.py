from abc import ABC, abstractmethod
from pathlib import Path

from wiki_agent.embedding import BaseEmbeddingModel, create_embedding_model
from wiki_agent.ingestion.data_loader import DataLoader
from wiki_agent.ingestion.processor import Processor
from wiki_agent.log import get_logger
from wiki_agent.storage import BaseStorage, StorageDocument

logger = get_logger("PIPELINE")


class BasePipeline(ABC):
    def __init__(self, name):
        self.name = name

    @abstractmethod
    async def run(self, files):
        """pipeline 具体执行函数。"""


class TextPipeline(BasePipeline):
    def __init__(
        self,
        name: str,
        data_loader: DataLoader,
        embedding_model: BaseEmbeddingModel,
        processor: Processor,
        storage: BaseStorage,
    ):
        super().__init__(name=name)
        self.embedding_model = embedding_model
        self.data_loader = data_loader
        self.processor = processor
        self.storage = storage

    async def run(
        self,
        collection_name: str,
        files: list[str | Path],
        base_path: str | Path = ".",
    ):
        summary = self.data_loader.load(files, base_path)
        chunks = await self.processor.abatch_process(summary.files)

        documents = [
            StorageDocument(
                id=chunk.id,
                content=chunk.content,
                vector=self.embedding_model.encode(chunk.content),
                metadata=chunk.metadata,
            )
            for chunk in chunks
        ]

        self.storage.upsert(collection=collection_name, documents=documents)


if __name__ == "__main__":
    import asyncio

    from dotenv import load_dotenv
    from qdrant_client.models import Distance, VectorParams

    from wiki_agent.config import (
        DashScopeEmbeddingConfig,
        QdrantStorageConfig,
        TextChunkerConfig,
    )
    from wiki_agent.ingestion.chunker import TextChunker
    from wiki_agent.ingestion.converter import Converter
    from wiki_agent.storage import create_storage

    env_path = Path(__file__).parent.parent / "env" / ".env"
    load_dotenv(env_path)

    from wiki_agent.ingestion.chunker import TextChunker, Chunker
    converter = Converter()
    processor = Processor(converter=converter, chunker=Chunker([TextChunker()]))

    embedding_model = create_embedding_model(DashScopeEmbeddingConfig.from_env())
    storage = create_storage(QdrantStorageConfig.from_env())

    pipeline = TextPipeline(
        name="test pipeline",
        data_loader=DataLoader(),
        embedding_model=embedding_model,
        processor=processor,
        storage=storage,
    )
    if not storage.client.collection_exists("new"):
        storage.client.create_collection(
            "new",
            vectors_config=VectorParams(
                size=embedding_model.embedding_dim,
                distance=storage.distance_method,
            ),
        )

    asyncio.run(
        pipeline.run(
            collection_name="new",
            files=["test.txt", "test2.txt"],
            base_path="/media/zilan/B44018EE4018B8D6/notebook/Agent开发/Agent实战/LearnRag/docs",
        )
    )
