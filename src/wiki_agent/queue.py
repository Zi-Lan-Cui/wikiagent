"""统一异常队列——处理失败事项的人机接口。

生命周期区别（单真相源原则的继承）:
- events.jsonl: "发生了什么"的永久事实（机器读，从不删除）
- queue.jsonl:   "待用户处理"的临时事项（人读，处理完即移除）

二者不重复——事件按"发生"记录，队列按"待决"记录；队列项
被移除时事件仍在，审计不丢。失败重试成功 = 队列项完成，
不需要第二份清单（failed.json 的教训）。

队列项类型:
- ingest_failure:  compile/refine/watch 的文件 ingest 失败
                  （stage/source/file/error/error_class/retry_policy）
- surgery_conflict: 手术冲突无法仲裁（kind/detail/proposals）
- correction:      QA 纠错待裁决（corrections.md 的队列视图，
                  由 /queue 聚合展示，裁决走 /resolve）

文件格式: JSONL 追加（append 原子友好，update/remove 通过临时文件替换）。
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from wiki_agent.log import get_logger

logger = get_logger("QUEUE")


class QueueStore:
    """workspace/queue.jsonl——失败事项的追加/列出/移除。"""

    def __init__(self, workspace: str | Path):
        self._file = Path(workspace) / "queue.jsonl"
        self._lock_file = self._file.with_name(self._file.name + ".lock")

    @contextmanager
    def _lock(self, *, shared: bool = False):
        """锁住整个 queue 文件生命周期。

        lock 文件独立于 queue.jsonl，避免 replace queue.jsonl 后锁对象
        被替换，导致两个进程各自锁住不同 inode。
        """
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_file, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> list[dict]:
        try:
            with open(self._file, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def _write_unlocked(self, items: list[dict]) -> None:
        tmp = self._file.with_name(self._file.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._file)

    def append(self, type_: str, **fields) -> str:
        """追加一条待处理项。

        Args:
            type_: 事项类型（ingest_failure / surgery_conflict / correction，
                含义见模块 docstring）。
            **fields: 随记录写入的附加字段（如 stage/source/error）。

        Returns:
            生成的记录 id。
        """
        ts = datetime.now().isoformat()
        item_id = f"{type_}_{ts.replace(':', '').replace('-', '').replace('.', '')}"
        record = {"id": item_id, "type": type_, "ts": ts, **fields}
        with self._lock():
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("队列新增 [%s] %s", type_, item_id)
        return item_id

    def list(self) -> list[dict]:
        """返回全部待处理项（按时间顺序）。

        Returns:
            队列中所有记录的列表；队列为空或文件不存在时返回空列表。
        """
        with self._lock(shared=True):
            return self._read_unlocked()

    def get(self, item_id: str) -> dict | None:
        """按 id 查找单条记录。

        Args:
            item_id: 记录 id（append 返回的值）。

        Returns:
            匹配的记录；不存在时返回 None。
        """
        for item in self.list():
            if item.get("id") == item_id:
                return item
        return None

    def remove(self, item_id: str) -> bool:
        """移除一条记录（重写文件）——处理完成的语义。

        Args:
            item_id: 记录 id（append 返回的值）。

        Returns:
            True 表示记录存在并已移除；False 表示记录不存在（无需操作）。
        """
        with self._lock():
            items = self._read_unlocked()
            kept = [i for i in items if i.get("id") != item_id]
            if len(kept) == len(items):
                return False
            self._write_unlocked(kept)
        logger.info("队列移除 [%s]", item_id)
        return True

    def update(self, item_id: str, **fields) -> bool:
        """更新队列项并通过临时文件替换，避免写出半条记录。"""
        with self._lock():
            items = self._read_unlocked()
            for item in items:
                if item.get("id") == item_id:
                    item.update(fields)
                    self._write_unlocked(items)
                    return True
            return False

    def count_by_type(self) -> dict[str, int]:
        """按类型统计待处理项数量。

        Returns:
            类型到数量的映射；类型未知的记录归入 "unknown"。
        """
        counts: dict[str, int] = {}
        for item in self.list():
            t = item.get("type", "unknown")
            counts[t] = counts.get(t, 0) + 1
        return counts
