"""维护线领域包：状态、判定与执行体同域安放。

- models/store：提议账本，状态机与处置留痕
- coordinator：线提交入口——共闸、单元校验、入队与账本挂账
- outcomes：PREVIEWED 结算入账的注册项
- pipeline：提议→复核→冲突消解的生成流程（脚本与命令共用）
- flow：/maintain 流程编排（预检文案、提议、整批入队）
- restructure：重组单元的声明模型与提议/复核/冲突消解/落盘算法
- link：出链维护的执行算法（字面替换清单经代码校验后应用）
- jobs：重组/补链/结构分析三类 job 的执行体

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
