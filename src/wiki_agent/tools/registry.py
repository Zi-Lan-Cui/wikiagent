from wiki_agent.tools.base import BaseTool
from wiki_agent.log import get_logger

logger=get_logger("ToolRegistry")

class ToolRegistry:
    def __init__(self):
        self._tools={}

    def register(self,tool:BaseTool):
        self._tools[tool.name]=tool

    def unregister(self,name:str):
        self._tools.pop(name,None)

    def get(self,name):
        return self._tools.get(name,None)

    async def execute(self,name,params):
        if name not in self._tools:
            return f"Error: 使用了未被注册的工具 {name} "
        result= await self._tools[name].execute(**params)
        return result

    def get_all_description(self):
        return "\n".join(\
            [
                f"函数名:{name},description: {tool.description}"
                for name,tool in self._tools.items()
            ]
        )

    def get_all_schema_openai(self):
        all_schema=[]
        for tool in self._tools.values():
            all_schema.append(
                {
                    "type":"function",
                    "function":{
                        "name":tool.name,
                        "description":tool.description,
                        "parameters":tool.parameters
                    }
                }
            )
        return all_schema