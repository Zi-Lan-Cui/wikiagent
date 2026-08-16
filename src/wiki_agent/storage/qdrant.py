from wiki_agent.storage.base import BaseStorage,StorageDocument,SearchQuery,BatchSearchQuery,SearchResult,ScoredStorgeDocument
from wiki_agent.log import get_logger
from wiki_agent.config import QdrantStorageConfig,BaseEmbeddingConfig,DashScopeEmbeddingConfig

from qdrant_client import QdrantClient,models
from qdrant_client.models import Document,PointStruct,Distance,VectorParams
from datetime import datetime

logger = get_logger("STORAGE")

distance_map={
    "cosine":Distance.COSINE
}

class QdrantStorage(BaseStorage):
    def __init__(self):
        super().__init__()
        self.name=""
        self._collections:list[str]=[]
        self.base_url=""
        self.api_key=""
        self.distance_method:str=""
        self.embedding_dim:int=None
    
    def _initialize(self, config:QdrantStorageConfig):
        logger.info(f"存储库{self.name}正在初始化>>>>")
        self.name = config.backend_name
        self.base_url=config.base_url
        self.api_key=config.api_key
        self.embedding_dim=config.embedding_dim
        self.distance_method=distance_map[config.vector_distance_method.lower()]
        self._collections=config.collections

        self.client=QdrantClient(url=config.base_url,api_key=config.api_key)
        self.health_check()
        vector_config=models.VectorParams(size=self.embedding_dim,distance=self.distance_method)
        self.vector_config=vector_config

        # 先获取已有collection
        original_collections_info=self.client.get_collections()
        original_collections=[collection.name for collection in original_collections_info.collections]
        
        for collection in self._collections: 
            if not self.client.collection_exists(collection):
                self.client.create_collection(collection,vectors_config=vector_config)
        
        logger.info(f"存储库{self.name}初始化完成, 新加载collections: {self._collections}，已有collections: {original_collections}")
        self._collections.extend(original_collections)
    
    def _search(self, search_query:SearchQuery|BatchSearchQuery)->SearchResult|list[SearchResult]:
        
        def process_with_payload(with_payload):
            """
            处理with_payload多类型，为True时返回所有payload,为list时返回选择的部分，如果为None，则至少返回content用于
            接口兼容
            """
            if isinstance(with_payload,bool):
                return True
            if isinstance(with_payload,list):
                return with_payload+["content"]
            elif with_payload is None:
                return ["content"]
            else:
                raise TypeError(f"with_payload 类型错误，期望[bool,list,None]，实际是{type(with_payload)}")
            
        if isinstance(search_query,BatchSearchQuery):
            collection_name=search_query.collection_name
            search_queries=[
                models.QueryRequest(
                    query=query.vector,
                    with_payload=process_with_payload(query.with_payload),
                    limit=query.limit,
                    filter=query.query_filter,
                    
                    with_vector=query.with_vector,
                ) 
                for query in search_query.queries
            ]

            results=self.client.query_batch_points(
                collection_name=collection_name,
                requests=search_queries,  
                timeout=20    
            )

            all_documents=[]
            for result in results:
                currrent_result_points=[]
                for point in result.points:
                    currrent_result_points.append(
                        ScoredStorgeDocument(
                            id=point.id,
                            content=point.payload.get("content",""),
                            metadata={k:v for k,v in point.payload.items() if k!="content"},
                            vector=point.vector if point.vector else [],
                            score=point.score 
                        )
                    )
                all_documents.append(currrent_result_points)

            return [SearchResult(documents=documents) for documents in all_documents] 
        else:
            result=self.client.query_points(
                collection_name=search_query.collection_name,
                query_filter=search_query.query_filter,
                query=search_query.vector,
                with_payload=process_with_payload(search_query.with_payload),
                limit=search_query.limit,
                with_vectors=search_query.with_vector,
                timeout=20
            )
            
            documents=[]
            for point in result.points:
                documents.append(
                    ScoredStorgeDocument(
                        id=point.id,
                        metadata={k:v for k,v in point.payload.items() if k!="content"},
                        score=point.score,
                        vector=point.vector if point.vector else [],
                        content=point.payload.get("content","")
                    )
                )

            return SearchResult(documents=documents) 
    
    def _upsert(self,collection:str,documents:StorageDocument|list[StorageDocument]):
        if not isinstance(documents,list):
            documents=[documents]

        points=[
            PointStruct(
                id=document.id,
                vector=document.vector,
                payload={
                    "content":document.content,
                    **document.metadata
                }
            )

            for document in documents
        ]

        self.client.upsert(collection_name=collection,points=points,timeout=20)
  
    def health_check(self)->bool:
        try:
            self.client.get_collections()
            return True
        except Exception as e:
            # 只捕异常不捕 KeyboardInterrupt/SystemExit
            logger.warning(f"数据库{self.name}连接失败 - {type(e).__name__}: {e}")
            return False
        
    def exists(self,collection_name,id:str)->bool:
        """
        查找内容是不是已经被存储
        """
        result=self.client.retrieve(collection_name,id)
        return len(result>0)
    
    def statstic(self):
        """
        返回数据库本身的统计数据
        """
        pass

    def create_collection(self, collection:str):
        """
        创建新collection,向量配置为数据库初始化配置
        """
        if self.client.create_collection(collection_name=collection,vectors_config=self.vector_config):
            self._collections.append(collection)
        else:
            logger.error(f"创建新collection:{collection}失败")

