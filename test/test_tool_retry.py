"""工具副作用等级与取消/重试边界测试。"""

import asyncio

from wiki_agent.errors import RetryableError
from wiki_agent.tools.base import BaseTool
from wiki_agent.tools.registry import ToolRegistry
from wiki_agent.tools.wiki_tools import Grep, ListDir, ReadFile


async def _execute(tool: BaseTool, **params) -> str:
    registry = ToolRegistry()
    registry.register(tool)
    return await registry.execute(tool.name, params)


class _ReadOnlyFlaky(BaseTool):
    name = "read_only_flaky"
    description = "test"
    parameters = {"type": "object", "properties": {}}
    retry_attempts = 3
    retry_base_delay = 0.001

    def __init__(self):
        self.calls = 0

    async def execute_once(self):
        self.calls += 1
        if self.calls < 3:
            raise RetryableError("temporary")
        return "ok"


class _IrreversibleFlaky(_ReadOnlyFlaky):
    name = "irreversible_flaky"
    side_effect = "irreversible"


class _IdempotentFlaky(_ReadOnlyFlaky):
    name = "idempotent_flaky"
    side_effect = "idempotent_write"
    idempotency_key_param = "operation_id"

    async def execute_once(self, operation_id=""):
        return await super().execute_once()


def test_read_only_retries_transient_error():
    async def run():
        tool = _ReadOnlyFlaky()
        result = await _execute(tool)
        return tool, result

    tool, result = asyncio.run(run())
    assert result == "ok"
    assert tool.calls == 3


def test_irreversible_does_not_retry():
    async def run():
        tool = _IrreversibleFlaky()
        result = await _execute(tool)
        return tool, result

    tool, result = asyncio.run(run())
    assert "code: transient_unavailable" in result
    assert "next_action:" in result
    assert tool.calls == 1


def test_idempotent_write_needs_operation_key_for_retry():
    async def run():
        without_key = _IdempotentFlaky()
        without_result = await _execute(without_key)
        with_key = _IdempotentFlaky()
        with_result = await _execute(with_key, operation_id="op-1")
        return without_key, without_result, with_key, with_result

    without_key, without_result, with_key, with_result = asyncio.run(run())
    assert without_key.calls == 1
    assert "temporary" in without_result
    assert with_key.calls == 3
    assert with_result == "ok"


def test_cancelled_error_is_not_retried():
    class _Cancelled(BaseTool):
        name = "cancelled"
        description = "test"
        parameters = {"type": "object", "properties": {}}

        def __init__(self):
            self.calls = 0

        async def execute_once(self):
            self.calls += 1
            raise asyncio.CancelledError

    async def run():
        tool = _Cancelled()
        try:
            await _execute(tool)
        except asyncio.CancelledError:
            return tool
        raise AssertionError("CancelledError 必须继续传播")

    tool = asyncio.run(run())
    assert tool.calls == 1


def test_fatal_error_gives_safe_actionable_result():
    class _Broken(BaseTool):
        name = "broken"
        description = "test"
        parameters = {"type": "object", "properties": {}}

        async def execute_once(self):
            raise RuntimeError("secret internal path")

    result = asyncio.run(_execute(_Broken()))
    assert "code: internal_error" in result
    assert "secret internal path" not in result
    assert "next_action:" in result


def test_wiki_tools_return_actionable_input_errors(tmp_path):
    (tmp_path / "concepts").mkdir()

    read_result = asyncio.run(_execute(ReadFile(tmp_path), file_path="../secret.md"))
    assert "code: invalid_path" in read_result
    assert "next_action:" in read_result

    list_result = asyncio.run(_execute(ListDir(tmp_path), dir_path="missing"))
    assert "code: not_a_directory" in list_result
    assert "ListDir" in list_result or "目录" in list_result

    grep_result = asyncio.run(_execute(Grep(tmp_path), pattern="["))
    assert "code: invalid_pattern" in grep_result


