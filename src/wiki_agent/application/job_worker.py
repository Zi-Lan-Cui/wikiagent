"""Single durable worker for application jobs.

Worker 是唯一的终态写入者：claim（按注册 kinds + 到期时间）→
handler 返回 JobResult → complete_with_outcome 单事务落终态。
handler 只表达业务结局；bug 抛异常由这里归为 transient 进链式退避。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from wiki_agent.application.job_results import JobResult
from wiki_agent.application.job_service import JobService
from wiki_agent.jobs import Job
from wiki_agent.log import get_logger

JobHandler = Callable[[Job, Callable[[str], None]], Awaitable[JobResult | None]]

logger = get_logger("JOB_WORKER")


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

    def is_registered(self, kind: str) -> bool:
        """装配点（web create_app 可能被多 TestClient 复用）幂等注册用。"""
        return kind in self._handlers

    @property
    def registered_kinds(self) -> set[str]:
        return set(self._handlers)

    async def run_once(self) -> Job | None:
        # kinds 过滤 = 多进程共库的分工边界：只领本进程注册了 handler 的类型
        job = self.service.claim_next(kinds=self.registered_kinds)
        if job is None:
            return None
        handler = self._handlers.get(job.kind)
        if handler is None:
            # kinds 过滤下理论不可达（外部显式 claim 的兜底），不重试
            self.service.complete_with_outcome(
                job,
                JobResult(
                    status="failed",
                    detail={"error": f"未注册 Job 类型: {job.kind}"},
                ),
            )
            return job

        def progress(stage: str) -> None:
            self.service.mark_stage(job.id, stage)

        try:
            result = await handler(job, progress)
        except asyncio.CancelledError:
            # 进程取消：终态单事务落库（无联动），再继续传播退出
            self.service.cancel_terminal(job)
            raise
        except Exception as exc:  # noqa: BLE001 - handler bug = transient 链式退避
            result = JobResult(
                status="failed",
                error_type="transient",
                detail={"error": f"{type(exc).__name__}: {str(exc)[:400]}"},
            )
        if result is None:  # 兼容返回 None 的旧 handler——语义 = 成功
            result = JobResult(status="succeeded")
        if not isinstance(result, JobResult):
            result = JobResult(
                status="failed",
                error_type="transient",
                detail={"error": f"handler 返回了 {type(result).__name__}，应为 JobResult"},
            )
        return self.service.complete_with_outcome(job, result)

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
