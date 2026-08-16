from wiki_agent.tools.base import BaseTool
from wiki_agent.log import get_logger

logger=get_logger("ToolRegistry")

class ToolRegistry:
    def __init__(self):
        self._tools={}

    def register(self, tool: BaseTool) -> None:
        """注册工具（同名覆盖）。

        Args:
            tool: 工具实例（name 作 key）。
        """
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销工具（不存在时静默）。

        Args:
            name: 工具名。
        """
        self._tools.pop(name, None)

    def get(self, name: str):
        """按名取工具实例。

        Args:
            name: 工具名。

        Returns:
            工具实例；未注册返回 None。
        """
        return self._tools.get(name, None)

    async def execute(self, name: str, params: dict) -> str:
        """执行工具调用。

        Args:
            name: 工具名。
            params: 工具参数字典。

        Returns:
            工具结果文本；未注册工具返回错误文本。
        """
        if name not in self._tools:
            return f"Error: 使用了未被注册的工具 {name} "
        result = await self._tools[name].execute(**params)
        return result

    def get_all_description(self) -> str:
        """拼接全部工具描述（供 prompt 使用）。

        Returns:
            "函数名: X, description: Y" 换行拼接文本。
        """
        return "\n".join(\
            [
                f"函数名:{name},description: {tool.description}"
                for name, tool in self._tools.items()
            ]
        )

    def get_all_schema_openai(self) -> list[dict]:
        """返回全部工具的 OpenAI schema 列表。

        Returns:
            OpenAI 工具 schema 列表（注册表内全部工具）。
        """
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