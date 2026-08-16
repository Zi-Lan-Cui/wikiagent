from abc import ABC,abstractmethod
from wiki_agent.log import get_logger

from pathlib import Path

logger=get_logger("AGENT")

class BaseAgent(ABC):
    def __init__(self,name,workspace:Path):
        self.name:str=name
        self.workspace=workspace

    async def run(
            self,
            session_key:str,
            user_input:str,
            stream=False,
        ):
        """执行 Agent 一轮对话。

        输出经 hook 事件（on_stream_delta 等）订阅，不传渲染回调。

        Args:
            session_key: 会话标识（持久化文件名）。
            user_input: 用户输入文本。
            stream: 为 True 时以流式模式调用 LLM。

        Returns:
            None。运行结果经 hook 事件对外发布。
        """
        await self._run(
            session_key=session_key,
            user_input=user_input,
            stream=stream,
        )

    @abstractmethod
    async def _run(
            self,
            session_key:str,
            user_input:str,
            stream=False,
    ):
        pass

