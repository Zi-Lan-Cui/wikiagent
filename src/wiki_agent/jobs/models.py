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
    # 退避重排的到期时间；"" = 立即可领取
    next_run_at: str = ""

    @property
    def chain_attempt(self) -> int:
        """链式重试的累计代数。

        每个重试新行的 attempts 从 0 起——耗尽判断必须读 payload 里
        随链传递的 attempt_no（旧行没有该字段时退回自身 attempts）。
        """
        value = self.payload.get("attempt_no")
        inherited = value if isinstance(value, int) else 0
        return max(inherited, self.attempts)
