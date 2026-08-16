"""统一异常队列——处理失败事项的人机接口。

生命周期区别（单真相源原则的继承）:
- events.jsonl: "发生了什么"的永久事实（机器读，从不删除）
- queue.jsonl:   "待用户处理"的临时事项（人读，处理完即移除）

二者不重复——事件按"发生"记录，队列按"待决"记录；队列项
被移除时事件仍在，审计不丢。失败重试成功 = 队列项完成，
不需要第二份清单（failed.json 的教训）。

队列项类型:
- ingest_failure:  compile/refine/watch 的文件 ingest 失败
                  （stage/source/file/error）
- surgery_conflict: 手术冲突无法仲裁（kind/detail/proposals）
- correction:      QA 纠错待裁决（corrections.md 的队列视图，
                  由 /queue 聚合展示，裁决走 /resolve）

文件格式: JSONL 追加（append 原子友好，remove 重写文件——量小）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from wiki_agent.log import get_logger

logger = get_logger("QUEUE")


class QueueStore:
    """workspace/queue.jsonl——失败事项的追加/列出/移除。"""

    def __init__(self, workspace: str | Path):
        self._file = Path(workspace) / "queue.jsonl"

    def append(self, type_: str, **fields) -> str:
        """追加一条待处理项，返回生成的 id。"""
        ts = datetime.now().isoformat()
        item_id = f"{type_}_{ts.replace(':', '').replace('-', '').replace('.', '')}"
        record = {"id": item_id, "type": type_, "ts": ts, **fields}
        with open(self._file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("队列新增 [%s] %s", type_, item_id)
        return item_id

    def list(self) -> list[dict]:
        """全部待处理项（按时间顺序）。"""
        try:
            with open(self._file, encoding="utf-8") as f:
                return [
                    json.loads(line)
                    for line in f
                    if line.strip()
                ]
        except FileNotFoundError:
            return []

    def get(self, item_id: str) -> dict | None:
        for item in self.list():
            if item.get("id") == item_id:
                return item
        return None

    def remove(self, item_id: str) -> bool:
        """移除一条（重写文件）——处理完成的语义。"""
        items = self.list()
        kept = [i for i in items if i.get("id") != item_id]
        if len(kept) == len(items):
            return False
        with open(self._file, "w", encoding="utf-8") as f:
            for item in kept:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        logger.info("队列移除 [%s]", item_id)
        return True

    def count_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.list():
            t = item.get("type", "unknown")
            counts[t] = counts.get(t, 0) + 1
        return counts