if __name__=="__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    from wiki_agent.embedding import create_embedding_model
    env_path=Path(__file__).parent.parent/"env"/".env"
    load_dotenv(env_path)

    menu_items = [
        ("Pad Thai with Tofu", "Stir-fried rice noodles with tofu bean sprouts scallions and crushed peanuts in traditional tamarind sauce", "$13.95", "Noodles"),
        ("Grilled Salmon Fillet", "Wild-caught Atlantic salmon grilled with lemon butter and fresh herbs served with seasonal vegetables", "$24.50", "Seafood Entrees"),
        ("Mushroom Risotto", "Creamy arborio rice with mixed mushrooms parmesan truffle oil and fresh thyme", "$16.75", "Vegetarian"),
        ("Bibimbap Bowl", "Korean rice bowl with seasoned vegetables fried egg gochujang sauce and choice of protein", "$14.50", "Korean Bowls"),
        ("Falafel Wrap", "Crispy chickpea fritters with hummus tahini cucumber tomato and pickled vegetables in warm pita", "$11.25", "Mediterranean"),
        ("Shrimp Tacos", "Three soft tacos with grilled shrimp cabbage slaw chipotle aioli and fresh lime", "$13.00", "Tacos"),
        ("Vegetable Curry", "Mixed vegetables in aromatic coconut curry sauce with jasmine rice and naan bread", "$12.95", "Indian Curries"),
        ("Tuna Poke Bowl", "Fresh ahi tuna with avocado edamame cucumber seaweed salad over sushi rice with spicy mayo", "$16.50", "Poke Bowls"),
        ("Margherita Pizza", "Fresh mozzarella san marzano tomatoes basil and extra virgin olive oil on wood-fired crust", "$14.00", "Pizza"),
        ("Chicken Tikka Masala", "Tandoori chicken in creamy tomato sauce with aromatic spices served with basmati rice", "$15.95", "Indian Entrees"),
        ("Greek Salad", "Romaine lettuce tomatoes cucumbers kalamata olives feta cheese red onion with lemon oregano dressing", "$10.50", "Salads"),
        ("Lobster Roll", "Fresh Maine lobster meat with light mayo on toasted buttery roll served with chips", "$22.00", "Seafood Sandwiches"),
        ("Quinoa Buddha Bowl", "Organic quinoa with roasted chickpeas kale sweet potato tahini dressing and hemp seeds", "$13.50", "Healthy Bowls"),
        ("Beef Pho", "Traditional Vietnamese beef noodle soup with rice noodles fresh herbs bean sprouts and lime", "$12.75", "Noodle Soups"),
        ("Eggplant Parmesan", "Breaded eggplant layered with marinara mozzarella and parmesan served with pasta", "$15.25", "Italian Entrees"),
        ("Crab Cakes", "Maryland-style lump crab cakes with remoulade sauce and mixed greens", "$18.50", "Seafood Appetizers"),
        ("Tofu Stir Fry", "Crispy tofu with broccoli bell peppers snap peas in garlic ginger sauce over steamed rice", "$12.50", "Vegetarian Entrees"),
        ("Salmon Sushi Platter", "12 pieces of fresh salmon nigiri and sashimi with wasabi pickled ginger and soy sauce", "$19.95", "Sushi"),
        ("Caprese Sandwich", "Fresh mozzarella tomatoes basil pesto balsamic glaze on ciabatta bread", "$11.75", "Sandwiches"),
        ("Tom Yum Soup", "Spicy and sour Thai soup with shrimp lemongrass galangal mushrooms and kaffir lime leaves", "$11.50", "Soups"),
        ("Lentil Dal", "Red lentils simmered with turmeric cumin coriander served with rice and naan", "$11.95", "Vegan Entrees"),
        ("Fish and Chips", "Beer-battered cod with crispy fries malt vinegar and tartar sauce", "$16.00", "British Classics"),
        ("Veggie Burger", "House-made black bean and quinoa patty with avocado sprouts tomato on brioche bun", "$13.25", "Burgers"),
        ("Miso Ramen", "Rich miso broth with ramen noodles soft-boiled egg bamboo shoots nori and scallions", "$14.50", "Ramen"),
        ("Stuffed Bell Peppers", "Roasted bell peppers filled with rice vegetables herbs and melted cheese", "$13.75", "Vegetarian Entrees"),
        ("Scallop Risotto", "Pan-seared sea scallops over creamy parmesan risotto with white wine and lemon", "$26.50", "Seafood Specials"),
        ("Spring Rolls", "Fresh rice paper rolls with vegetables tofu rice noodles herbs and peanut dipping sauce", "$8.95", "Appetizers"),
        ("Oyster Po Boy", "Fried oysters with lettuce tomato pickles and remoulade on french bread", "$15.50", "Sandwiches"),
        ("Portobello Mushroom Steak", "Grilled portobello cap marinated in balsamic with roasted vegetables and quinoa", "$14.95", "Vegan Entrees"),
        ("Coconut Shrimp", "Jumbo shrimp breaded in shredded coconut served with sweet chili sauce", "$14.25", "Seafood Appetizers")
    ]

    
    embedding_model=create_embedding_model(DashScopeEmbeddingConfig.from_env())
    storage=QdrantStorage()
    storage.initialize(QdrantStorageConfig.from_env())

    need_add=[]
    for item in menu_items:
        document=StorageDocument(
            vector=embedding_model.encode(item[1]),
            content=item[1],
            metadata={
                "topic":"test",
                "time_stamp":datetime.now().isoformat(),
                "item_name":item[0],
                "description":item[1],
                "price":item[2],
                "category":item[3],
            },
        )

        need_add.append(document)
    
    storage.upsert(collection="new",documents=need_add)
    response=storage.search(
        BatchSearchQuery(
            collection_name="new",
            queries=[
                SearchQuery(
                    vector=embedding_model.encode("vegetarain dishes"),
                    limit=3,
                    with_payload=["item_name","price","description"]
                ),
                SearchQuery(
                    vector=embedding_model.encode("a big fish"),
                    limit=3,
                    with_payload=["item_name","price","description"]
                )
            ]
        )
    )

    if not isinstance(response,list):
        for document in response.documents:
            print(f"分数：{document.score} id:{document.id} 内容:{document.content}")
    else:
        for i,res in enumerate(response):
            print(f"\n问题{i+1}:")
            for document in res.documents:
                print(f"分数：{document.score} id:{document.id} 内容:{document.content}")
    
    # #! 清空所有数据
    # info=storage.client.get_collections()
    # for collection in info.collections:
    #     storage.client.delete_collection(collection.name)
