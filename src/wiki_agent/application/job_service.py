"""Unified durable job submission and lifecycle API."""

from __future__ import annotations

from pathlib import Path

from wiki_agent.state import Job, JobStore


class JobService:
    """Application boundary for all executable work.

    Handlers will be attached in the next migration step; keeping submission
    and persistence here first prevents new entry points from adding queues.
    """

    def __init__(self, workspace: str | Path):
        self.store = JobStore(workspace)
        self.recovered_jobs = self.store.recover_stale()

    def submit(
        self,
        *,
        kind: str,
        resource: str,
        mode: str,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> Job:
        return self.store.enqueue(
            kind=kind,
            resource=resource,
            mode=mode,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def list(self, *, limit: int = 100) -> list[Job]:
        return self.store.list(limit=limit)

    def claim_next(self) -> Job | None:
        return self.store.claim_next()

    def mark_stage(self, job_id: str, stage: str) -> Job:
        return self.store.update(job_id, stage=stage)

    def succeed(self, job_id: str) -> Job:
        return self.store.update(job_id, status="succeeded", stage="completed")

    def fail(self, job_id: str, error: str) -> Job:
        return self.store.update(job_id, status="failed", error=error)

    def cancel(self, job_id: str) -> Job:
        return self.store.update(job_id, status="cancelled", stage="cancelled")
