"""Job 执行引擎的两个数据契约：持久化的任务行与 handler 的返回结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

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
    succeeded 时 detail 携带 digest/text 供 outcome 核账。
    """

    status: Literal["succeeded", "failed", "cancelled"]
    error_type: str = ""  # "" | "ingest_error"
    detail: Detail = field(default_factory=dict)
