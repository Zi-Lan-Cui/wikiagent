from typing import Any, List, Literal

from pydantic import BaseModel, Field
from datetime import datetime
import json


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class MessageMeta(BaseModel):
    """消息元数据——系统侧信息，不进 LLM 内容。

    time_stamp 是创建时刻——TTL 驱逐按它算年龄；checkpoint 持久化
    保留原始值（恢复会话不重算，旧工具结果不会被误判为新鲜）。

    LLM 不需要消息时间: 绝对时间戳对语义无价值（相对顺序靠上下文），
    估计文本（text_schema）与真实发送内容（openai_schema）同形
    才有可靠的 token 估计。时间是系统元数据，消费者经 created_at
    显式取用——LLM 需要"现在几点"时未来给 GetTime 工具（按需查），
    而不是每条消息塞时间戳。
    """

    time_stamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class Message(BaseModel):
    role: Literal["assistant", "tool", "user", "system"]
    content: str = ""
    images: list[str] = Field(default_factory=list)  # base64 图片（不带 data: 前缀）
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str = ""
    tool_name: str = ""  # tool 消息携带工具名，供 governor 按工具豁免截断
    metadata: MessageMeta = Field(default_factory=MessageMeta)

    @property
    def created_at(self) -> datetime | None:
        """创建时刻——时间戳的查询接口（TTL 驱逐等系统消费者用）。

        解析失败返回 None（消费者自行降级——时间是元数据，
        不应因它崩溃）。
        """
        try:
            return datetime.fromisoformat(self.metadata.time_stamp)
        except (ValueError, TypeError):
            return None

    @property
    def text_schema(self):
        """token 估计/压缩用的文本形态——与 openai_schema 内容对齐。

        不含时间戳/图片等元数据——估计文本与真实发送内容同形，
        否则 token 估计系统性偏差（时间戳每条消息都变，还会让
        内容相同时间不同的消息产生不同估计）。
        """
        text = ""
        if self.role == "user":
            text += f"{self.role}:{self.content}\n"
        elif self.role == "assistant":
            text += f"{self.role}:{self.content}\n"
            if self.tool_calls:
                for tc in self.tool_calls:
                    text += json.dumps(tc.model_dump()) + "\n"
        elif self.role == "tool":
            text += f"{self.role} tool_id:{self.tool_call_id}:{self.content}\n"
        elif self.role == "system":
            text += f"{self.role}:{self.content}\n"

        return text

    # ── openai_schema — 统一的消息序列化点 ────────────────

    def _build_content(self):
        """根据 content + images 构造 OpenAI 格式的 content 字段。"""
        if not self.images:
            return self.content
        parts: list[dict[str, Any]] = [
            {"type": "text", "text": self.content},
        ]
        for img in self.images:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img}"},
            })
        return parts

    @property
    def openai_schema(self):
        if self.role == "tool":
            return {
                "role": self.role,
                "tool_call_id": self.tool_call_id,
                "content": self.content,
            }
        result: dict[str, Any] = {
            "role": self.role,
            "content": self._build_content(),
        }
        if self.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        return result

    @classmethod
    def create_from_openai(cls, data: dict) -> "Message":
        tool_calls = []
        for tool_call in data.get("tool_calls", []):
            args = tool_call["function"]["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            tool_calls.append(
                ToolCall(
                    id=tool_call["id"],
                    name=tool_call["function"]["name"],
                    arguments=args,
                )
            )

        content = data.get("content", "")
        # 还原纯文本（如果 LLM 返回了多模态 content 数组）
        if isinstance(content, list):
            parts = [p["text"] for p in content if p.get("type") == "text"]
            content = "\n".join(parts) if parts else ""

        return Message(
            role=data["role"],
            content=content,
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id", ""),
        )
            
class LLMResponse(BaseModel):
    role:str="assistant"
    content:str=""
    tool_calls:List[ToolCall]=Field(default_factory=list)
    finish_reason:str=""
    usage:dict=Field(default_factory=dict)
    reasoning_content:str=""  # reasoning 模型思考段（编译流水线应禁用 thinking）
    check_ok:bool=True        # 输出校验结果——retry 层填充（无 check 时恒 True）
    check_reason:str=""       # 校验失败原因（check_ok=False 时非空）


def find_first_legal_idx(messages: list[Message], extend_to_user: bool = True):
    """找到消息列表的合法起始下标——保证历史截断后 API 请求合法。

    规则（消息领域逻辑——从 utils 移入本模块）:
    - 孤儿 tool 结果（无对应调用）之后的位置是合法起点
      （截断不能从孤儿结果开始——API 会拒绝）
    - extend_to_user=True 时首个 user 消息即返回
      （对话窗口应以 user 提问开头）
    """
    start = 0
    called = set()
    for idx, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            for tool in message.tool_calls:
                called.add(tool.id)
        if message.role == "tool" and message.tool_call_id not in called:
            start = idx + 1
            # 清除所有标记（把这里当做新的开始）
            called.clear()
        if extend_to_user and message.role == "user":
            return idx
    return start





    
