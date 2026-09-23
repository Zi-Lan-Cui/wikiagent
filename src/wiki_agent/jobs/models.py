"""Job 执行引擎的两个数据契约：持久化的任务行与 handler 的返回结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class Settlement(StrEnum):
    """成功 job 的结算类别——"做完了，做的是哪一种事"。

    三句话分工：status 只说做完没有；settlement 说完成的是哪种业务事实，
    由 handler 申报在 detail["settlement"]；issue 账本怎么变动由
    JobOutcomeHandler 查 ISSUE_RULES，handler 无权写账。
    未申报、或表里没有的类别一律不动账本（未知值另留警告）。
    """

    INGESTED = "ingested"  # 源文件真的编译进了 wiki
    ALREADY_INGESTED = "already_ingested"  # 快照内容已在完成账（崩溃重放短路）
    DELETE_APPLIED = "delete_applied"  # 删除清理完成
    REFINED = "refined"  # 页面精炼完成
    UNIT_MISSING = "unit_missing"  # refine 的页面已不在（排队期间被删）
    APPLIED = "applied"  # 重组提议执行并通过扫描闸门
    REJECTED_BY_GATE = "rejected_by_gate"  # 被闸门撤销
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
    # 挂账的 issue（成功销账/失败并账的缝合键）；"" = 与 issue 无关的纯执行 job
    issue_id: str = ""


@dataclass(frozen=True, slots=True)
class JobResult:
    """handler 与 Worker 之间的返回值契约——业务结局，bug 才抛异常。

    handler 只表达业务结局；未捕获异常由 Worker 就地记日志+事件承接，
    不产出结果对象。error_type 决定失败进哪本账（手动重试模型：只记账，
    不排程）：

    - "ingest_error": source 级业务失败 → 同事务上报/合并 issue 中心，
      等待人工重试（sync 或 retry 按钮）；detail 必须携带 draft 构造所需
      字段：error/stage/diagnostics/raw/source/source_path/source_kind。
    - "":             无联动语义的终态失败（如未注册 kind）。

    status="cancelled" 由取消路径直达终态，handler 无需返回。
    succeeded 时 detail 必须申报 settlement（结算类别）——它是 issue
    联动的唯一驱动源（outcomes.ISSUE_RULES 查表），未申报 = 账本不动；
    compile 成功另携带 digest/text 供完成账核账。
    """

    status: Literal["succeeded", "failed", "cancelled"]
    error_type: str = ""  # "" | "ingest_error"
    detail: Detail = field(default_factory=dict)
