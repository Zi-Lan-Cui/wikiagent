from wiki_agent.embedding.base import BaseEmbeddingModel
from wiki_agent.config import DashScopeEmbeddingConfig
from wiki_agent.log import get_logger
import os

logger=get_logger("EMBEDDING")

class DashScopeEmbedding(BaseEmbeddingModel):
    def __init__(self):
        super().__init__()
        self.model_name:str=""
        self.api_key:str=""
        self.base_url:str=""

    def _initialize(self, config:DashScopeEmbeddingConfig):
        self.embedding_dim=config.embedding_dim
        self.model_name=config.model_name
        self.api_key=config.api_key
        self.base_url=config.base_url

        if not self.base_url:
            try:
                os.environ["DASHSCOPE_API_KEY"] = config.api_key
                import dashscope
            except ImportError as e:
                raise ImportError("无base_url方法需要安装 dashscope ，请安装 dashscope 包！或者设置EMBEDDING_BASE_URL环境变量")
        
        if not self.health_check():
            raise RuntimeError("模型配置成功导入，但是测试失败,请检查配置")
        
        self._initialized=True
        logger.info(f"DashScopeEmbedding模型初始化成功，当前嵌入维度: {self.embedding_dim}")
    
    def _encode(self,query:str|list[str]):
        single=True if isinstance(query,str) else False

        if self.base_url:
            import requests
            request_url=self.base_url.rstrip('/')+"/embeddings"
            header={
                "Authorization":f"Bearer {self.api_key}",
                "Content-Type":"application/json"
            }

            payload={
                "model":self.model_name,
                "input":query,
                "dimensions":self.embedding_dim,
                "encoding_format":"float"
            }

            response=requests.post(url=request_url,headers=header,json=payload,timeout=20)

            if response.status_code>=400:
                raise RuntimeError(f"嵌入模型调用失败，错误码: {response.status_code}")
            
            data=response.json().get("data") or []
            vecs=[x.get("embedding") for x in data]

            if single:
                return vecs[0]
            return vecs
        
        from dashscope import TextEmbedding
        
        response=TextEmbedding.call(
            model=self.model_name,
            input=query,
            dimension=self.embedding_dim
        )

        if response.status_code>=400:
            raise RuntimeError(f"嵌入模型调用失败。错误信息: {response.message}")
        
        if single:
            return response.output["embeddings"][0]["embedding"]
        return [dic["embedding"] for dic in response.output["embeddings"]]

    def health_check(self)->bool:
        try:
            self.encode("test")
            return True
        except Exception as e:
            logger.error(f"{self.model_name}健康检查失败! -{e}")
            return False
        
if __name__=="__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    env_path=Path(__file__).parent.parent/"env"/".env"
    load_dotenv(env_path)
    embedding_model=DashScopeEmbedding()
    embedding_model.initialize(DashScopeEmbeddingConfig.from_env())
    print(embedding_model.encode(query="test"))




