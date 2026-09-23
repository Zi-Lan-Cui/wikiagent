"""Single durable worker——jobs 引擎的认领循环。

Worker 是唯一的终态写入者：claim（按注册 kinds）→
handler 返回 JobResult → complete_with_outcome 单事务落终态。
handler 只表达业务结局；bug 抛异常由这里归日志 + 事件——不进问题账本，
代码错误不是用户的待办。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from wiki_agent.jobs import Job, JobResult
from wiki_agent.jobs.service import JobService
from wiki_agent.log import emit_event, get_logger

JobHandler = Callable[[Job, Callable[[str], None]], Awaitable[JobResult]]

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
        except Exception as exc:  # noqa: BLE001 - handler bug 不是用户的待办：日志+事件承接
            logger.exception("job %s handler 崩溃: %s", job.id, f"{type(exc).__name__}: {exc}")
            emit_event(
                "job_handler_crash",
                job_id=job.id,
                kind=job.kind,
                resource=job.resource,
                error=f"{type(exc).__name__}: {str(exc)[:400]}",
            )
            result = JobResult(
                status="failed",
                detail={"error": f"{type(exc).__name__}: {str(exc)[:400]}"},
            )
        if not isinstance(result, JobResult):
            # 契约违约同样是代码 bug：记日志/事件，账上不留给用户
            logger.error("job %s handler 返回了 %s，应为 JobResult", job.id, type(result).__name__)
            emit_event("job_handler_contract_violation", job_id=job.id, kind=job.kind)
            result = JobResult(
                status="failed",
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
