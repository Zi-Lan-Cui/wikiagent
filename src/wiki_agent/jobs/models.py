"""Job 执行引擎的两个数据契约：持久化的任务行与 handler 的返回结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class Kind(StrEnum):
    """job 类型：worker 按它分派 handler，提交互斥与撤销检查按它统计在途行。"""

    COMPILE = "compile"  # 编译一个源文件（sync 批与 issue retry 共用）
    DELETE = "delete"  # 清理一个已删除来源
    RESTRUCTURE = "restructure"  # 重组单元：结构调整 + 触及页的逐页成文
    LINK = "link"  # 单页出链维护（只改链接，不重写内容）
    ISSUE_ACTION = "issue_action"  # issue 动作（rescan 等）
    MAINTENANCE_PREVIEW = "maintenance_preview"  # 整理结构分析：提议→复核→消解，不写 wiki


# 提交互斥、sync_status 与 /wiki revert 前检查共用
WIKI_WRITE_KINDS: tuple[Kind, ...] = (
    Kind.COMPILE,
    Kind.DELETE,
    Kind.RESTRUCTURE,
    Kind.LINK,
)


class Settlement(StrEnum):
    """成功 job 的结算类别：说明完成的是哪种业务事实。

    status 表示任务是否完成；settlement 由 handler 申报在 detail["settlement"]，
    表示完成的业务事实类型；issue 状态变更由 JobOutcomeHandler 依 ISSUE_RULES
    决定，handler 不直接改 issue。未申报或表中无此类别时不改 issue，未知值记警告。
    """

    INGESTED = "ingested"  # 源文件编译进 wiki
    ALREADY_INGESTED = "already_ingested"  # 快照内容已在完成记录，重放时短路返回
    DELETE_APPLIED = "delete_applied"  # 删除清理完成
    APPLIED = "applied"  # 重组单元执行并通过质量检查
    UNIT_MISSING = "unit_missing"  # 单页改写/补链的目标页在排队期间已消失
    LINKED = "linked"  # 该页出链核对完成（applied 计数在 detail，可为 0）
    RESCAN_STILL_PRESENT = "rescan_still_present"  # 复扫确认问题仍在
    RESCAN_CLEARED = "rescan_cleared"  # 复扫确认问题已消失

Detail = dict[str, object]  # 执行结果明细，值类型比 issues.JsonObject 宽，经 json.dumps 落库


@dataclass(frozen=True, slots=True)
class Job:
    """持久化的后台任务行。"""

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
    # 关联的 issue；成功与失败都据此更新 issue；"" = 与 issue 无关
    issue_id: str = ""
    # handler 的执行结果明细（JobResult.detail）随终态落库；
    # 分析类 job 的产出（如整理建议清单）供 UI 事后回看
    result: Detail = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobResult:
    """handler 与 Worker 之间的返回契约：表达业务结局；程序错误抛异常。

    handler 只返回业务结局；未捕获异常由 Worker 记日志并发事件，不产结果对象。
    error_type 决定失败记到哪类 issue（手动重试模型：只记 issue，不自动排程）：

    - "ingest_error": source 级业务失败 → 同事务上报/合并 issue，等待人工重试；
      detail 须携带构造 draft 的字段：error/stage/diagnostics/raw/source/
      source_path/source_kind。
    - "": 无 issue 联动的终态失败（如未注册 kind）。

    status="cancelled" 由取消路径直接置终态，handler 无需返回。
    succeeded 时 detail 须申报 settlement，issue 联动据此在 outcomes.ISSUE_RULES
    查表；compile 成功另带 digest/text 供写完成记录。
    """

    status: Literal["succeeded", "failed", "cancelled"]
    error_type: str = ""  # "" | "ingest_error"
    detail: Detail = field(default_factory=dict)
