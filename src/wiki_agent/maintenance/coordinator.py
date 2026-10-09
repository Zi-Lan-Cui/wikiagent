"""维护线提交入口：重组批、发现型补链、结构分析，共用一条线闸。

闸在提交事务内取在途计数（queue.in_flight_wiki_write_counts）：已有
restructure 在途抛 RestructureInProgress，其余写在途抛 PipelineBusy，再查
基线落后抛 SyncBaselineLag。整理分析不是写任务，但结论基于 wiki 现状，
同样被写在途与基线挡住——基于执行中 wiki 的提议没有执行价值。

单元校验与批尾补链目标要读 wiki 盘面与 restructure 声明模型，同在本包内；提议账本与提交同事务挂账，重复提交被 CAS 拒绝。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from wiki_agent.compiler.content_pages import all_content_slugs
from wiki_agent.maintenance.restructure import Unit, UnitError, assert_units_valid, pages_linking_to
from wiki_agent.queue import Job, Kind, RestructureInProgress
from wiki_agent.queue.queue import JobQueue

if TYPE_CHECKING:
    from wiki_agent.maintenance.models import Proposal
    from wiki_agent.maintenance.store import ProposalStore
    from wiki_agent.sync.baseline import SyncBaseline


@dataclass(frozen=True, slots=True)
class RestructurePlan:
    """重组批的入队素材：校验后的单元声明（已序列化）与批尾补链目标。

    units 顺序即入队顺序；link_slugs 为批尾补链页。
    """

    units: list[dict]
    link_slugs: list[str]


class MaintenanceCoordinator:
    """维护线提交入口：闸、校验、入队、账本挂账。"""

    def __init__(
        self,
        *,
        queue: JobQueue,
        baseline: SyncBaseline | None = None,
        wiki_dir: str | Path | None = None,
        proposals: ProposalStore | None = None,
    ):
        self.queue = queue
        # 基线闸复用 sync 线判定；离线装配无基线时跳过该项检查
        self.baseline = baseline
        self.wiki_dir = Path(wiki_dir) if wiki_dir is not None else None
        self.proposals = proposals

    @staticmethod
    def plan_restructure(units: list[dict], wiki_dir: Path) -> RestructurePlan:
        """基于 wiki 当前内容重跑消解规则：解析→校验→算批尾补链目标。

        损坏声明与违例单元都抛 UnitError（提交入口拒绝绕过消解的清单）；
        补链目标 = 全部产出页 ∪ 消失页的入链页（wiki 内容不变时该集合确定）。
        """
        try:
            parsed = [Unit.from_dict(raw) for raw in units]
        except (TypeError, ValueError) as exc:
            raise UnitError(f"单元声明损坏: {exc}") from exc
        assert_units_valid(parsed, set(all_content_slugs(wiki_dir)))
        if not parsed:
            return RestructurePlan(units=[], link_slugs=[])
        vanished = sorted({s for unit in parsed for s in unit.vanished})
        link_slugs = sorted(
            {p for unit in parsed for p in unit.out_slugs}
            | set(pages_linking_to(wiki_dir, vanished))
        )
        return RestructurePlan(units=[unit.to_dict() for unit in parsed], link_slugs=link_slugs)

    @staticmethod
    def resolve_link_targets(slugs: list[str] | None, wiki_dir: Path) -> list[str]:
        """全库扫描的补链目标：None=全部内容页；给定 slug 不在内容页列表则抛 ValueError。"""
        roster = all_content_slugs(wiki_dir)
        if slugs is None:
            return list(roster)
        unknown = [s for s in dict.fromkeys(slugs) if s not in set(roster)]
        if unknown:
            raise ValueError(f"不是可维护的 wiki 页: {unknown}")
        return list(dict.fromkeys(slugs))

    def _raise_if_blocked(self, _conn: sqlite3.Connection | None = None) -> None:
        """维护线共用闸：restructure 在途抛专门异常，其余在途抛 PipelineBusy，再判基线。"""
        busy = self.queue.in_flight_wiki_write_counts(_conn=_conn)
        if Kind.RESTRUCTURE in busy:
            raise RestructureInProgress()
        if busy:
            raise JobQueue.pipeline_busy(busy)
        if self.baseline is not None:
            self.baseline.raise_if_lagging()

    def submit_restructure(self, units: list[dict], proposal_id: str = "") -> list[Job]:
        """已确认的单元清单整批入队：一单元一 job，批尾自动跟受影响页补链。

        单元声明先经 plan_restructure 校验与影响范围计算；一个事务内入队全部
        单元与批尾 link 任务。payload 只带声明，章节归属由执行时路由计算。

        带 proposal_id 时提交清单必须是提议 effective 的子集（按 in_pages 与
        out 判等）——部分采纳合法，提议之外的单元拒绝；同事务把该提议置
        submitted，记采纳单元与产生的任务行，同一提议重复提交被 CAS 拒绝。
        单元清单为空时不入队也不处置账本。
        """
        if self.wiki_dir is None:
            raise RuntimeError("submit_restructure 需要 wiki_dir")
        plan = self.plan_restructure(units, self.wiki_dir)
        if not plan.units:
            return []
        accepted: list[dict] | None = None
        if proposal_id:
            if self.proposals is None:
                raise RuntimeError("submit_restructure 带 proposal_id 需要账本注入")
            accepted = self._match_proposal(self.proposals.require(proposal_id), plan.units)
        batch = f"restructure_{uuid4().hex}"
        jobs: list[Job] = []
        with self.queue.store.transaction(immediate=True) as conn:
            self._raise_if_blocked(conn)
            for index, unit in enumerate(plan.units):
                jobs.append(
                    self.queue.submit(
                        kind=Kind.RESTRUCTURE,
                        resource=f"restructure:{batch}:{index}",
                        mode="manual",
                        payload={"unit": unit, "batch": batch, "index": index},
                        idempotency_key=f"restructure:{batch}:{index}",
                        _conn=conn,
                    )
                )
            for slug in plan.link_slugs:
                jobs.append(
                    self.queue.submit(
                        kind=Kind.LINK,
                        resource=f"link:{slug}",
                        mode="tail",
                        payload={"slug": slug, "batch": batch},
                        idempotency_key=f"link:{batch}:{slug}",
                        _conn=conn,
                    )
                )
            if proposal_id:
                assert accepted is not None
                self.proposals.mark_submitted(
                    proposal_id, [job.id for job in jobs], accepted=accepted, _conn=conn
                )
        return jobs

    @staticmethod
    def _unit_key(unit: dict) -> tuple[frozenset[str], tuple[str, ...]]:
        """单元身份：输入页集合 + 输出页 slug 序列，提议与提交两侧同式判等。"""
        return (
            frozenset(str(s) for s in unit.get("in_pages") or []),
            tuple(str(p.get("slug") or "") for p in unit.get("out") or []),
        )

    @classmethod
    def _match_proposal(cls, proposal: Proposal, units: list[dict]) -> list[dict]:
        """提交清单逐条要求在提议 effective 内；返回采纳单元的标识。"""
        raw = proposal.payload.get("effective")
        pool = {
            cls._unit_key(entry): entry
            for entry in raw
            if isinstance(entry, dict)
        } if isinstance(raw, list) else {}
        accepted: list[dict] = []
        for unit in units:
            entry = pool.get(cls._unit_key(unit))
            if entry is None:
                raise ValueError(f"提交的单元不在提议之内: {sorted(unit.get('in_pages') or [])}")
            accepted.append(
                {
                    "in_pages": sorted(str(s) for s in entry.get("in_pages") or []),
                    "out": [str(p.get("slug") or "") for p in entry.get("out") or []],
                }
            )
        return accepted

    def submit_link_batch(self, slugs: list[str] | None = None) -> list[Job]:
        """发现型补链：指定页（默认全库内容页）逐页入队，一页一 job 一提交。

        slug 按内容页白名单校验（不存在即 ValueError）；互斥与基线检查同
        重组提交口（维护线共闸）。
        """
        if self.wiki_dir is None:
            raise RuntimeError("submit_link_batch 需要 wiki_dir")
        targets = self.resolve_link_targets(slugs, self.wiki_dir)
        if not targets:
            return []
        batch = f"link_{uuid4().hex}"
        jobs: list[Job] = []
        with self.queue.store.transaction(immediate=True) as conn:
            self._raise_if_blocked(conn)
            for slug in targets:
                jobs.append(
                    self.queue.submit(
                        kind=Kind.LINK,
                        resource=f"link:{slug}",
                        mode="manual",
                        payload={"slug": slug, "batch": batch},
                        idempotency_key=f"link:{batch}:{slug}",
                        _conn=conn,
                    )
                )
        return jobs

    def submit_restructure_preview(self) -> Job:
        """整理结构分析入队（提议→复核→冲突消解）：只产建议清单，不写 wiki。

        resource 与幂等键固定，重复发起收敛为同一在途分析。上一轮提议在新
        一轮结果入账的 PREVIEWED 联动时作废，分析失败不动账本。
        """
        with self.queue.store.transaction(immediate=True) as conn:
            busy = self.queue.in_flight_wiki_write_counts(_conn=conn)
            if busy:
                raise JobQueue.pipeline_busy(busy)
            if self.baseline is not None:
                self.baseline.raise_if_lagging()
            return self.queue.submit(
                kind=Kind.RESTRUCTURE_PREVIEW,
                resource="restructure:preview",
                mode="manual",
                payload={},
                idempotency_key="restructure:preview",
                _conn=conn,
            )
