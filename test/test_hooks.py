"""hook 协议回归：CompositeHook 转发必须兼容 react 调用侧的关键字参数。

react._execute_tools 以 context=/tool_name=/tool_call_id=/arguments= 关键字
调用 tool 三个 hook；两个以上 hook 组合时走 CompositeHook 转发，参数名
不一致会直接 TypeError（真实问答工具路径全断）。
"""

import asyncio

from wiki_agent.events import AgentHook, CompositeHook


class _Recorder(AgentHook):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    async def on_tool_call_start(self, context, tool_name, tool_call_id, arguments):
        self.calls.append(("start", {"t": tool_name, "id": tool_call_id, "a": arguments}))

    async def on_tool_result(self, context, tool_name, tool_call_id, result):
        self.calls.append(("result", {"t": tool_name, "id": tool_call_id, "r": result}))

    async def on_tool_error(self, context, tool_name, tool_call_id, error):
        self.calls.append(("error", {"t": tool_name, "id": tool_call_id, "e": str(error)}))


def test_composite_forwards_tool_hooks_with_keyword_args():
    rec = _Recorder()
    composite = CompositeHook([rec, _Recorder()])

    async def go():
        await composite.on_tool_call_start(
            context=None, tool_name="read_page", tool_call_id="tc1", arguments={"path": "a"}
        )
        await composite.on_tool_result(
            context=None, tool_name="read_page", tool_call_id="tc1", result="ok"
        )
        await composite.on_tool_error(
            context=None, tool_name="read_page", tool_call_id="tc2", error=ValueError("x")
        )

    asyncio.run(go())
    assert [c[0] for c in rec.calls] == ["start", "result", "error"]
    assert rec.calls[0][1] == {"t": "read_page", "id": "tc1", "a": {"path": "a"}}
