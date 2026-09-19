from __future__ import annotations

import contextvars
import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_trace_store_var: contextvars.ContextVar[TraceStore | None] = contextvars.ContextVar(
    "wiki_trace_store", default=None
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"api_key", "authorization", "password", "secret"}:
                result[str(key)] = "[REDACTED]" if item else ""
            elif normalized == "images" and isinstance(item, list):
                result[str(key)] = [
                    {
                        "sha256": hashlib.sha256(str(image).encode()).hexdigest(),
                        "encoded_chars": len(str(image)),
                    }
                    for image in item
                ]
            else:
                result[str(key)] = _json_value(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TraceStore:
    def __init__(self, root: Path, *, trace_id: str, kind: str, metadata: dict[str, Any]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.trace_id = trace_id
        self.manifest_path = self.root / "trace.json"
        self.transcript_path = self.root / "transcript.jsonl"
        self._lock = threading.Lock()
        self._manifest: dict[str, Any] = {
            "version": 1,
            "trace_id": trace_id,
            "kind": kind,
            "status": "running",
            "started_at": _now(),
            "ended_at": None,
            "metadata": _json_value(metadata),
            "outputs": {},
            "metrics": {},
            "error": None,
        }
        self._write_manifest()

    def _write_manifest(self) -> None:
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.manifest_path)

    def record(self, event: str, **fields: Any) -> None:
        from wiki_agent.log.tracer import current_span_id

        record = {
            "ts": _now(),
            "trace_id": self.trace_id,
            "span_id": current_span_id(),
            "event": event,
            **_json_value(fields),
        }
        with self._lock:
            with self.transcript_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def finish(
        self,
        status: str,
        *,
        outputs: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._manifest.update(
                {
                    "status": status,
                    "ended_at": _now(),
                    "outputs": _json_value(outputs or {}),
                    "metrics": _json_value(metrics or {}),
                    "error": error,
                }
            )
            self._write_manifest()


def setup_trace(
    root: str | Path,
    *,
    trace_id: str,
    kind: str,
    metadata: dict[str, Any] | None = None,
) -> TraceStore:
    store = TraceStore(Path(root), trace_id=trace_id, kind=kind, metadata=metadata or {})
    _trace_store_var.set(store)
    return store


def current_trace_store() -> TraceStore | None:
    return _trace_store_var.get()


def record_trace(event: str, **fields: Any) -> None:
    store = current_trace_store()
    if store is not None:
        store.record(event, **fields)


def finish_trace(
    status: str,
    *,
    outputs: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    store = current_trace_store()
    if store is not None:
        store.finish(status, outputs=outputs, metrics=metrics, error=error)
