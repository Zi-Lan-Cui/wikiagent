"""Test cases for wiki_agent.message — Message, ToolCall, LLMResponse, ChatHistory.

Run:
    cd /home/zilan/桌面/wiki_agent
    uv run pytest test/test_messages.py -v
"""

from datetime import datetime

import pytest

from wiki_agent.message import (
    LLMResponse,
    Message,
    ToolCall,
)


# ════════════════════════════════════════════════════════════════
#  ToolCall
# ════════════════════════════════════════════════════════════════

class TestToolCall:
    def test_create(self):
        tc = ToolCall(id="call_1", name="search", arguments={"q": "hello"})
        assert tc.id == "call_1"
        assert tc.name == "search"
        assert tc.arguments == {"q": "hello"}

    def test_empty_arguments(self):
        tc = ToolCall(id="call_2", name="get_time", arguments={})
        assert tc.arguments == {}


# ════════════════════════════════════════════════════════════════
#  Message — construction
# ════════════════════════════════════════════════════════════════

class TestMessageConstruction:
    def test_defaults(self):
        msg = Message(role="user")
        assert msg.role == "user"
        assert msg.content == ""
        assert msg.images == []
        assert msg.tool_calls == []
        assert msg.tool_call_id == ""
        assert msg.metadata.time_stamp  # MessageMeta——时间是元数据
        assert msg.created_at is not None  # 查询接口

    def test_user_message(self):
        msg = Message(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"

    def test_assistant_with_tool_calls(self):
        tc = ToolCall(id="c1", name="search", arguments={"q": "x"})
        msg = Message(role="assistant", content="", tool_calls=[tc])
        assert msg.role == "assistant"
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].id == "c1"

    def test_tool_result_message(self):
        msg = Message(role="tool", content="result text", tool_call_id="call_9")
        assert msg.role == "tool"
        assert msg.tool_call_id == "call_9"
        assert msg.content == "result text"

    def test_system_message(self):
        msg = Message(role="system", content="You are helpful.")
        assert msg.role == "system"

    def test_with_images(self):
        msg = Message(role="user", content="describe", images=["abc", "def"])
        assert msg.images == ["abc", "def"]
        assert msg.content == "describe"

    def test_content_empty_by_default(self):
        msg = Message(role="user", images=["abc"])
        assert msg.content == ""


# ════════════════════════════════════════════════════════════════
#  Message — openai_schema
# ════════════════════════════════════════════════════════════════

class TestOpenAISchema:
    # ── 纯文本 ──────────────────────────────────────────

    def test_simple_user(self):
        schema = Message(role="user", content="hi").openai_schema
        assert schema == {"role": "user", "content": "hi"}

    def test_assistant_text_only(self):
        schema = Message(role="assistant", content="ok").openai_schema
        assert schema == {"role": "assistant", "content": "ok"}

    def test_system_message(self):
        schema = Message(role="system", content="prompt").openai_schema
        assert schema == {"role": "system", "content": "prompt"}

    # ── 图片 ────────────────────────────────────────────

    def test_user_with_one_image(self):
        msg = Message(role="user", content="看图", images=["img_b64"])
        schema = msg.openai_schema
        assert schema["role"] == "user"
        assert isinstance(schema["content"], list)
        assert schema["content"][0] == {"type": "text", "text": "看图"}
        img_part = schema["content"][1]
        assert img_part["type"] == "image_url"
        assert img_part["image_url"]["url"] == "data:image/png;base64,img_b64"

    def test_user_with_multiple_images(self):
        msg = Message(role="user", content="对比", images=["a", "b", "c"])
        schema = msg.openai_schema
        assert len(schema["content"]) == 4  # 1 text + 3 images
        for i in range(3):
            assert schema["content"][i + 1]["type"] == "image_url"

    def test_images_are_ignored_for_non_user_role(self):
        """images 字段存在也不影响 tool/assistant/system 的 openai_schema。

        目前 _build_content 对任意 role 都生效——文档约定 images 仅用于 user。
        这个测试记录当前行为，如果未来加了 role 检查，更新此测试。
        """
        msg = Message(role="assistant", content="done", images=["x"])
        schema = msg.openai_schema
        # assistant 不会携带图片，但 schema 不做 role 判断
        assert isinstance(schema["content"], list)

    # ── tool_calls ──────────────────────────────────────

    def test_message_with_tool_calls(self):
        tc = ToolCall(id="t1", name="calc", arguments={"expr": "1+1"})
        msg = Message(role="assistant", content="", tool_calls=[tc])
        schema = msg.openai_schema

        assert schema["role"] == "assistant"
        assert schema["content"] == ""
        assert "tool_calls" in schema
        assert len(schema["tool_calls"]) == 1
        assert schema["tool_calls"][0] == {
            "id": "t1",
            "type": "function",
            "function": {
                "name": "calc",
                "arguments": '{"expr": "1+1"}',
            },
        }

    def test_tool_calls_arguments_preserve_non_ascii(self):
        tc = ToolCall(id="t2", name="translate", arguments={"text": "中文"})
        msg = Message(role="assistant", content="", tool_calls=[tc])
        args_json = msg.openai_schema["tool_calls"][0]["function"]["arguments"]
        assert "中文" in args_json

    def test_multiple_tool_calls(self):
        tcs = [
            ToolCall(id="a", name="search", arguments={"q": "x"}),
            ToolCall(id="b", name="fetch", arguments={"url": "/"}),
        ]
        schema = Message(role="assistant", content="", tool_calls=tcs).openai_schema
        assert len(schema["tool_calls"]) == 2
        assert schema["tool_calls"][0]["id"] == "a"
        assert schema["tool_calls"][1]["id"] == "b"

    # ── 组合：text + images + tool_calls ─────────────────

    def test_text_with_images_and_tool_calls(self):
        """assistant 带 content + tool_calls，同时 images 字段不为空。"""
        tc = ToolCall(id="c", name="run", arguments={})
        msg = Message(role="assistant", content="thinking", tool_calls=[tc], images=["img"])
        schema = msg.openai_schema

        assert schema["role"] == "assistant"
        assert isinstance(schema["content"], list)
        assert "tool_calls" in schema

    # ── tool 角色 ───────────────────────────────────────

    def test_tool_result(self):
        schema = Message(
            role="tool", content="result", tool_call_id="call_7",
        ).openai_schema
        assert schema == {
            "role": "tool",
            "tool_call_id": "call_7",
            "content": "result",
        }

    def test_tool_result_with_images_ignored(self):
        """tool 消息忽略 images，和 assistant 不同。

        tool role 的 schema 是固定格式，不走 _build_content。
        """
        msg = Message(role="tool", content="done", tool_call_id="x", images=["img"])
        schema = msg.openai_schema
        assert schema["content"] == "done"  # 纯文本，没变成数组
        assert "image_url" not in str(schema)

    # ── 边界 ────────────────────────────────────────────

    def test_empty_images_produces_plain_content(self):
        msg = Message(role="user", content="hi", images=[])
        schema = msg.openai_schema
        assert schema["content"] == "hi"  # 字符串，非列表

    def test_no_images_field(self):
        msg = Message(role="user", content="hi")
        schema = msg.openai_schema
        assert schema["content"] == "hi"

    def test_content_empty_but_has_images(self):
        msg = Message(role="user", content="", images=["img"])
        parts = msg.openai_schema["content"]
        assert parts[0] == {"type": "text", "text": ""}
        assert parts[1]["type"] == "image_url"


