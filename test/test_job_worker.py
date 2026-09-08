from __future__ import annotations

import asyncio

from wiki_agent.application.job_service import JobService
from wiki_agent.application.job_worker import JobWorker


def test_worker_claims_updates_stage_and_completes(tmp_path):
    service = JobService(tmp_path)
    job = service.submit(kind="compile", resource="note.md", mode="watch")
    worker = JobWorker(service)
    stages: list[str] = []

    async def handle(current, progress):
        progress("extract")
        stages.append(current.resource)

    worker.register("compile", handle)
    asyncio.run(worker.run_once())

    result = service.store.get(job.id)
    assert result.status == "succeeded"
    assert result.stage == "completed"
    assert stages == ["note.md"]


def test_worker_persists_unknown_kind_as_failure(tmp_path):
    service = JobService(tmp_path)
    job = service.submit(kind="unknown", resource="note.md", mode="test")
    asyncio.run(JobWorker(service).run_once())
    result = service.store.get(job.id)
    assert result.status == "failed"
    assert "未注册" in result.error
