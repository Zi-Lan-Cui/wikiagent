"""compile/refine 共用 source 级失败处理测试。"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from wiki_agent.compiler.workflows.failures import SourceFailureConsumer, SourceFailureHandler
from wiki_agent.errors import IngestError, IngestStage, RetryableError
from wiki_agent.queue import QueueStore


def test_source_failure_handler_unifies_queue_record(tmp_path: Path):
    queue = QueueStore(tmp_path)
    handler = SourceFailureHandler(queue, mode="compile")
    error = IngestError(
        IngestStage.PLAN,
        "plan 输出校验失败",
        source="note.md",
        raw='{"bad": true}',
    )

    recorded = handler.handle(
        error,
        source="note.md",
        source_path=tmp_path / "note.md",
    )

    assert recorded is error
    items = queue.list()
    assert len(items) == 1
    assert items[0]["type"] == "ingest_failure"
    assert items[0]["mode"] == "compile"
    assert items[0]["file"] == "note.md"
    assert items[0]["stage"] == "plan"
    assert items[0]["error_code"] == "ingest_error"
    assert items[0]["error_class"] == "unknown"
    assert items[0]["status"] == "pending"


def test_failure_handler_classifies_retryable_error(tmp_path: Path):
    queue = QueueStore(tmp_path)
    handler = SourceFailureHandler(queue, mode="compile")
    handler.handle(
        IngestError(
            IngestStage.EXTRACT, "timeout", source="note.md", cause=RetryableError("timeout")
        ),
        source="note.md",
    )
    item = queue.list()[0]
    assert item["error_class"] == "transient"
    assert item["retry_policy"] == "auto_retry"


def test_ingest_error_can_carry_retry_policy_without_cause():
    error = IngestError(
        IngestStage.PLAN,
        "模型输出不符合协议",
        error_code="output_validation",
        error_class="transient",
        retry_policy="auto_retry",
    )
    assert error.error_code == "output_validation"
    assert error.error_class == "transient"
    assert error.retry_policy == "auto_retry"


def test_source_failure_consumer_removes_success_and_marks_manual(tmp_path: Path):
    queue = QueueStore(tmp_path)
    queue.append("ingest_failure", retry_policy="auto_retry", attempts=1)
    queue.append("ingest_failure", retry_policy="manual", attempts=1)
    items = queue.list()

    async def process(item):
        if item["retry_policy"] == "auto_retry":
            return
        raise AssertionError("manual item 不应进入 processor")

    consumer = SourceFailureConsumer(queue, process)
    first = asyncio.run(consumer.consume(items[0]))
    second = asyncio.run(consumer.consume(items[1]))
    assert first["status"] == "succeeded"
    assert second["status"] == "manual"
    assert len(queue.list()) == 1


def test_source_failure_consumer_defers_until_next_retry(tmp_path: Path):
    queue = QueueStore(tmp_path)
    queue.append(
        "ingest_failure",
        retry_policy="auto_retry",
        attempts=1,
        next_retry_at=(datetime.now() + timedelta(hours=1)).isoformat(),
    )
    item = queue.list()[0]
    called = False

    async def process(_item):
        nonlocal called
        called = True

    result = asyncio.run(SourceFailureConsumer(queue, process).consume(item))
    assert result["status"] == "deferred"
    assert called is False


def test_source_failure_consumer_sets_exponential_backoff(tmp_path: Path):
    queue = QueueStore(tmp_path)
    queue.append(
        "ingest_failure",
        retry_policy="auto_retry",
        attempts=1,
        next_retry_at="",
    )
    item = queue.list()[0]

    async def process(_item):
        raise RuntimeError("still unavailable")

    result = asyncio.run(
        SourceFailureConsumer(
            queue,
            process,
            base_delay_seconds=10,
            max_delay_seconds=100,
        ).consume(item)
    )
    stored = queue.get(item["id"])
    assert result["status"] == "failed"
    assert stored["status"] == "pending"
    assert stored["attempts"] == 2
    assert stored["next_retry_at"]


def test_expired_retry_item_becomes_manual(tmp_path: Path):
    queue = QueueStore(tmp_path)
    queue.append(
        "ingest_failure",
        retry_policy="auto_retry",
        attempts=1,
        retry_expires_at=(datetime.now() - timedelta(seconds=1)).isoformat(),
    )
    item = queue.list()[0]
    called = False

    async def process(_item):
        nonlocal called
        called = True

    result = asyncio.run(SourceFailureConsumer(queue, process).consume(item))
    assert result["status"] == "manual"
    assert queue.get(item["id"])["status"] == "manual"
    assert called is False