# ════════════════════════════════════════════════════════════════
#  Message — text_schema
# ════════════════════════════════════════════════════════════════

class TestTextSchema:
    def test_user_text_structure(self):
        msg = Message(role="user", content="hello")
        text = msg.text_schema
        assert "user" in text
        assert "hello" in text

    def test_assistant_without_tool_calls(self):
        msg = Message(role="assistant", content="reply")
        text = msg.text_schema
        assert "assistant" in text
        assert "reply" in text

    def test_assistant_with_tool_calls(self):
        tc = ToolCall(id="x", name="search", arguments={"q": "test"})
        msg = Message(role="assistant", content="", tool_calls=[tc])
        text = msg.text_schema
        # json.dumps 默认 separators=(", ", ": ")，会带空格
        assert "search" in text
        assert "test" in text

    def test_tool_message(self):
        msg = Message(role="tool", content="output", tool_call_id="call_3")
        text = msg.text_schema
        assert "tool" in text
        assert "call_3" in text
        assert "output" in text

    def test_system_message(self):
        msg = Message(role="system", content="prompt")
        text = msg.text_schema
        assert "system" in text
        assert "prompt" in text

    def test_text_schema_excludes_timestamp(self):
        """text_schema 与 openai_schema 内容对齐——时间是元数据不进内容
        （旧契约含时间戳已反转：估计文本与真实发送内容同形）。"""
        msg = Message(role="user", content="hi")
        assert "T" not in msg.text_schema  # ISO datetime 分隔符不在内容里
        assert msg.text_schema == "user:hi\n"
        # 时间经查询接口取用（TTL 消费者）
        assert msg.created_at is not None

    def test_multiple_tool_calls_in_text(self):
        tcs = [
            ToolCall(id="1", name="f1", arguments={}),
            ToolCall(id="2", name="f2", arguments={}),
        ]
        msg = Message(role="assistant", content="pre", tool_calls=tcs)
        text = msg.text_schema
        assert "f1" in text
        assert "f2" in text


# ════════════════════════════════════════════════════════════════
#  Message — create_from_openai (逆序列化)
# ════════════════════════════════════════════════════════════════

