"""watch 状态持久层——记录每个文件的已知指纹与两段确认现场。

这是 watcher 的持久层（队列本身不持久）:
- 进程重启后凭 state 判断哪些文件变了（启动 reconcile 的数据源）
- 两段确认（保存抖动过滤）的中间现场也存这里——重启不丢半次确认

文件: ``wiki/.watch/state.json``
格式: {"<绝对路径>": {"hash": str, "text": str|None, "pending_text": str|None,
                      "pending_seen": int, "last_ingested_at": str}}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileState:
    """单个文件的已知状态。"""

    hash: str = ""          # 已知内容哈希（sha256）
    text: str | None = None  # 已 ingest 过的内容文本（None = 从未 ingest）
    pending_text: str | None = None  # 两段确认的第一段内容（待第二次确认）
    pending_seen: int = 0   # pending 内容被看到的轮询次数
    last_ingested_at: str = ""

    def to_dict(self) -> dict:
        """序列化为字典（持久化格式）。

        Returns:
            字段字典。
        """
        return {
            "hash": self.hash,
            "text": self.text,
            "pending_text": self.pending_text,
            "pending_seen": self.pending_seen,
            "last_ingested_at": self.last_ingested_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileState":
        """从字典构造（读盘）。

        Args:
            d: 持久化字典。

        Returns:
            FileState 实例（缺省字段取默认）。
        """
        return cls(
            hash=d.get("hash", ""),
            text=d.get("text"),
            pending_text=d.get("pending_text"),
            pending_seen=d.get("pending_seen", 0),
            last_ingested_at=d.get("last_ingested_at", ""),
        )


class WatchState:
    """state.json 的读写封装——原子写，读失败回空（首次运行无状态文件）。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._entries: dict[str, FileState] = {}
        self._load()

    # ── 访问 ──────────────────────────────────────────────

    def get(self, abs_path: str) -> FileState:
        """获取文件状态——不存在返回空 FileState（视为新文件）。

        Args:
            abs_path: 文件绝对路径。

        Returns:
            状态对象。
        """
        return self._entries.get(abs_path, FileState())

    def set(self, abs_path: str, state: FileState) -> None:
        """写入/覆盖文件状态。

        Args:
            abs_path: 文件绝对路径。
            state: 状态对象。
        """
        self._entries[abs_path] = state

    def all_paths(self) -> list[str]:
        """返回全部已记录路径。

        Returns:
            路径列表。
        """
        return list(self._entries.keys())

    def drop(self, abs_path: str) -> None:
        """移除条目（源文件被删除时清理）。

        Args:
            abs_path: 文件绝对路径。
        """
        self._entries.pop(abs_path, None)
        self._entries.pop(abs_path, None)

    # ── 持久化 ────────────────────────────────────────────

    def save(self) -> None:
        """原子写: 先写临时文件再 rename——避免中途崩溃留半个 JSON。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        payload = {
            path: st.to_dict() for path, st in self._entries.items()
        }
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self._entries = {
            path: FileState.from_dict(d)
            for path, d in raw.items() if isinstance(d, dict)
        }
