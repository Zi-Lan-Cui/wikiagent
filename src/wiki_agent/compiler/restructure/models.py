"""结构重组的数据模型与超参数——纯数据，无 LLM、无副作用。"""

from __future__ import annotations

from dataclasses import dataclass, field

CONTENT_DIRS = ("concepts", "entities", "topics")

# 重组超参数（暂定值——精细调参时统一校准，勿散落魔法数字）
PROPOSE_MAX_TOKENS = 8_000  # 粗提输出预算（130 页 index 实测 2000 截断）
PROPOSE_MAX_COUNT = 12  # 粗提单次输出上限——召回不是枚举，只提最可疑的
RECHECK_MAX_TOKENS = 800  # 复判输出预算（确认/否决 + 方向 + 理由）
RE_ARBITRATE_MAX_TOKENS = 1_000  # 复裁输出预算（冲突清单重提议）
RECHECK_BODY_CHARS = 2_000  # 复判时给 LLM 的页面正文截断长度


@dataclass
class Proposal:
    """一条原子提议。"""

    op: str  # merge/delete/create/trim（复判后可能为 merge_into_*）
    pages: list[str]  # 涉及的已有页面 slug（create 的源页也放这里）
    target: str = ""  # merge 的吸收方向目标页（复判阶段定）
    reason: str = ""
    id: str = ""
    group_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    title: str = ""
    summary: str = ""
    goal: str = ""


@dataclass
class SurgeryResult:
    """一次执行的结构化结果——机器可消费（事件流之外的人工可读汇总）。"""

    actions: list[str]  # 实际执行的动作描述
    skipped: list[str]  # 因页面不存在而跳过的动作
    backed_up: list[str]  # 备份的文件路径（相对 backup 目录）


@dataclass
class Conflict:
    """一组无法确定性消解的提议冲突——交给 LLM 复裁或人工。"""

    kind: str  # "opposite_direction" / "delete_vs_merge"
    proposals: list[Proposal]
    detail: str = ""


@dataclass
class ArbitrationResult:
    """复裁结果——resolved 进执行，unresolved 记录待决策（不阻塞）。

    unresolved 的归宿是统一的决策队列（TODO）——与 ingest 失败重试
    等"需要用户抉择"的项同一条通道，用户在合适的时机统一处理。
    """

    resolved: list[Proposal]
    unresolved: list[Conflict]
