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
        """
        执行Agent——输出经 hook 事件（on_stream_delta 等）订阅，
        不传渲染回调。
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

