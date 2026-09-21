"""Data models for durable background jobs."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Job:
    """A persisted unit of background work."""

    id: str
    kind: str
    resource: str
    mode: str
    status: str
    stage: str
    attempts: int
    error: str
    payload: dict[str, object]
    created_at: str
    updated_at: str
    # 关联的 issue（执行链回写账本用）；"" = 与 issue 无关的纯执行 job
    issue_id: str = ""
