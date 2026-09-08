"""Run events and an in-process publisher used by CLI/Web adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from wiki_agent.events.hooks import AgentHook, CommandProgress, RunContext


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Serializable event emitted during one Agent run."""

    run_id: str
    session_id: str
    sequence: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(UTC).isoformat())


class EventPublisher(AgentHook):
    """Publish Agent lifecycle events to in-process subscribers.

    The publisher is an AgentHook, so it can be composed with the terminal
    renderer.  It deliberately knows nothing about HTTP or SSE.
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        super().__init__()
        if queue_size < 1:
            raise ValueError("queue_size 必须至少为 1")
        self._queue_size = queue_size
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self, context: RunContext, event_type: str, data: dict[str, Any] | None = None
    ):
        """Create and fan out one event to subscribers of its run."""
        event = AgentEvent(
            run_id=context.run_id,
            session_id=context.session_key,
            sequence=context.next_sequence(),
            type=event_type,
            data=data or {},
        )
        async with self._lock:
            queues = tuple(self._subscribers.get(context.run_id, ()))
        for queue in queues:
            await queue.put(event)
        return event

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[asyncio.Queue[AgentEvent]]:
        """Subscribe to future events for one run."""
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.setdefault(run_id, set()).add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(run_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(run_id, None)

    async def _emit(self, context: RunContext, event_type: str, **data: Any) -> None:
        await self.publish(context, event_type, data)

    async def on_status(self, context: RunContext, status: str) -> None:
        await self._emit(context, "status", status=status)

    async def on_run_start(self, context: RunContext) -> None:
        await self._emit(context, "run_started")

    async def on_run_end(self, context: RunContext) -> None:
        await self._emit(context, "run_finished", stop_reason=context.stop_reason)

    async def on_run_error(self, context: RunContext) -> None:
        await self._emit(context, "run_error", error=context.error)

    async def on_command_start(self, context: RunContext, command: str, task_id: str) -> None:
        await self._emit(context, "command_started", command=command, task_id=task_id)

    async def on_command_progress(self, context: RunContext, progress: CommandProgress) -> None:
        await self._emit(context, "command_progress", **asdict(progress))

    async def on_command_end(
        self, context: RunContext, command: str, task_id: str, result: Any
    ) -> None:
        await self._emit(
            context, "command_finished", command=command, task_id=task_id, result=str(result)
        )

    async def on_command_error(
        self, context: RunContext, command: str, task_id: str, error: Any
    ) -> None:
        await self._emit(
            context, "command_error", command=command, task_id=task_id, error=str(error)
        )

    async def on_command_cancelled(self, context: RunContext, command: str, task_id: str) -> None:
        await self._emit(context, "command_cancelled", command=command, task_id=task_id)

    async def on_iteration_start(self, context: RunContext) -> None:
        await self._emit(context, "iteration_started")

    async def on_iteration_end(self, context: RunContext) -> None:
        await self._emit(context, "iteration_finished")

    async def on_stream_delta(self, context: RunContext, delta: str) -> None:
        await self._emit(context, "text_delta", delta=delta)

    async def on_stream_end(self, context: RunContext) -> None:
        await self._emit(context, "text_finished")

    async def on_tool_call_start(
        self, context: RunContext, tool_name: str, tool_call_id: str, arguments: dict[str, Any]
    ) -> None:
        await self._emit(
            context,
            "tool_started",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
        )

    async def on_tool_result(
        self, context: RunContext, tool_name: str, tool_call_id: str, result: Any
    ) -> None:
        await self._emit(
            context,
            "tool_finished",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=str(result),
        )

    async def on_tool_error(
        self, context: RunContext, tool_name: str, tool_call_id: str, error: Any
    ) -> None:
        await self._emit(
            context,
            "tool_error",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            error=str(error),
        )

    async def on_reasoning_start(self, context: RunContext) -> None:
        await self._emit(context, "reasoning_started")

    async def on_reasoning_delta(self, context: RunContext, delta: str) -> None:
        await self._emit(context, "reasoning_delta", delta=delta)

    async def on_reasoning_end(self, context: RunContext) -> None:
        await self._emit(context, "reasoning_finished")
