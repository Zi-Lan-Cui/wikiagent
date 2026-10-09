"""维护线领域包：状态、判定与执行体同域安放（与 sync 同构的三件套）。

- models/store：提议账本（pending/dismissed/submitted/superseded，处置留痕）
- pipeline：提议→复核→冲突消解的生成流程（脚本与命令共用）
- flow：提交侧编排与 MaintenancePlanner 判定实现
- jobs：重组/补链/整理分析三类 job 的执行体

与分析执行（jobs 队列）分离：任务行只记"这次分析跑没跑成"，建议清单与
处置状态在账本。状态机手法与 issues 同构，语义独立——提议的处置是人的
裁决，不是执行的结果。
"""

from wiki_agent.maintenance.models import (
    Proposal,
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    Resolution,
)
from wiki_agent.maintenance.store import ProposalStore

__all__ = [
    "Proposal",
    "ProposalAlreadyResolvedError",
    "ProposalNotFoundError",
    "ProposalStore",
    "Resolution",
]
