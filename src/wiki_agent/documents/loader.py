from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from wiki_agent.log import emit_event, get_logger

logger = get_logger("DATALOADER")


class FileModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    RICH = "rich"  # 富文档（PDF、docx 等）


class RawFileProperties(BaseModel):
    """单个文件的原始属性，供转换器与切块器使用。"""

    name: str
    ext: str  # 不含点，如 "txt"
    path: Path
    modality: FileModality
    content: str = ""
    size_bytes: int = 0
    encoding: str | None = None
    create_time: str = ""  # ISO 格式


class LoadSummary(BaseModel):
    """一次加载操作的汇总：已加载与被跳过的文件。"""

    files: list[RawFileProperties] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    total_found: int = 0
    by_modality: dict[str, int] = Field(default_factory=dict)


class DataLoader:
    """文件发现 → 属性提取 → 文本内容读取。

    只产出 ``RawFileProperties``，不做转换与切块。
    """

    # 扩展名 → 模态
    ext_to_modality: dict[str, FileModality] = {
        ".txt": FileModality.TEXT,
        ".py": FileModality.TEXT,
        ".js": FileModality.TEXT,
        ".json": FileModality.TEXT,
        ".yaml": FileModality.TEXT,
        ".yml": FileModality.TEXT,
        ".csv": FileModality.TEXT,
        ".xml": FileModality.TEXT,
        ".html": FileModality.TEXT,
        ".log": FileModality.TEXT,
        ".sql": FileModality.TEXT,
        ".sh": FileModality.TEXT,
        ".toml": FileModality.TEXT,
        ".cfg": FileModality.TEXT,
        ".ini": FileModality.TEXT,
        ".env": FileModality.TEXT,
        ".rst": FileModality.TEXT,
        ".jpg": FileModality.IMAGE,
        ".jpeg": FileModality.IMAGE,
        ".png": FileModality.IMAGE,
        ".gif": FileModality.IMAGE,
        ".webp": FileModality.IMAGE,
        ".svg": FileModality.IMAGE,
        ".bmp": FileModality.IMAGE,
        ".md": FileModality.RICH,
        ".markdown": FileModality.RICH,
        ".pdf": FileModality.RICH,
        ".doc": FileModality.RICH,
        ".docx": FileModality.RICH,
        ".pptx": FileModality.RICH,
        ".xlsx": FileModality.RICH,
    }

    _TEXT_ENCODINGS: tuple[str, ...] = ("utf-8", "gbk", "gb2312", "latin-1")

    def load(
        self,
        files: list[str | Path],
        base_path: str | Path = ".",
    ) -> LoadSummary:
        """加载显式指定的文件列表。

        Args:
            files: 文件路径列表（相对 base_path）。
            base_path: 基准目录。

        Returns:
            汇总，含已加载与被跳过的文件。
        """
        base = Path(base_path)
        paths = [base / Path(f) for f in files]
        return self._load_from_paths(paths)

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

    def _get_file_properties(
        self, file: Path, summary: LoadSummary | None = None
    ) -> RawFileProperties | None:
        """单文件属性提取 + 文本内容读取。

        Args:
            file: 文件路径。
            summary: 汇总（跳过时记录原因）。

        Returns:
            RawFileProperties；文件不存在、非文件、扩展名不支持
            或内容为空时返回 None。
        """
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
            if self._is_empty_content(content):
                self._skip(file, f"空文件（{size}B，无有效内容）", summary)
                emit_event("file_skipped", file=file.name, reason="empty", size_bytes=size)
                return None

        return RawFileProperties(
            name=file.name,
            ext=ext.lstrip("."),
            path=file,
            modality=modality,
            content=content,
            size_bytes=size,
            encoding=encoding,
            create_time=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        )

    def _read_text(self, file: Path) -> tuple[str, str | None]:
        """尝试多种编码读取文本。

        Args:
            file: 文件路径。

        Returns:
            (content, encoding)；全部失败时以 utf-8 加 errors=replace 解码。
        """
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

    def _is_empty_content(self, content: str) -> bool:
        """判断文本内容是否语义为空。

        - 空串或纯空白
        - JSON 空容器：[]、{} 及含空白的变体

        Args:
            content: 文本内容。

        Returns:
            True 表示语义为空。
        """
        stripped = content.strip()
        if not stripped:
            return True
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

