"""LLM-driven consolidation of conversation history into user memory."""

import json
from pathlib import Path

from wiki_agent.conversation import Message
from wiki_agent.llm import LLMClient
from wiki_agent.log import get_logger
from wiki_agent.memory.store import MemoryStore

logger = get_logger("MEMORY")


class Dreamer:
    _DREAM_PROMPT = """
        你是一名语言大师，擅长根据记录更新用户的画像，分析用户的爱好，价值观等能描述用户的信息，
        并根据已有的记录信息，对记录进行完整的重写，新的重写涵盖更丰富完整的用户描述。

        # 注意
        1. 不能简单增加文字，而是进行汇总，重新整理
        2. 只更新与用户相关内容，不关心助手
        3. 如果记录中记录了无关信息，则在更新中移除

        需要处理的历史:
        {history}

        已有的描述：
        {memory}
    """

    def __init__(self, workspace: Path, memory_store: MemoryStore):
        self.workspace = workspace
        self.memory_store = memory_store

    def build_dream_prompt(self, history: str, memory: str) -> str:
        return self._DREAM_PROMPT.format(history=history, memory=memory)

    async def dream(self, llm: LLMClient):
        grouped_history = self.memory_store.get_unprocessed_history()
        memory = self.memory_store.get_memory_text()
        new_cursor = self.memory_store.get_cursor()

        failed = False
        for session_key, history in grouped_history.items():
            text_history = "\n".join(json.dumps(record) for record in history)
            update_messages = [
                Message(
                    role="system",
                    content=self.build_dream_prompt(history=text_history, memory=memory),
                )
            ]
            try:
                response = await llm.async_invoke(messages=update_messages)
            except Exception as exc:
                failed = True
                logger.warning(
                    "session %s Dream失败: %s: %s",
                    session_key,
                    type(exc).__name__,
                    str(exc)[:160],
                )
                continue

            if response.content:
                self.update_memory(update_content=response.content)
            else:
                failed = True
                logger.warning("session %s Dream返回结果为空，跳过更新", session_key)

        if not failed:
            self.memory_store.update_dream_cursor(new_cursor=new_cursor)

    def update_memory(self, update_content: str, fsync: bool = False):
        self.memory_store._atomic_write(
            self.memory_store.memory_file,
            update_content,
            fsync=fsync,
        )
