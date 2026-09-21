"""统一的 Job 执行结果——handler 与 Worker 之间的返回值契约。

handler（执行体）返回 JobResult 表达业务结局；只有 handler 自身的 bug
才抛未捕获异常（由 Worker 归为 transient 走链式退避）。业务失败
（IngestError）必须由 handler 捕获转成 failed/ingest_error 结果——
终态与 issue/watch 的联动只发生在 Worker 的单事务提交里。

error_type 决定失败走哪条唯一通道（每类失败只有一条重试通道）：
- "ingest_error": source 级业务失败 → 同事务上报 issue 中心（重试归它）；
  detail 必须携带 draft 构造所需字段：error/stage/diagnostics/raw/
  source/source_path/source_kind/retry_policy。
- "transient":    未预期异常 →  attempts 内链式新 job 退避重排，
  超限升级为 run_failure issue。
- "cancelled":    进程取消/被取代 → 不判失败；issue 全程未动状态，无账可还。
- "":             终态失败但无联动语义（如未注册 kind 直接失败）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Detail = dict[str, object]  # 执行结果明细——值比 issues.JsonObject 宽（json.dumps 落库）


@dataclass(frozen=True, slots=True)
class JobResult:
    """一次 Job 执行的结构化结局（succeeded 时 detail 携带 digest/text 供核账）。"""

    status: Literal["succeeded", "failed", "cancelled"]
    error_type: str = ""  # "" | "ingest_error" | "transient" | "cancelled"
    detail: Detail = field(default_factory=dict)
