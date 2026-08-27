"""compile/refine 共用的 source 级失败处理。

Pipeline 内部保留 ``IngestError.stage/raw`` 的细节；边界统一把失败
转换成一条 source 级待处理事项。watch 有自己的实时失败通道，不使用本模块。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from wiki_agent.config import RetryConfig
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.log import emit_event, get_logger
from wiki_agent.queue import QueueStore


class SourceFailureHandler:
    """将 compile/refine 失败统一记录到事件流和 source 队列。"""

    def __init__(self, queue: QueueStore, *, mode: str):
        if mode not in {"compile", "refine"}:
            raise ValueError(f"source failure 不支持的 mode: {mode!r}")
        self._queue = queue
        self._mode = mode
        self._logger = get_logger(f"{mode.upper()}_FAILURE")
        self._retry_window = timedelta(hours=24)

    def handle(
        self,
        error: IngestError | Exception,
        *,
        source: str,
        source_path: str | Path = "",
        source_kind: str = "input_file",
    ) -> IngestError:
        """记录一次 source 失败并返回标准化的 ``IngestError``。

        队列只保存重试索引和短错误消息；完整 raw 进入事件流，避免队列
        被大段 LLM 输出污染。重试时应从该 source 的 Pipeline 起点重新执行。
        """
        err = (
            error
            if isinstance(error, IngestError)
            else IngestError(
                IngestStage.LOAD,
                f"未分类: {error}",
                source=source,
                cause=error,
            )
        )
        stage = err.stage.value
        queue_id = self._queue.append(
            "ingest_failure",
            source=self._mode,
            mode=self._mode,
            source_kind=source_kind,
            file=source,
            source_path=str(source_path),
            stage=stage,
            error=str(err)[:500],
            error_code=err.error_code,
            error_class=err.error_class,
            retry_policy=err.retry_policy,
            attempts=1,
            status="pending",
            next_retry_at="",
            retry_expires_at=(datetime.now() + self._retry_window).isoformat(),
        )
        emit_event(
            "ingest_failure",
            queue_id=queue_id,
            mode=self._mode,
            source_kind=source_kind,
            file=source,
            source_path=str(source_path),
            stage=stage,
            error=str(err),
            cause=type(err.cause).__name__ if err.cause else None,
            raw=err.raw,
        )
        self._logger.error(
            "source 失败 [%s] %s: %s",
            stage,
            source,
            str(err)[:200],
        )
        return err


class SourceFailureConsumer:
    """source 失败队列的分发内核。

    ``processor`` 从 source 起点重新执行；消费者只负责策略、次数和
    队列状态，避免把 compile/refine 两套流水线复制进队列层。
    """

    def __init__(
        self,
        queue: QueueStore,
        processor,
        *,
        retry_config: RetryConfig | None = None,
        max_attempts: int | None = None,
        remove_on_success: bool = True,
        base_delay_seconds: float | None = None,
        max_delay_seconds: float | None = None,
    ):
        retry = retry_config or RetryConfig()
        self._queue = queue
        self._processor = processor
        self._max_attempts = max_attempts if max_attempts is not None else retry.source_max_attempts
        self._remove_on_success = remove_on_success
        self._base_delay_seconds = (
            base_delay_seconds
            if base_delay_seconds is not None
            else retry.source_base_delay_seconds
        )
        self._max_delay_seconds = (
            max_delay_seconds if max_delay_seconds is not None else retry.source_max_delay_seconds
        )

    def classify(self, item: dict) -> str:
        policy = item.get("retry_policy", "manual")
        attempts = int(item.get("attempts", 0) or 0)
        if self._is_expired(item):
            return "manual"
        if policy == "auto_retry" and attempts < self._max_attempts:
            return "retry"
        if policy == "retry_once" and attempts < min(self._max_attempts, 2):
            return "retry_once"
        return "manual"

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _is_expired(self, item: dict) -> bool:
        deadline = self._parse_time(str(item.get("retry_expires_at", "")))
        return deadline is not None and datetime.now() >= deadline

    def _is_deferred(self, item: dict) -> bool:
        next_retry = self._parse_time(str(item.get("next_retry_at", "")))
        return next_retry is not None and datetime.now() < next_retry

    async def consume(self, item: dict) -> dict:
        """消费一条 source 项，成功移除，失败保留并更新状态。"""
        decision = self.classify(item)
        if decision == "manual":
            self._queue.update(item["id"], status="manual")
            return {"id": item["id"], "status": "manual"}
        if self._is_deferred(item):
            return {
                "id": item["id"],
                "status": "deferred",
                "next_retry_at": item.get("next_retry_at", ""),
            }

        attempts = int(item.get("attempts", 0) or 0) + 1
        self._queue.update(item["id"], status="processing", attempts=attempts)
        try:
            await self._processor(item)
        except Exception as exc:
            expired = self._is_expired(item)
            policy_limit = (
                min(self._max_attempts, 2)
                if item.get("retry_policy") == "retry_once"
                else self._max_attempts
            )
            status = "pending" if attempts < policy_limit and not expired else "manual"
            delay = min(
                self._max_delay_seconds,
                self._base_delay_seconds * (2 ** max(0, attempts - 1)),
            )
            next_retry_at = (
                (datetime.now() + timedelta(seconds=delay)).isoformat()
                if status == "pending"
                else ""
            )
            self._queue.update(
                item["id"],
                status=status,
                attempts=attempts,
                last_error=str(exc)[:500],
                next_retry_at=next_retry_at,
            )
            emit_event(
                "source_retry_failed",
                queue_id=item["id"],
                attempts=attempts,
                error=str(exc),
                next_retry_at=next_retry_at,
            )
            return {
                "id": item["id"],
                "status": "failed",
                "attempts": attempts,
                "next_retry_at": next_retry_at,
            }
        if self._remove_on_success:
            self._queue.remove(item["id"])
        else:
            self._queue.update(item["id"], status="succeeded")
        emit_event("source_retry_succeeded", queue_id=item["id"], attempts=attempts)
        return {"id": item["id"], "status": "succeeded", "attempts": attempts}
