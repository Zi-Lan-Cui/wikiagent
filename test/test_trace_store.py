"""Durable trace manifest, transcript and span hierarchy tests."""

from __future__ import annotations

import asyncio
import json

from wiki_agent.log import (
    begin_trace,
    finish_trace,
    record_trace,
    setup_event_log,
    setup_trace,
    span,
)


def _json_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_trace_store_redacts_secrets_and_summarizes_images(tmp_path) -> None:
    begin_trace("trace-test")
    setup_trace(
        tmp_path,
        trace_id="trace-test",
        kind="test",
        metadata={"api_key": "secret-value", "model": "fake"},
    )
    record_trace(
        "llm.request",
        messages=[{"role": "user", "content": "hello", "images": ["encoded-image"]}],
    )
    finish_trace("succeeded", outputs={"answer": "ok"}, metrics={"total_tokens": 3})

    manifest = json.loads((tmp_path / "trace.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["api_key"] == "[REDACTED]"
    assert manifest["status"] == "succeeded"
    transcript = _json_lines(tmp_path / "transcript.jsonl")
    image = transcript[0]["messages"][0]["images"][0]
    assert image["encoded_chars"] == len("encoded-image")
    assert "encoded-image" not in (tmp_path / "transcript.jsonl").read_text(encoding="utf-8")


def test_nested_spans_record_parent_relationship(tmp_path) -> None:
    begin_trace("span-test")
    setup_event_log(tmp_path / "events.jsonl")

    async def run() -> None:
        async with span("outer"):
            async with span("inner"):
                pass

    asyncio.run(run())
    setup_event_log(None)
    events = _json_lines(tmp_path / "events.jsonl")
    starts = {event["span"]: event for event in events if event["event"] == "span_started"}
    assert starts["outer"]["parent_span_id"] is None
    assert starts["inner"]["parent_span_id"] == starts["outer"]["span_id"]