def test_internal_logs_sources_and_git_files_are_not_readable(tmp_path):
    (tmp_path / ".logs" / "runs").mkdir(parents=True)
    (tmp_path / ".logs" / "runs" / "run.md").write_text("internal", encoding="utf-8")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "source.md").write_text("provenance", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "internal.md").write_text("git internals", encoding="utf-8")
    (tmp_path / "concepts").mkdir()
    (tmp_path / "concepts" / "page.md").write_text("public", encoding="utf-8")

    read_logs = asyncio.run(_execute(ReadFile(tmp_path), file_path=".logs/runs/run.md"))
    read_sources = asyncio.run(_execute(ReadFile(tmp_path), file_path="sources/source.md"))
    read_git = asyncio.run(_execute(ReadFile(tmp_path), file_path=".git/internal.md"))
    listed = asyncio.run(_execute(ListDir(tmp_path), dir_path=""))
    searched = asyncio.run(_execute(Grep(tmp_path), pattern="internal"))

    assert "code: invalid_path" in read_logs
    assert "code: invalid_path" in read_sources
    assert "code: invalid_path" in read_git
    assert ".logs" not in listed
    assert ".git" not in listed
    assert "sources" not in listed
    assert "run.md" not in searched


def test_registry_retries_idempotent_write_only_with_key():
    async def run():
        without_key = _IdempotentFlaky()
        registry = ToolRegistry()
        registry.register(without_key)
        without_result = await registry.execute(without_key.name, {})

        with_key = _IdempotentFlaky()
        registry.register(with_key)
        with_result = await registry.execute(with_key.name, {"operation_id": "op-1"})
        return without_key, without_result, with_key, with_result

    without_key, without_result, with_key, with_result = asyncio.run(run())
    assert without_key.calls == 1
    assert "temporary" in without_result
    assert with_key.calls == 3
    assert with_result == "ok"


def test_registry_opens_circuit_after_final_transient_failures():
    class _DownstreamDown(BaseTool):
        name = "downstream_down"
        description = "test"
        parameters = {"type": "object", "properties": {}}
        retry_attempts = 1
        breaker_failure_threshold = 2
        retry_base_delay = 0.001

        def __init__(self):
            self.calls = 0

        async def execute_once(self):
            self.calls += 1
            raise RetryableError("dependency unavailable")

    async def run():
        tool = _DownstreamDown()
        registry = ToolRegistry()
        registry.register(tool)
        first = await registry.execute(tool.name, {})
        second = await registry.execute(tool.name, {})
        rejected = await registry.execute(tool.name, {})
        return tool, first, second, rejected

    tool, first, second, rejected = asyncio.run(run())
    assert "code: transient_unavailable" in first
    assert "code: transient_unavailable" in second
    assert "code: circuit_open" in rejected
    assert tool.calls == 2


def test_registry_timeout_is_transient_and_can_open_circuit():
    class _Slow(BaseTool):
        name = "slow_tool"
        description = "test"
        parameters = {"type": "object", "properties": {}}
        retry_attempts = 1
        timeout_seconds = 0.001
        breaker_failure_threshold = 1

        async def execute_once(self):
            await asyncio.sleep(0.05)
            return "too late"

    async def run():
        tool = _Slow()
        registry = ToolRegistry()
        registry.register(tool)
        timed_out = await registry.execute(tool.name, {})
        rejected = await registry.execute(tool.name, {})
        return timed_out, rejected

    timed_out, rejected = asyncio.run(run())
    assert "code: transient_unavailable" in timed_out
    assert "code: circuit_open" in rejected


def test_registry_cancel_does_not_count_as_circuit_failure():
    class _Cancelled(BaseTool):
        name = "registry_cancelled"
        description = "test"
        parameters = {"type": "object", "properties": {}}
        breaker_failure_threshold = 1

        def __init__(self):
            self.calls = 0

        async def execute_once(self):
            self.calls += 1
            raise asyncio.CancelledError

    async def run():
        tool = _Cancelled()
        registry = ToolRegistry()
        registry.register(tool)
        for _ in range(2):
            try:
                await registry.execute(tool.name, {})
            except asyncio.CancelledError:
                pass
        return tool

    tool = asyncio.run(run())
    assert tool.calls == 2
