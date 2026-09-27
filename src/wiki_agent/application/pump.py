"""自泵助手——批处理脚本把队列泵到空的共用循环。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wiki_agent.jobs.service import JobService
    from wiki_agent.jobs.worker import JobWorker


async def drain_queue(service: JobService, worker: JobWorker) -> int:
    """本进程充当 worker 把队列泵空，返回泵不动时剩余的在途数。

    run_once 返回 None 表示在途行都不是本进程注册的 kind（例如常驻 web
    进程正领着的活），停下等待对方，不是异常。
    """
    while service.count_in_flight() > 0:
        if await worker.run_once() is None:
            break
    return service.count_in_flight()
