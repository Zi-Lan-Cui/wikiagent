"""sync 完成账持久层——记录每个文件的"已处理"账。

hash/text 只在 job 成功后由 record 写入；本模块同时提供 sync 快照的
两半：scan_disk（磁盘现状指纹表）与 SyncState.diff（现状 − 账本 =
待同步/待清理）。sync 语义下"账本没有的内容"即脏，失败不写账 →
失败内容保持脏 → 再次 sync 天然就是重试。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def scan_disk(root: str | Path) -> dict[str, str]:
    """受支持文件的指纹表：绝对路径 → digest。sync 快照的数据源。

    读不到（权限/消失竞态）的文件跳过——下轮快照会再见到它。
    """
    from wiki_agent.documents.loader import DataLoader

    supported = DataLoader.ext_to_modality
    base = Path(root).resolve()
    out: dict[str, str] = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in supported:
            continue
        read = digest_file_text(p)
        if read is not None:
            out[str(p.resolve())] = read[0]
    return out


def digest_file_text(path: str | Path) -> tuple[str, str] | None:
    """内容指纹唯一配方——read_text(errors=replace) + sha256。

    sync 判变更、consumer 落账必须调用同一函数：两侧各自手写哈希配方
    迟早漂移（digest 对不上 = 永远无法确认完成）。
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

    hash: str = ""  # 已处理内容的哈希（sha256，只在 job 成功时写）
    text: str | None = None  # 已 ingest 过的内容文本（None = 从未 ingest）
    last_ingested_at: str = ""

    def to_dict(self) -> dict:
        """序列化为字典（持久化格式）。

        Returns:
            字段字典。
        """
        return {
            "hash": self.hash,
            "text": self.text,
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
            last_ingested_at=d.get("last_ingested_at", ""),
        )


class SyncState:
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

    def diff(self, disk: dict[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
        """sync 快照对比：磁盘现状 − 完成账。

        dirty = 账上没有该 digest 的文件（新文件/改过/失败过——失败不写账
        所以保持脏）；removed = 账上有成功记录但磁盘已无的文件（名册式的
        空条目不参与删除判定：从未入账，无账可清）。

        Args:
            disk: scan_disk 产出的 绝对路径→digest 表。

        Returns:
            ([(路径, digest)], [待清理路径])。
        """
        dirty = [(path, digest) for path, digest in disk.items() if self.get(path).hash != digest]
        removed = [
            old for old in self.all_paths() if old not in disk and self._entries[old].hash != ""
        ]
        return dirty, removed

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
