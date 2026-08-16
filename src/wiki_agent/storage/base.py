from wiki_agent.log import get_logger
from wiki_agent.config import BaseStorageConfig
from wiki_agent.embedding import BaseEmbeddingModel

from abc import ABC,abstractmethod
from pydantic import BaseModel,Field,model_validator
from datetime import datetime,timezone
from typing import Any,Dict,List
import uuid

logger=get_logger("STORAGE")

class SearchQuery(BaseModel):
    collection_name:str=""
    vector:list[float]=Field(default_factory=list)
    content:str=""
    limit:int=5
    query_filter:dict|None=None
    with_payload:bool|list[str]|None=None
    with_vector:bool|None=None

class BatchSearchQuery(BaseModel):
    collection_name:str=""
    queries:list[SearchQuery]=Field(default_factory=list)

    # 同步batch块collection_name和子块collection_name
    @model_validator(mode="after")
    def sync_collection_name(self):
        for q in self.queries:
            q.collection_name=self.collection_name
        return self

class StorageDocument(BaseModel):
    """
    content的内容是encode编码的内容
    """
    id:str=Field(default_factory=lambda: uuid.uuid4().hex)
    content:str|None=""
    vector:list[float]=Field(default_factory=list)
    metadata:Dict[str,Any]=Field(default_factory=list)

class ScoredStorgeDocument(StorageDocument):
    score:float|None

class SearchResult(BaseModel):
    documents:list[ScoredStorgeDocument]=Field(default_factory=list)

class BaseStorage(ABC):
    
    def __init__(self):
        self.name:str=""
        self.embedding_dim:int=None
        self._initialized:bool=False

    def initialize(self,config:BaseStorageConfig):
        logger.info("初始化数据库中>>>>")
        try:
            self._initialize(config)
        except Exception as e:
            raise RuntimeError(f"数据库初始化失败，请修改后重新尝试 -{e}")
        logger.info("数据库初始化完成<<<<")
    
    @abstractmethod
    def _initialize(self,config:BaseStorageConfig):
        """
        子类初始化具体实现
        """
        pass

    def search(self, search_query:SearchQuery|BatchSearchQuery)->SearchResult|list[SearchResult]:
        """
        从数据库中搜索query
        """
        logger.info(f"正在搜索数据...")
        if self.health_check():
            try:
                result=self._search(search_query)
                logger.info(f"文件搜索完成")
            except Exception as e:
                raise RuntimeError(f"搜索数据库时遇到错误 - {e}")
        else:
            raise RuntimeError("数据库健康检查失败")
        return result

    @abstractmethod
    def _search(self, search_query:SearchQuery|BatchSearchQuery)->SearchResult|list[SearchResult]:
        """
        搜索的具体实现
        """
        pass

    def upsert(self,collection:str,documents:StorageDocument|list[StorageDocument]):
        """
        对数据库更新/插入content
        """
        logger.info(f"准备插入/更新文件...")
        if self.health_check():
            try:
                self._upsert(collection,documents)
                logger.info(f"文件插入/更新完成")
            except Exception as e:
                raise RuntimeError(f"插入数据库时遇到错误 - {e}")
        else:
            raise RuntimeError("数据库健康检查失败")

    @abstractmethod
    def _upsert(self,collection:str,documents:StorageDocument|list[StorageDocument]):
        """
        子类实现
        """
        pass            

    @abstractmethod
    def health_check(self)->bool:
        """
        检查是否链接正常
        子类具体实现
        """
        pass

    @abstractmethod
    def exists(self,id:str)->bool:
        """
        查找对应id是不是已经被存储
        """
        pass
    
    @abstractmethod
    def statstic(self):
        """
        返回数据库本身的统计数据
        """
        pass

    @abstractmethod
    def create_collection(self,collection:str):
        """
        pass
        """