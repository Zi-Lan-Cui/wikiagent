"""retry 层空响应路径回归测试——最后一次尝试空响应时 check_ok 必须为 False。

审计发现: 空响应分支 continue 跳过 check 回调，check_ok 默认 True，
调用方漏过空内容（Searcher 静默降级 0 候选 / Analyzer 产出空分析）。
"""

import asyncio
from types import SimpleNamespace

from wiki_agent.conversation import LLMResponse, Message
from wiki_agent.llm.retry import async_invoke_with_retry


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
            max_retries=2,
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
            max_retries=2,
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
            max_retries=2,
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
                max_retries=3,
                base_delay=0.01,
            )
        except asyncio.CancelledError:
            return client
        raise AssertionError("CancelledError 必须继续传播")

    client = asyncio.run(run())
    assert client.calls == 1


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
