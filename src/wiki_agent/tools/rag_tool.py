from wiki_agent.tools.base import BaseTool
from wiki_agent.storage import BaseStorage
from wiki_agent.storage import SearchQuery,BatchSearchQuery
from wiki_agent.embedding import BaseEmbeddingModel,create_embedding_model

from typing import ClassVar

class RAGTool(BaseTool):
    name:ClassVar[str]="RAGTool"
    description:ClassVar[str]="检索向量库中相关数据"
    parameters:ClassVar[dict]={
        "type":"object",
        "properties":{
            "query":{
                "oneOf":[
                    {"type":"string","description":"查询单个内容"},
                    {"type":"array","items":{"type":"string"},"description":"批量查询多个内容"}
                ],
                "description":"你要查询的内容"
            },
            "collection_name":{
                "type":"string",
                "description":"要搜索的集合名"
            },
            "limit":{
                "type":"integer",
                "description":"返回的数目"
            }
        },
        "required":["query","collection_name"]
    }

    def __init__(self,storage:BaseStorage,embedding_model:BaseEmbeddingModel):
        self.storage=storage
        self.embedding_model=embedding_model

    async def _execute(self,query:str|list[str],collection_name:str,limit:int=3)->str:
        if not isinstance(query,list):
            vec=self.embedding_model.encode(query)
            search_query=SearchQuery(
                collection_name=collection_name,
                vector=vec,
                content=query,
                with_payload=True,
                with_vector=False,
                limit=limit
            )
            search_result= self.storage.search(search_query)
            return "\n".join(f"{i+1}. {doc.content} " for i,doc in enumerate(search_result.documents))
        else:
            vecs=self.embedding_model.encode(query)
            search_query=BatchSearchQuery(
                collection_name=collection_name,
                queries=[
                    SearchQuery(
                        vector=vecs[i],
                        content=query[i],
                        with_payload=True,
                        with_vector=False,
                        limit=limit
                    )
                    for i in range(len(query)) 
                ]
            )
            search_result=self.storage.search(search_query)
            ans=""
            for idx,res in enumerate(search_result):
                ans+=f"问题{idx}:{query[idx]}\n"
                for i,doc in enumerate(res.documents):
                    ans+=f"{i+1}. 分值:{doc.score}\n{doc.content}\n\n"
                ans+="\n"
            return ans

if __name__=="__main__":
    from wiki_agent.storage import create_storage
    from wiki_agent.config import QdrantStorageConfig,DashScopeEmbeddingConfig
    from dotenv import load_dotenv
    from pathlib import Path
    env_path=Path(__file__).parent.parent/"env"/".env"
    load_dotenv(env_path)

    tool=RAGTool(
        storage=create_storage(QdrantStorageConfig.from_env()),
        embedding_model=create_embedding_model(DashScopeEmbeddingConfig.from_env())
    )
    response=tool._execute(["big fish","salad"],"new")

    print(response)
    