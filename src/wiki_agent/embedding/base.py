from abc import ABC,abstractmethod
from wiki_agent.log import get_logger

logger=get_logger("EMBEDDING")

class BaseEmbeddingModel(ABC):
    def __init__(self):
        self.embedding_dim:int=0
        self._initialized:bool=False
    
    def initialize(self,config):
        logger.info("开始初始化Embedding模块...")
        try:
            self._initialize(config)
        except Exception as e:
            raise ValueError(f"Embedding Model 初始化失败 - {e}")
        logger.info("Embedding模块初始化完成...")

    @abstractmethod
    def _initialize(self,config):
        """
        子类初始化实现
        """
        pass

    def encode(self,query:str|list[str]):
        try:
            result=self._encode(query)
        except Exception as e:
            raise RuntimeError(f"编码失败 - {e}")
        return result
    
    @abstractmethod
    def _encode(self,query):
        """
        子类编码方式实现
        """
        pass

    @abstractmethod
    def health_check()->bool:
        """
        判断服务是否正常运行
        """
        pass
