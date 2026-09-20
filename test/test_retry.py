"""统一 LLM 重试层的契约回归测试。

覆盖: 空响应必须标记校验失败、耗尽混合态、Fatal/Cancelled 穿透、
耗尽异常消息格式、重试回调语义、通用面与流式重试提示。
"""

import asyncio
from types import SimpleNamespace

from wiki_agent.config import RetryConfig
from wiki_agent.conversation import LLMResponse, Message, ToolCall
from wiki_agent.errors import FatalError, RetryableError
from wiki_agent.events import RunContext
from wiki_agent.llm.retry import async_invoke_with_retry, retry_llm_call

_FAST = RetryConfig(llm_max_attempts=3, llm_base_delay_seconds=0.001)


class _FlakyClient:
    """按脚本逐项返回响应或抛异常，并记录每次收到的消息（验证修正消息追加）。"""

    model_id = "mock"

    def __init__(self, items: list):
        self._items = list(items)
        self.calls = 0
        self.seen_messages: list[list[Message]] = []
        self.retry_config = SimpleNamespace(llm_max_attempts=3, llm_base_delay_seconds=0.01)

    async def async_invoke(self, messages, **kw):
        self.calls += 1
        self.seen_messages.append(list(messages))
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _RecordingSleep:
    """临时替换重试内核的退避睡眠，记录发生次数（断言"零 sleep"用）。"""

    def __init__(self):
        self.calls: list[int] = []

    def __enter__(self):
        import wiki_agent.llm.retry as mod

        self._mod = mod
        self._orig = mod._sleep_backoff

        async def fake(attempt: int, delay: float):
            self.calls.append(attempt)

        mod._sleep_backoff = fake
        return self

    def __exit__(self, *exc):
        self._mod._sleep_backoff = self._orig


class _ScriptedClient:
    """按脚本序列返回响应——第一次坏 JSON，第二次空内容。"""

    model_id = "mock"

    def __init__(self, responses: list[LLMResponse]):
        self._responses = responses
        self.calls = 0
        self.retry_config = SimpleNamespace(llm_max_attempts=3, llm_base_delay_seconds=0.01)

    async def async_invoke(
        self, messages, tools=None, max_tokens=None, temperature=0.5, extra_body=None
    ):
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


def _check_json_array(content: str) -> tuple[bool, str]:
    import json

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return False, f"JSON 格式错误: {e}"
    return isinstance(data, list), "必须是数组"


def test_empty_last_attempt_marks_check_failed():
    """最后一次尝试是空响应 → check_ok=False（而不是默认 True 漏过）。"""
    client = _ScriptedClient(
        [
            LLMResponse(content="not json"),
            LLMResponse(content=""),  # 空响应——旧 bug 路径
        ]
    )

    async def run():
        return await async_invoke_with_retry(
            client,
            [Message(role="system", content="输出 JSON 数组")],
            check=_check_json_array,
            max_attempts=2,
            base_delay=0.01,
        )

    resp = asyncio.run(run())
    assert resp.check_ok is False, "空响应必须标记 check_ok=False"
    assert "为空" in resp.check_reason
    assert client.calls == 2


def test_empty_then_valid_retries():
    """空响应不是最后一次 → 继续重试直到校验通过。"""
    client = _ScriptedClient(
        [
            LLMResponse(content=""),
            LLMResponse(content='["a"]'),
        ]
    )

    async def run():
        return await async_invoke_with_retry(
            client,
            [Message(role="system", content="输出 JSON 数组")],
            check=_check_json_array,
            max_attempts=2,
            base_delay=0.01,
        )

    resp = asyncio.run(run())
    assert resp.check_ok is True
    assert resp.content == '["a"]'
    assert client.calls == 2


def test_all_attempts_exception_raises():
    """全部尝试都是异常 → RuntimeError 冒泡（调用方包装成 IngestError）。"""

    class _BoomClient:
        model_id = "mock"

        async def async_invoke(self, *a, **kw):
            from wiki_agent.errors import RetryableError

            raise RetryableError("timeout")

    async def run():
        return await async_invoke_with_retry(
            _BoomClient(),
            [Message(role="system", content="x")],
            max_attempts=2,
            base_delay=0.01,
        )

    try:
        asyncio.run(run())
        assert False, "应 raise RuntimeError"
    except RuntimeError as e:
        assert "2 次均失败" in str(e)


def test_client_retry_config_is_used_when_call_does_not_override_it():
    """默认重试次数来自注入的 RootConfig.retry，而非函数常量。"""

    class _BoomClient:
        model_id = "mock"
        retry_config = SimpleNamespace(llm_max_attempts=2, llm_base_delay_seconds=0.001)

        def __init__(self):
            self.calls = 0

        async def async_invoke(self, *args, **kwargs):
            from wiki_agent.errors import RetryableError

            self.calls += 1
            raise RetryableError("timeout")

    async def run():
        client = _BoomClient()
        try:
            await async_invoke_with_retry(client, [Message(role="system", content="x")])
        except RuntimeError:
            return client
        raise AssertionError("重试耗尽后应失败")

    assert asyncio.run(run()).calls == 2


