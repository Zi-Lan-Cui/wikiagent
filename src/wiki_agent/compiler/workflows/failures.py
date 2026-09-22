"""source 级失败诊断——把 IngestError 转成可持久化/展示的结构化字段。

记账只有一条路：handler 产出 JobResult(ingest_error) → JobOutcomeHandler
终态联动进问题账本（jobs/outcomes）。本模块不做上报，只负责诊断提取。
"""

from __future__ import annotations

import json
from typing import Any

from wiki_agent.errors import IngestError


def _find_ingest_error(error: Exception) -> IngestError | None:
    """在异常链中定位保留了阶段和原始输出的 ingestion 异常。"""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, IngestError):
            return current
        nested = getattr(current, "cause", None)
        current = nested if isinstance(nested, BaseException) else current.__cause__
    return None


def failure_diagnostics(error: Exception) -> tuple[dict[str, Any], str]:
    """生成可持久化/展示的诊断，并将原始模型输出单独返回给事件日志。"""
    ingest_error = _find_ingest_error(error)
    diagnostics: dict[str, Any] = {"detail": str(error)[:1000]}
    raw = ""
    if ingest_error is None:
        return diagnostics, raw

    diagnostics.update(
        {
            "stage": ingest_error.stage.value,
            "error_code": ingest_error.error_code,
            "error_class": ingest_error.error_class,
        }
    )
    raw = ingest_error.raw
    if not raw:
        return diagnostics, raw

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return diagnostics, raw
    if not isinstance(payload, list):
        return diagnostics, raw

    failures: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")[:500]
        reason = str(item.get("error") or "")[:1000]
        if path or reason:
            # item["raw"] 可能是大段 LLM 输出，只允许进入事件日志。
            failures.append({"path": path, "reason": reason})
    if failures:
        diagnostics["failures"] = failures
    return diagnostics, raw
