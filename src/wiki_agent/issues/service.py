from __future__ import annotations

from wiki_agent.issues.models import (
    IssueCard,
    IssueDraft,
    IssueKind,
    IssueStatus,
    JsonObject,
)
from wiki_agent.issues.projectors import available_actions, to_card
from wiki_agent.issues.store import IssueStore


class IssueService:
    """Keep adapters and producers independent from persistence details."""

    def __init__(self, store: IssueStore):
        self.store = store

    def report(self, draft: IssueDraft) -> IssueCard:
        return to_card(self.store.report(draft))

    def get(self, issue_id: str) -> IssueCard:
        return to_card(self.store.require(issue_id))

    def list(
        self,
        *,
        statuses: set[IssueStatus] | None = None,
        kinds: set[IssueKind] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[IssueCard]:
        return [
            to_card(record)
            for record in self.store.list(
                statuses=statuses,
                kinds=kinds,
                limit=limit,
                offset=offset,
            )
        ]

    def count(
        self,
        *,
        statuses: set[IssueStatus] | None = None,
        kinds: set[IssueKind] | None = None,
    ) -> int:
        return self.store.count(statuses=statuses, kinds=kinds)

    def apply_simple_action(
        self, issue_id: str, action: str, payload: JsonObject | None = None
    ) -> IssueCard:
        """Apply state-only decisions; workflow actions are delegated later."""
        record = self.store.require(issue_id)
        allowed = {item.id for item in available_actions(record)}
        if action not in allowed:
            raise ValueError(f"当前问题不允许操作: {action}")
        if action == "dismiss":
            return to_card(
                self.store.transition(
                    issue_id,
                    IssueStatus.DISMISSED,
                    resolution={"action": action, **(payload or {})},
                    event="dismissed",
                )
            )
        if action == "reopen":
            return to_card(
                self.store.transition(
                    issue_id,
                    IssueStatus.OPEN,
                    resolution={},
                    event="reopened",
                )
            )
        raise ValueError(f"操作需要工作流 handler: {action}")
