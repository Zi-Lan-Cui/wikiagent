"""watch 状态持久层——记录每个文件的已知指纹与两段确认现场。

进程重启后凭状态判断哪些文件变了（启动 reconcile 的数据源）；
两段确认（保存抖动过滤）的中间现场也存这里——重启不丢半次确认。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def digest_file_text(path: str | Path) -> tuple[str, str] | None:
    """内容指纹唯一配方——read_text(errors=replace) + sha256。

    watcher 判变更、consumer/ack 核账必须调用同一函数：两侧各自手写
    哈希配方迟早漂移（digest 对不上 = 永远无法确认完成）。
    读失败（消失/权限）返回 None。
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), text


@dataclass
class FileState:
    """单个文件的已知状态。"""

    hash: str = ""  # 已知内容哈希（sha256）
    text: str | None = None  # 已 ingest 过的内容文本（None = 从未 ingest）
    pending_text: str | None = None  # 两段确认的第一段内容（待第二次确认）
    pending_seen: int = 0  # pending 内容被看到的轮询次数
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
    def from_dict(cls, d: dict) -> FileState:
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

    # 访问

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

    # 完成账本——hash/text 的唯一写入口是 record，且只允许 job 成功时调用

    def matches(self, abs_path: str, digest: str) -> bool:
        """该内容是否已确认完成（幂等短路 + 扫描去重共用）。"""
        return (
            bool(digest)
            and self._entries.get(abs_path) is not None
            and (self._entries[abs_path].hash == digest)
        )

    def record(self, abs_path: str, digest: str, text: str) -> None:
        """成功核账：把"确实进入 Wiki 的内容"记为已处理并落盘。"""
        st = self._entries.get(abs_path) or FileState()
        st.hash = digest
        st.text = text
        st.pending_text = None
        st.pending_seen = 0
        st.last_ingested_at = datetime.now().isoformat()
        self._entries[abs_path] = st
        self.save()

    # 持久化

    def save(self) -> None:
        """原子写: 先写临时文件再 rename——避免中途崩溃留半个 JSON。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        payload = {path: st.to_dict() for path, st in self._entries.items()}
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
            path: FileState.from_dict(d) for path, d in raw.items() if isinstance(d, dict)
        }