class TestCreateFromOpenAI:
    def test_roundtrip_user_message(self):
        original = Message(role="user", content="hello world")
        serialized = original.openai_schema
        restored = Message.create_from_openai(serialized)
        assert restored.role == "user"
        assert restored.content == "hello world"
        assert restored.tool_calls == []
        assert restored.images == []

    def test_roundtrip_assistant_with_tool_call(self):
        original = Message(
            role="assistant",
            content="调用工具",
            tool_calls=[ToolCall(id="t99", name="calc", arguments={"x": 1})],
        )
        restored = Message.create_from_openai(original.openai_schema)
        assert restored.role == "assistant"
        assert restored.content == "调用工具"
        assert len(restored.tool_calls) == 1
        assert restored.tool_calls[0].id == "t99"
        assert restored.tool_calls[0].name == "calc"
        assert restored.tool_calls[0].arguments == {"x": 1}

    def test_roundtrip_tool_message(self):
        original = Message(role="tool", content="result 42", tool_call_id="abc")
        restored = Message.create_from_openai(original.openai_schema)
        assert restored.role == "tool"
        assert restored.content == "result 42"
        assert restored.tool_call_id == "abc"

    def test_roundtrip_system_message(self):
        original = Message(role="system", content="system prompt")
        restored = Message.create_from_openai(original.openai_schema)
        assert restored.role == "system"
        assert restored.content == "system prompt"

    def test_multimodal_content_reduced_to_text(self):
        """多模态 content 数组还原为纯文本（图片信息丢失，按设计）。"""
        data = {
            "role": "user",
            "content": [
                {"type": "text", "text": "第一段"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
                {"type": "text", "text": "第二段"},
            ],
        }
        msg = Message.create_from_openai(data)
        assert msg.content == "第一段\n第二段"
        assert msg.images == []

    def test_no_tool_calls_field(self):
        msg = Message.create_from_openai({"role": "assistant", "content": "ok"})
        assert msg.tool_calls == []

    def test_missing_content_defaults_to_empty(self):
        msg = Message.create_from_openai({"role": "user"})
        assert msg.content == ""

    def test_multiple_tool_calls_roundtrip(self):
        original = Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="1", name="a", arguments={"k": "v"}),
                ToolCall(id="2", name="b", arguments={}),
            ],
        )
        restored = Message.create_from_openai(original.openai_schema)
        assert len(restored.tool_calls) == 2
        assert restored.tool_calls[0].name == "a"
        assert restored.tool_calls[1].name == "b"


# ════════════════════════════════════════════════════════════════
#  LLMResponse
# ════════════════════════════════════════════════════════════════

class TestLLMResponse:
    def test_defaults(self):
        resp = LLMResponse()
        assert resp.role == "assistant"
        assert resp.content == ""
        assert resp.tool_calls == []
        assert resp.finish_reason == ""
        assert resp.usage == {}

    def test_with_content(self):
        resp = LLMResponse(content="hello", finish_reason="stop")
        assert resp.content == "hello"
        assert resp.finish_reason == "stop"

    def test_with_tool_calls(self):
        tcs = [ToolCall(id="x", name="run", arguments={})]
        resp = LLMResponse(content="", tool_calls=tcs)
        assert len(resp.tool_calls) == 1

    def test_with_usage(self):
        resp = LLMResponse(usage={"prompt": 10, "completion": 20, "total": 30})
        assert resp.usage["total"] == 30


# ════════════════════════════════════════════════════════════════
#  Integration — 模拟一次对话的完整 schema 流转
# ════════════════════════════════════════════════════════════════

class TestFullConversationSchema:
    def test_multimodal_conversation(self):
        """模拟用户发图提问 → assistant 调用工具 → tool 返回结果。"""
        messages = [
            Message(role="system", content="You are a visual assistant."),
            Message(
                role="user",
                content="这张图里有什么？",
                images=["fake_base64_image_data"],
            ),
        ]

        # system
        assert messages[0].openai_schema["content"] == "You are a visual assistant."

        # user (multimodal)
        user_content = messages[1].openai_schema["content"]
        assert isinstance(user_content, list)
        assert user_content[0]["text"] == "这张图里有什么？"
        assert user_content[1]["type"] == "image_url"

        # assistant (with tool_call)
        messages.append(Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tc1", name="vision", arguments={"action": "describe"})],
        ))
        asst_schema = messages[2].openai_schema
        assert asst_schema["role"] == "assistant"
        assert len(asst_schema["tool_calls"]) == 1

        # tool result
        messages.append(Message(
            role="tool",
            content="一只猫坐在窗台上",
            tool_call_id="tc1",
        ))
        tool_schema = messages[3].openai_schema
        assert tool_schema["role"] == "tool"
        assert tool_schema["tool_call_id"] == "tc1"
        assert tool_schema["content"] == "一只猫坐在窗台上"

    def test_roundtrip_full_cycle(self):
        """从原始 Message → openai_schema → create_from_openai → openai_schema。"""
        turn = [
            Message(role="system", content="sys"),
            Message(role="user", content="question"),
            Message(
                role="assistant",
                content="answer",
                tool_calls=[ToolCall(id="t", name="fn", arguments={"a": 1})],
            ),
            Message(role="tool", content="result", tool_call_id="t"),
        ]

        for original in turn:
            restored = Message.create_from_openai(original.openai_schema)
            assert restored.role == original.role
            assert restored.content == original.content
            assert len(restored.tool_calls) == len(original.tool_calls)
            assert restored.tool_call_id == original.tool_call_id
