"""sync 状态持久层——记录每个文件的已处理状态。

hash/text 只在 job 成功后由 record 写入。快照由两部分组成：scan_disk
（磁盘现状指纹表）与 SyncState.diff（现状与记录之差 = 待同步/待清理）。
失败不写记录，内容保持待同步，再次 sync 即重试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wiki_agent.snapshots import digest_file_text


def scan_disk(root: str | Path) -> dict[str, str]:
    """扫描受支持文件，返回 绝对路径 → digest 表。

    读不到的文件（权限/竞态消失）跳过，下轮扫描会再见到。
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


@dataclass
class FileState:
    """单个文件的已知状态。"""

    hash: str = ""  # 已处理内容的 sha256
    text: str | None = None  # 已编译内容的文本，None 表示从未编译
    last_ingested_at: str = ""

    def to_dict(self) -> dict:
        """序列化为持久化格式。"""
        return {
            "hash": self.hash,
            "text": self.text,
            "last_ingested_at": self.last_ingested_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FileState:
        """从持久化字典构造，缺省字段取默认。"""
        return cls(
            hash=d.get("hash", ""),
            text=d.get("text"),
            last_ingested_at=d.get("last_ingested_at", ""),
        )


class SyncState:
    """state.json 的读写封装：原子写，读失败视为空（首次运行无文件）。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._entries: dict[str, FileState] = {}
        self._load()

    def get(self, abs_path: str) -> FileState:
        """获取文件状态；不存在返回空 FileState，视为新文件。"""
        return self._entries.get(abs_path, FileState())

    def set(self, abs_path: str, state: FileState) -> None:
        """写入/覆盖文件状态。"""
        self._entries[abs_path] = state

    def all_paths(self) -> list[str]:
        """返回全部已记录路径。"""
        return list(self._entries.keys())

    def drop(self, abs_path: str) -> None:
        """移除条目，源文件被删除时清理。"""
        self._entries.pop(abs_path, None)

    def diff(self, disk: dict[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
        """磁盘现状与已处理记录之差。

        dirty：digest 与记录不符的文件（新文件、改过、失败过）；
        removed：有成功记录但磁盘已无的文件（无成功记录的空条目不参与
        删除判定）。

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

    def matches(self, abs_path: str, digest: str) -> bool:
        """该内容是否已处理（供幂等短路与扫描去重）。"""
        return (
            bool(digest)
            and self._entries.get(abs_path) is not None
            and (self._entries[abs_path].hash == digest)
        )

    def record(self, abs_path: str, digest: str, text: str) -> None:
        """job 成功后调用：把已写入 Wiki 的内容记为已处理并落盘。"""
        st = self._entries.get(abs_path) or FileState()
        st.hash = digest
        st.text = text
        st.last_ingested_at = datetime.now().isoformat()
        self._entries[abs_path] = st
        self.save()

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