def test_cancelled_error_is_not_retried():
    """LLM 调用被取消是控制流，不应进入重试循环。"""

    class _CancelledClient:
        model_id = "mock"

        def __init__(self):
            self.calls = 0

        async def async_invoke(self, *a, **kw):
            self.calls += 1
            raise asyncio.CancelledError

    async def run():
        client = _CancelledClient()
        try:
            await async_invoke_with_retry(
                client,
                [Message(role="system", content="x")],
                max_attempts=3,
                base_delay=0.01,
            )
        except asyncio.CancelledError:
            return client
        raise AssertionError("CancelledError 必须继续传播")

    client = asyncio.run(run())
    assert client.calls == 1


# 内核耗尽/穿透契约


def test_exhaustion_with_prior_invalid_response_returns_it():
    """此前拿到过校验失败响应、最后一次抛异常 → 返回该响应（非 RuntimeError）。"""
    client = _FlakyClient([LLMResponse(content="not json"), RetryableError("boom")])

    async def run():
        return await async_invoke_with_retry(
            client,
            [Message(role="user", content="x")],
            check=_check_json_array,
            max_attempts=2,
            base_delay=0.01,
        )

    resp = asyncio.run(run())
    assert resp.check_ok is False
    assert client.calls == 2


def test_fatal_propagates_despite_prior_response():
    """FatalError 必须穿透——不能被"返回此前的响应"兜底吞掉。"""
    client = _FlakyClient([LLMResponse(content="not json"), FatalError("401")])

    async def run():
        return await async_invoke_with_retry(
            client,
            [Message(role="user", content="x")],
            check=_check_json_array,
            max_attempts=2,
            base_delay=0.01,
        )

    try:
        asyncio.run(run())
        assert False, "应抛 FatalError"
    except FatalError:
        pass
    assert client.calls == 2


def test_check_callback_bug_propagates_raw():
    """check 回调自身抛错是 bug，原样冒泡，不伪装成 LLM 故障也不吃兜底响应。"""
    client = _FlakyClient([LLMResponse(content="x")])

    def _boom(content: str):
        raise ValueError("classify bug")

    async def run():
        return await async_invoke_with_retry(
            client,
            [Message(role="user", content="x")],
            check=_boom,
            max_attempts=2,
            base_delay=0.01,
        )

    try:
        asyncio.run(run())
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "classify bug" in str(e)


def test_runtime_message_format_by_exception_type():
    """RuntimeError 尾串两态: Retryable 用 str(exc)，未知异常带类型前缀。"""

    async def run(client, attempts=2):
        return await async_invoke_with_retry(
            client, [Message(role="user", content="x")], max_attempts=attempts, base_delay=0.01
        )

    try:
        asyncio.run(run(_FlakyClient([RetryableError("timeout"), RetryableError("timeout")])))
        assert False
    except RuntimeError as e:
        assert "LLM 调用 2 次均失败 - timeout" in str(e)

    try:
        asyncio.run(run(_FlakyClient([ValueError("boom"), ValueError("boom")])))
        assert False
    except RuntimeError as e:
        assert "LLM 调用 2 次均失败 - ValueError: boom" in str(e)


def test_single_attempt_exception_raises_runtime():
    """attempts=1、异常 → RuntimeError '1 次均失败'，无第二次调用。"""

    async def run():
        return await async_invoke_with_retry(
            _FlakyClient([RetryableError("t")]),
            [Message(role="user", content="x")],
            max_attempts=1,
            base_delay=5,
        )

    try:
        asyncio.run(run())
        assert False
    except RuntimeError as e:
        assert "1 次均失败" in str(e)


def test_single_attempt_rejected_returns_without_sleep():
    """attempts=1、校验拒绝 → 返回该响应且零 sleep（守卫 attempt<attempts-1）。"""
    client = _FlakyClient([LLMResponse(content="not json")])
    with _RecordingSleep() as recorder:

        async def run():
            return await async_invoke_with_retry(
                client,
                [Message(role="user", content="x")],
                check=_check_json_array,
                max_attempts=1,
                base_delay=5,
            )

        resp = asyncio.run(run())
    assert resp.check_ok is False
    assert client.calls == 1
    assert recorder.calls == []


def test_empty_attempt_does_not_append_fixup():
    """空响应重发同一请求；只有 check_failed 才追加修正消息（与旧实现一致）。"""
    client = _FlakyClient(
        [
            LLMResponse(content=""),  # 尝试1 空——不追加
            LLMResponse(content="not json"),  # 尝试2 校验失败——追加
            LLMResponse(content='["a"]'),  # 尝试3 通过
        ]
    )

    async def run():
        return await async_invoke_with_retry(
            client,
            [Message(role="user", content="输出 JSON 数组")],
            check=_check_json_array,
            max_attempts=3,
            base_delay=0.01,
        )

    resp = asyncio.run(run())
    assert resp.check_ok is True
    base = client.seen_messages[0]
    assert client.seen_messages[1] == base, "空响应不应追加修正消息"
    third = client.seen_messages[2]
    assert len(third) == len(base) + 2
    assert third[-2].role == "assistant" and third[-1].role == "user"
    assert "修正后重新输出" in third[-1].content


