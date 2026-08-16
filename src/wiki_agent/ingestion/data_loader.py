import hashlib
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from wiki_agent.log import emit_event, get_logger

logger=get_logger("DATALOADER")

class FileModality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    RICH = "rich"  # 多模态/富文档（PDF、docx 等）


class RawFileProperties(BaseModel):
    """单个文件的原始属性，等待送入 chunker。"""

    name: str
    ext: str  # 不含点，如 "txt"
    path: Path
    modality: FileModality
    content: str = ""
    size_bytes: int = 0
    encoding: str | None = None
    content_hash: str | None = None  # sha256 用于去重追踪
    create_time: str = ""  # ISO 格式


class LoadSummary(BaseModel):
    """一次加载操作的汇总——已加载 & 被跳过的文件。"""

    files: list[RawFileProperties] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    total_found: int = 0
    by_modality: dict[str, int] = Field(default_factory=dict)

    @property
    def loaded_count(self) -> int:
        return len(self.files)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    def iter_text(self):
        for f in self.files:
            if f.modality == FileModality.TEXT:
                yield f

class DataLoader:
    """文件发现 → 属性提取 → 文本内容读取。

    不负责 chunk——只产出 ``RawFileProperties``，由 Processor/Chunker 消费。
    """

    # 扩展名 → 模态
    ext_to_modality: dict[str, FileModality] = {
        # ── 纯文本 ──
        ".txt": FileModality.TEXT,
        ".py": FileModality.TEXT,       ".js": FileModality.TEXT,
        ".json": FileModality.TEXT,     ".yaml": FileModality.TEXT,
        ".yml": FileModality.TEXT,      ".csv": FileModality.TEXT,
        ".xml": FileModality.TEXT,      ".html": FileModality.TEXT,
        ".log": FileModality.TEXT,      ".sql": FileModality.TEXT,
        ".sh": FileModality.TEXT,       ".toml": FileModality.TEXT,
        ".cfg": FileModality.TEXT,      ".ini": FileModality.TEXT,
        ".env": FileModality.TEXT,      ".rst": FileModality.TEXT,
        # ── 图片 ──
        ".jpg": FileModality.IMAGE,     ".jpeg": FileModality.IMAGE,
        ".png": FileModality.IMAGE,     ".gif": FileModality.IMAGE,
        ".webp": FileModality.IMAGE,    ".svg": FileModality.IMAGE,
        ".bmp": FileModality.IMAGE,
        # ── 多模态/富文档 ──
        ".md": FileModality.RICH,       ".markdown": FileModality.RICH,
        ".pdf": FileModality.RICH,      ".doc": FileModality.RICH,
        ".docx": FileModality.RICH,     ".pptx": FileModality.RICH,
        ".xlsx": FileModality.RICH,
    }

    _TEXT_ENCODINGS: tuple[str, ...] = ("utf-8", "gbk", "gb2312", "latin-1")

    # ── 公开 API ──────────────────────────────────────────

    def load(
        self,
        files: list[str | Path],
        base_path: str | Path = ".",
    ) -> LoadSummary:
        """加载显式指定的文件列表，返回汇总（含已加载 & 跳过）。"""
        base = Path(base_path)
        paths = [base / Path(f) for f in files]
        return self._load_from_paths(paths)

    # 递归扫描默认排除的目录——历史运行档案/版本控制/虚拟环境
    # 不是源材料（.logs/runs 里有几百个历史页面副本，吃进去会污染 wiki）
    _EXCLUDED_DIRS = {".git", ".venv", ".logs", ".watch", "__pycache__",
                      ".pytest_cache", "node_modules"}

    def load_dir(
        self,
        directory: str | Path,
        *,
        recursive: bool = False,
    ) -> LoadSummary:
        """扫描目录下所有可识别文件（排除 _EXCLUDED_DIRS）。

        recursive 默认 False——"加载指定目录"就是字面意义，只扫
        这一层。树扫描显式 opt-in（实测事故: 默认递归把源目录里的
        旧 wiki 构建 first_wiki/ 和工具配置 .llm-wiki/ 全部吃进去，
        6 行配置 JSON 被 LLM extract 编造出整条物理页幻觉链）。
        """
        dir_path = Path(directory)
        if not dir_path.is_dir():
            logger.warning(f"{dir_path} 不是目录，跳过")
            return LoadSummary()

        if recursive:
            paths = [
                p for p in dir_path.rglob("*")
                if p.is_file()
                and not any(part in self._EXCLUDED_DIRS for part in p.parts)
            ]
        else:
            paths = [p for p in dir_path.glob("*") if p.is_file()]
        logger.info(f"在 {dir_path} 中发现 {len(paths)} 个文件")
        return self._load_from_paths(paths)

    # ── 内部 ──────────────────────────────────────────────

    def _load_from_paths(self, paths: list[Path]) -> LoadSummary:
        summary = LoadSummary(
            total_found=len(paths),
            by_modality={"text": 0, "image": 0, "rich": 0},
        )

        for p in paths:
            props = self._get_file_properties(p, summary)
            if props is None:
                continue
            summary.files.append(props)
            summary.by_modality[props.modality.value] += 1
            logger.debug("  加载: [%s] %s (%s)", props.modality.value, props.name, props.ext)

        return summary

    def _get_file_properties(self, file: Path, summary: LoadSummary | None = None) -> RawFileProperties | None:
        """单文件属性提取 + 文本内容读取。"""
        if not file.exists():
            self._skip(file, "文件不存在", summary)
            return None
        if not file.is_file():
            self._skip(file, "不是文件", summary)
            return None

        ext = file.suffix.lower()
        if ext not in self.ext_to_modality:
            self._skip(file, f"不支持的扩展名 {ext}", summary)
            return None

        modality = self.ext_to_modality[ext]
        stat = file.stat()
        size = stat.st_size

        content, encoding = "", None
        if modality == FileModality.TEXT:
            content, encoding = self._read_text(file)
            # 空内容拦截: 0 字节 / 纯空白 / JSON 空容器（[]、{}）
            if self._is_empty_content(content):
                self._skip(file, f"空文件（{size}B，无有效内容）", summary)
                emit_event("file_skipped", file=file.name, reason="empty",
                           size_bytes=size)
                return None

        content_hash = None
        if size > 0:
            content_hash = self._hash_file(file)

        return RawFileProperties(
            name=file.name,
            ext=ext.lstrip("."),
            path=file,
            modality=modality,
            content=content,
            size_bytes=size,
            encoding=encoding,
            content_hash=content_hash,
            create_time=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        )

    def _read_text(self, file: Path) -> tuple[str, str | None]:
        """尝试多种编码读取文本。返回 (content, encoding)。"""
        for enc in self._TEXT_ENCODINGS:
            try:
                return file.read_text(encoding=enc), enc
            except (UnicodeDecodeError, UnicodeError):
                continue
        raw = file.read_bytes()
        return raw.decode("utf-8", errors="replace"), None

    def _skip(self, file: Path, reason: str, summary: LoadSummary | None = None) -> None:
        logger.warning(f"跳过 {file}: {reason}")
        if summary is not None:
            summary.skipped.append({"name": file.name, "path": str(file), "reason": reason})


    def _is_empty_content(self,content: str) -> bool:
        """判断文本内容是否"语义为空"。

        - 空串 / 纯空白
        - JSON 空容器: []、{}、[ ]、{ }（如 conversations.json = []）
        - 其他格式的空骨架（空列表/空字典）
        """
        stripped = content.strip()
        if not stripped:
            return True
        # JSON 空容器检测（兼容前后空白）
        if stripped in ("[]", "{}"):
            return True
        try:
            import json
            data = json.loads(stripped)
            if isinstance(data, (list, dict)) and len(data) == 0:
                return True
        except (json.JSONDecodeError, ValueError):
            pass
        return False


    def _hash_file(self,file: Path) -> str | None:
        """SHA256 用于去重追踪。"""
        try:
            sha = hashlib.sha256()
            with open(file, "rb") as fh:
                sha.update(fh.read(65536))
            return sha.hexdigest()
        except OSError:
            return None


# ────────────────────────────────────────────────────────────────
#  main — 调试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    loader = DataLoader()
    target = sys.argv[1] if len(sys.argv) > 1 else "."

    summary = loader.load_dir(target)

    print(f"\n扫描: {target}")
    print(f"发现: {summary.total_found}  加载: {summary.loaded_count}  跳过: {summary.skipped_count}")
    print(f"模态: {summary.by_modality}")

    print("\n── 文本文件预览 ──")
    for f in summary.iter_text():
        preview = f.content[:60].replace("\n", "\\n") if f.content else "(空)"
        print(f"  [{f.ext}] {f.name}  ({f.size_bytes}B)  {preview}...")
