"""wiki_agent.conversation 的用例：Message、ToolCall、LLMResponse 的构造与 schema 输出。

直接运行:  .venv/bin/python -m pytest test/test_messages.py -v
"""

from wiki_agent.conversation import (
    LLMResponse,
    Message,
    ToolCall,
)


class TestToolCall:
    def test_create(self):
        tc = ToolCall(id="call_1", name="search", arguments={"q": "hello"})
        assert tc.id == "call_1"
        assert tc.name == "search"
        assert tc.arguments == {"q": "hello"}

    def test_empty_arguments(self):
        tc = ToolCall(id="call_2", name="get_time", arguments={})
        assert tc.arguments == {}


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


class TestOpenAISchema:
    # 纯文本

    def test_simple_user(self):
        schema = Message(role="user", content="hi").openai_schema
        assert schema == {"role": "user", "content": "hi"}

    def test_assistant_text_only(self):
        schema = Message(role="assistant", content="ok").openai_schema
        assert schema == {"role": "assistant", "content": "ok"}

    def test_system_message(self):
        schema = Message(role="system", content="prompt").openai_schema
        assert schema == {"role": "system", "content": "prompt"}

    # 图片

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

        _build_content 对任意 role 都生效，按约定 images 仅用于 user。
        """
        msg = Message(role="assistant", content="done", images=["x"])
        schema = msg.openai_schema
        # assistant 不会携带图片，但 schema 不做 role 判断
        assert isinstance(schema["content"], list)

    # tool_calls

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

    # 组合：text + images + tool_calls

    def test_text_with_images_and_tool_calls(self):
        """assistant 带 content + tool_calls，同时 images 字段不为空。"""
        tc = ToolCall(id="c", name="run", arguments={})
        msg = Message(role="assistant", content="thinking", tool_calls=[tc], images=["img"])
        schema = msg.openai_schema

        assert schema["role"] == "assistant"
        assert isinstance(schema["content"], list)
        assert "tool_calls" in schema

    # tool 角色

    def test_tool_result(self):
        schema = Message(
            role="tool",
            content="result",
            tool_call_id="call_7",
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

    # 边界

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
        """text_schema 与 openai_schema 内容对齐——时间是元数据，不进内容。"""
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


# 集成：模拟一次对话的完整 schema 流转


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

        # user 多模态
        user_content = messages[1].openai_schema["content"]
        assert isinstance(user_content, list)
        assert user_content[0]["text"] == "这张图里有什么？"
        assert user_content[1]["type"] == "image_url"

        # assistant 带 tool_calls
        messages.append(
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="tc1", name="vision", arguments={"action": "describe"})],
            )
        )
        asst_schema = messages[2].openai_schema
        assert asst_schema["role"] == "assistant"
        assert len(asst_schema["tool_calls"]) == 1

        # tool 结果
        messages.append(
            Message(
                role="tool",
                content="一只猫坐在窗台上",
                tool_call_id="tc1",
            )
        )
        tool_schema = messages[3].openai_schema
        assert tool_schema["role"] == "tool"
        assert tool_schema["tool_call_id"] == "tc1"
        assert tool_schema["content"] == "一只猫坐在窗台上"