# retry_llm_call（react/流式适配面）


def test_call_with_retry_success_after_retryable():
    state = {"n": 0}

    async def call():
        state["n"] += 1
        if state["n"] == 1:
            raise RetryableError("t")
        return LLMResponse(content="ok")

    resp = asyncio.run(retry_llm_call(call, retry_config=_FAST))
    assert resp.content == "ok"
    assert state["n"] == 2


def test_call_with_retry_exhaustion_raises_original_instance():
    """耗尽抛回原异常对象本体——turn 边界依赖异常类型，不伪造包装。"""
    exc = RetryableError("net")

    async def call():
        raise exc

    try:
        asyncio.run(retry_llm_call(call, retry_config=_FAST))
        assert False
    except RetryableError as got:
        assert got is exc


def test_call_with_retry_accepts_empty_tool_call_response():
    """空内容 + 纯 tool_calls 是 ReAct 合法回合——直接接受，不重试。"""
    resp_in = LLMResponse(content="", tool_calls=[ToolCall(id="t1", name="Read", arguments={})])
    state = {"n": 0}

    async def call():
        state["n"] += 1
        return resp_in

    out = asyncio.run(retry_llm_call(call, retry_config=_FAST))
    assert out is resp_in
    assert state["n"] == 1


def test_call_with_retry_fatal_and_cancelled_single_call():
    for exc in (FatalError("401"), asyncio.CancelledError):
        state = {"n": 0}

        async def call():
            state["n"] += 1
            raise exc

        try:
            asyncio.run(retry_llm_call(call, retry_config=_FAST))
            assert False
        except (FatalError, asyncio.CancelledError):
            pass
        assert state["n"] == 1, f"{exc!r} 不应被重试"


def test_call_on_retry_contract():
    """on_retry 恰在每次退避前触发、最后一次失败不触发、attempt 从 1 计。"""
    events: list[tuple[int, int]] = []

    async def on_retry(attempt: int, total: int, exc: BaseException):
        events.append((attempt, len(_sleep_tracker.calls)))

    async def call():
        raise RetryableError("t")

    with _RecordingSleep() as _sleep_tracker:
        try:
            asyncio.run(retry_llm_call(call, retry_config=_FAST, on_retry=on_retry))
            assert False
        except RetryableError:
            pass
    assert events == [(1, 0), (2, 1)], "3 次尝试 = 2 次重试，各先于 sleep"


# react.py 薄壳（流式提示语义）


def _make_runner(llm, hooks):
    """够跑 _invoke/_stream 一圈的最小 fake agent（无 tool_calls → 不进工具分支）。"""
    from wiki_agent.agent.react import ReActRunner

    agent = SimpleNamespace(
        llm=llm,
        retry_config=_FAST,
        hooks=hooks,
        tool_registry=SimpleNamespace(get_all_schema_openai=lambda: []),
        agent_config=SimpleNamespace(max_tokens=10),
    )
    return ReActRunner(agent)  # type: ignore[arg-type]


class _RetryOnceLLM:
    """第一次抛 RetryableError、第二次成功——流式/非流式各一形态。"""

    model_id = "mock"

    def __init__(self):
        self.calls = 0

    async def async_stream(self, messages, *, tools, max_tokens, on_delta):
        self.calls += 1
        if self.calls == 1:
            raise RetryableError("t")
        return LLMResponse(content="hi")

    async def async_invoke(self, messages, *, tools, max_tokens):
        return await self.async_stream(messages, tools=tools, max_tokens=max_tokens, on_delta=None)


def test_react_stream_hint_exact_text():
    """流式重试提示走真实路径 _stream: 仅 RetryableError 触发、文案精确。"""
    hints: list[str] = []

    class _Hooks:
        async def on_stream_delta(self, ctx, text):
            hints.append(text)

    runner = _make_runner(_RetryOnceLLM(), _Hooks())
    messages: list[Message] = [Message(role="user", content="q")]
    session = SimpleNamespace(update_token_cost=lambda *a: None)
    ctx = RunContext(session_key="test")

    had_tools = asyncio.run(runner._stream(session, messages, list(messages), ctx))
    assert had_tools is False
    assert hints == ["_(网络抖动——重试中 1/2)_"], "attempt=1, total-1=2"
    assert messages[-1].content == "hi"


def test_react_non_stream_stays_silent():
    """非流式 _invoke 不注入 on_retry——屏幕无动态，静默退避。"""

    class _Hooks:
        async def on_stream_delta(self, ctx, text):
            raise AssertionError("非流式不应发提示")

    runner = _make_runner(_RetryOnceLLM(), _Hooks())
    messages: list[Message] = [Message(role="user", content="q")]
    session = SimpleNamespace(update_token_cost=lambda *a: None)
    ctx = RunContext(session_key="test")

    had_tools = asyncio.run(runner._invoke(session, messages, list(messages), ctx))
    assert had_tools is False
    assert messages[-1].content == "hi"


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
