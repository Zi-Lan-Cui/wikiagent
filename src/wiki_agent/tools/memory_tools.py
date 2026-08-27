"""记忆工具——agent 主动调用的记忆/纠错记录工具。

与记忆管道的分工:
    RecordCorrection 是纠错管道的入口——agent 在对话中识别到
    用户指出 wiki 错误/缺口时主动调用，自然语言原样落账进
    corrections.md（待修清单），不走 Dreamer 画像加工。
"""

from __future__ import annotations

from typing import ClassVar

from wiki_agent.memory import MemoryStore
from wiki_agent.tools.base import BaseTool


class RecordCorrection(BaseTool):
    """把用户指出的 wiki 错误记进待修清单。

    LLM 在对话中发现用户指出 wiki 页面内容错误、过时或缺失时
    主动调用——纠错是知识库演化的信号，不能丢在对话里。
    """

    name: str = "RecordCorrection"
    # append_correction 每次生成新的随机 corr_id；在 operation key 尚未
    # 贯穿存储层前，不能把它当作幂等写入自动重试。
    side_effect: ClassVar[str] = "irreversible"
    description: str = (
        "当用户指出 wiki 知识库中某页面的内容错误、过时或缺失时调用——"
        "把纠错记进待修清单（corrections.md），供后续 refine/手术使用。"
        "注意: 只在用户明确表达 wiki 内容有问题时调用；"
        "普通问答、用户提问不算纠错。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "page": {
                "type": "string",
                "description": "出错的 wiki 页面相对路径（如 concepts/lambda.md）；不确定可留空",
            },
            "issue": {
                "type": "string",
                "description": "自然语言描述的问题: 哪里错了/缺了什么/为什么错",
            },
        },
        "required": ["issue"],
    }

    def __init__(self, memory_store: MemoryStore):
        self._memory_store = memory_store

    async def execute_once(self, page: str = "", issue: str = "") -> str:
        """记录纠错进待修清单。

        Args:
            page: 出错页面的相对路径（可为空）。
            issue: 自然语言问题描述。

        Returns:
            记录结果文本；issue 为空时返回"未记录"。
        """
        text = issue.strip()
        if not text:
            return "未记录——issue 为空"
        self._memory_store.append_correction(text=text, page=page)
        shown = f"[{page.strip()}] {text}" if page.strip() else text
        return f"已记录纠错: {shown}"
