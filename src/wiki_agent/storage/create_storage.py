from wiki_agent.storage.qdrant import QdrantStorage
from wiki_agent.config import BaseStorageConfig

def create_storage(config:BaseStorageConfig):
    if config.backend_name=="qdrant":
        storage=QdrantStorage()
        storage.initialize(config)
        return storage
    else:
        raise ValueError(f"不支持的后端名称{config.backend_name}")
        