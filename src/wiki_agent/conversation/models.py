import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class ThinkingSegment(BaseModel):
    """UI 折叠块的有序分段，随消息持久化，不回发给 LLM。

    kind=think 时使用 text；kind=tool 时使用 name/arguments/ms/error。
    """

    kind: Literal["think", "tool"]
    text: str = ""
    name: str = ""
    arguments: dict = Field(default_factory=dict)
    ms: int = 0
    error: bool = False


class MessageMeta(BaseModel):
    """消息元数据，不进 LLM 内容。

    time_stamp 是创建时刻，TTL 驱逐按它算年龄；checkpoint 保留原始值，
    恢复会话不重算，旧工具结果不会被当作新鲜。

    绝对时间对 LLM 无语义价值，估计文本与发送内容都不含时间；
    系统消费者经 created_at 显式取用。
    """

    time_stamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class Message(BaseModel):
    role: Literal["assistant", "tool", "user", "system"]
    content: str = ""
    images: list[str] = Field(default_factory=list)  # base64 图片（不带 data: 前缀）
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str = ""
    tool_name: str = ""  # tool 消息携带工具名，供 governor 按工具处理
    thinking: list[ThinkingSegment] = Field(default_factory=list)
    # 仅持久化给 UI；openai_schema/text_schema 均不含，不回发、不计 token
    metadata: MessageMeta = Field(default_factory=MessageMeta)

    @property
    def created_at(self) -> datetime | None:
        """读取元数据时间戳，供 TTL 驱逐等系统消费者使用。

        Returns:
            消息创建时刻；时间戳缺失或非法时返回 None，由调用方处理。
        """
        try:
            return datetime.fromisoformat(self.metadata.time_stamp)
        except (ValueError, TypeError):
            return None

    @property
    def text_schema(self):
        """token 估计与压缩用的文本形态。

        与 openai_schema 内容对齐，不含时间戳和图片，
        保证估计文本与发送内容一致，token 估计才准确。

        Returns:
            该消息的纯文本表示（按 role 拼接）。
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

    def _build_content(self):
        """构造 OpenAI 格式的 content 字段。

        Returns:
            无图片时返回纯文本字符串；有图片时返回
            [{"type": "text"...}, {"type": "image_url"...}] 数组。
        """
        if not self.images:
            return self.content
        parts: list[dict[str, Any]] = [
            {"type": "text", "text": self.content},
        ]
        for img in self.images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img}"},
                }
            )
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


class LLMResponse(BaseModel):
    role: str = "assistant"
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = ""
    usage: dict = Field(default_factory=dict)
    reasoning_content: str = ""  # reasoning 模型的思考内容
    check_ok: bool = True  # 输出校验结果，由 retry 层填充
    check_reason: str = ""  # 校验失败原因（check_ok=False 时非空）


def find_first_legal_idx(messages: list[Message], extend_to_user: bool = True):
    """找到截断后仍能保证 API 请求合法的起始下标。

    规则:
    - 孤儿 tool 结果（无对应调用）不能作为起点，起点取其之后
    - extend_to_user=True 时返回首个 user 消息的下标

    Args:
        messages: 消息列表（一般为已截取的窗口）。
        extend_to_user: 为 True 时返回首个 user 消息的下标。

    Returns:
        合法的起始下标。
    """
    start = 0
    called = set()
    for idx, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            for tool in message.tool_calls:
                called.add(tool.id)
        if message.role == "tool" and message.tool_call_id not in called:
            start = idx + 1
            called.clear()
        if extend_to_user and message.role == "user":
            return idx
    return start
