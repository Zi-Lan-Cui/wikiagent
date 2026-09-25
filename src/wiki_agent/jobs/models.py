"""Job 执行引擎的两个数据契约：持久化的任务行与 handler 的返回结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class Kind(StrEnum):
    """job 类型：worker 按它分派 handler，互斥闸按它计数在途行。"""

    COMPILE = "compile"  # 编译一个源文件（sync 批与 issue retry 共用）
    DELETE = "delete"  # 清理一个已删除来源
    REFINE = "refine"  # 单页精炼
    RESTRUCTURE = "restructure"  # 重组执行单元
    ISSUE_ACTION = "issue_action"  # issue 动作（rescan 等）


# 写 wiki 的任务家族——提交互斥闸与 /wiki revert 的门共用这一集合，
# 写者身份在系统里只有这一个事实来源
WIKI_WRITE_KINDS: tuple[Kind, ...] = (
    Kind.COMPILE,
    Kind.DELETE,
    Kind.REFINE,
    Kind.RESTRUCTURE,
)


class Settlement(StrEnum):
    """成功 job 的结算类别：说明完成的是哪一种业务事实。

    分工：status 表示任务是否完成；settlement 由 handler 申报在
    detail["settlement"]，表示完成的事实类型；账本动作由
    JobOutcomeHandler 查 ISSUE_RULES 决定，handler 不能直接写账。
    未申报或表中不存在的类别不动账本，未知值记录警告。
    """

    INGESTED = "ingested"  # 源文件编译进 wiki
    ALREADY_INGESTED = "already_ingested"  # 快照内容已在完成账，崩溃重放时短路返回
    DELETE_APPLIED = "delete_applied"  # 删除清理完成
    REFINED = "refined"  # 页面精炼完成
    UNIT_MISSING = "unit_missing"  # refine 的页面在排队期间已被删除
    APPLIED = "applied"  # 重组提议执行并通过扫描闸门
    RESCAN_STILL_PRESENT = "rescan_still_present"  # 复扫确认问题仍在
    RESCAN_CLEARED = "rescan_cleared"  # 复扫确认问题已消失

Detail = dict[str, object]  # 执行结果明细——值比 issues.JsonObject 宽（json.dumps 落库）


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
    # 挂账的 issue；成功与失败都按它联动问题账本；"" = 与 issue 无关的纯执行 job
    issue_id: str = ""


@dataclass(frozen=True, slots=True)
class JobResult:
    """handler 与 Worker 之间的返回值契约：表达业务结局；程序错误抛异常。

    handler 只返回业务结局；未捕获异常由 Worker 记日志并发事件，
    不产出结果对象。error_type 决定失败记录到哪个账本（手动重试模型：
    只记账，不排程）：

    - "ingest_error": source 级业务失败 → 同事务上报/合并 issue 中心，
      等待人工重试（sync 或 retry 按钮）；detail 必须携带 draft 构造所需
      字段：error/stage/diagnostics/raw/source/source_path/source_kind。
    - "":             无联动语义的终态失败（如未注册 kind）。

    status="cancelled" 由取消路径直达终态，handler 无需返回。
    succeeded 时 detail 必须申报 settlement，issue 联动按它在
    outcomes.ISSUE_RULES 查表；未申报则账本不动；
    compile 成功另携带 digest/text 供完成账核账。
    """

    status: Literal["succeeded", "failed", "cancelled"]
    error_type: str = ""  # "" | "ingest_error"
    detail: Detail = field(default_factory=dict)
