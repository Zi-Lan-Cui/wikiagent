"""批处理脚本把 jobs 队列执行到空的共用循环。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wiki_agent.jobs.service import JobService
    from wiki_agent.jobs.worker import JobWorker


async def drain_queue(service: JobService, worker: JobWorker) -> int:
    """本进程充当 worker 执行队列直到无法继续，返回剩余的在途数。

    run_once 返回 None 表示在途行都不是本进程注册的 kind（例如常驻 web
    进程正在处理的行），此时停止等待对方完成，不是异常。

    前提是调用方已持执行锁。进入时先回收上次进程崩溃或中断遗留的
    running 行——它们属于本队列，不回收则队列无法清空。
    """
    service.recover_stale()
    while service.count_in_flight() > 0:
        if await worker.run_once() is None:
            break
    return service.count_in_flight()
