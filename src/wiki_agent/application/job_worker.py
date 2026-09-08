"""Single durable worker for application jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from wiki_agent.application.job_service import JobService
from wiki_agent.state import Job

JobHandler = Callable[[Job, Callable[[str], None]], Awaitable[None]]


class JobWorker:
    """Claim and execute persisted jobs serially.

    The database claim is the source of truth; the asyncio task only provides
    a local polling loop and can be recreated after a process restart.
    """

    def __init__(self, service: JobService, *, poll_interval: float = 0.5):
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须大于 0")
        self.service = service
        self.poll_interval = poll_interval
        self._handlers: dict[str, JobHandler] = {}
        self._stopped = asyncio.Event()

    def register(self, kind: str, handler: JobHandler) -> None:
        if not kind.strip():
            raise ValueError("kind 不能为空")
        if kind in self._handlers:
            raise ValueError(f"重复注册 Job handler: {kind}")
        self._handlers[kind] = handler

    async def run_once(self) -> Job | None:
        job = self.service.claim_next()
        if job is None:
            return None
        handler = self._handlers.get(job.kind)
        if handler is None:
            self.service.fail(job.id, f"未注册 Job 类型: {job.kind}")
            return job

        def progress(stage: str) -> None:
            self.service.mark_stage(job.id, stage)

        try:
            await handler(job, progress)
        except asyncio.CancelledError:
            self.service.cancel(job.id)
            raise
        except Exception as exc:  # noqa: BLE001 - worker must persist all failures
            self.service.fail(job.id, f"{type(exc).__name__}: {exc}")
        else:
            self.service.succeed(job.id)
        return job

    async def run(self) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            if await self.run_once() is None:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_interval)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopped.set()
