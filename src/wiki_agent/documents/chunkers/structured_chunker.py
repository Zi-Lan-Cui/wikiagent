"""结构化数据块切割器——CSV / JSON / JSONL / Excel"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import datetime

from wiki_agent.documents.chunkers.base import BaseChunker, ChunkedFileProperties
from wiki_agent.documents.converters.base import ConvertedFile
from wiki_agent.log import get_logger

logger = get_logger("STRUCTURED_CHUNKER")


class StructuredChunker(BaseChunker):
    """CSV / JSON / JSONL → 结构化 chunk。"""

    _SUPPORTED = {"csv", "json", "jsonl"}

    def __init__(self, *, batch_size: int = 20):
        self._batch_size = batch_size

    def can_process(self, file: ConvertedFile) -> bool:
        # 模态为 text 且扩展名匹配——文件可能已被 Converter 转义
        return file.modality == "text" and file.ext in self._SUPPORTED


    def chunk(self, file: ConvertedFile) -> list[ChunkedFileProperties]:
        """按扩展名分派切分。

        Args:
            file: 转换后的文件。

        Returns:
            chunk 列表（不支持的类型返回 []）。
        """
        if file.ext == "csv":
            return self._chunk_csv(file)
        if file.ext in ("json", "jsonl"):
            return self._chunk_json(file)
        return []

    def _chunk_csv(self, file: ConvertedFile) -> list[ChunkedFileProperties]:
        """CSV → 按 batch_size 行分组的 chunk（保留表头）。

        Args:
            file: 转换后的文件。

        Returns:
            chunk 列表（解析失败返回 [] 降级纯文本）。
        """
        try:
            reader = csv.DictReader(io.StringIO(file.content))
            headers = reader.fieldnames or []
        except csv.Error:
            logger.warning(f"CSV 解析失败: {file.name}，降级为纯文本 chunk")
            return []

        chunks: list[ChunkedFileProperties] = []
        batch_rows: list[dict] = []
        chunk_index = 0

        for row in reader:
            batch_rows.append(row)
            if len(batch_rows) >= self._batch_size:
                chunks.append(
                    self._make_chunk(
                        chunk_index,
                        batch_rows,
                        headers,
                        file,
                    )
                )
                chunk_index += 1
                batch_rows = []

        # 剩余行
        if batch_rows:
            chunks.append(
                self._make_chunk(
                    chunk_index,
                    batch_rows,
                    headers,
                    file,
                )
            )

        logger.info(f"CSV {file.name}: {chunk_index + 1} chunks")
        return chunks

    def _chunk_json(self, file: ConvertedFile) -> list[ChunkedFileProperties]:
        """JSON/JSONL → 结构化 chunk。

        JSONL 每行一个 chunk；JSON 数组按元素、对象按顶级 key 拆分。

        Args:
            file: 转换后的文件。

        Returns:
            chunk 列表（解析失败返回 []）。
        """
        is_jsonl = file.ext == "jsonl"
        chunks: list[ChunkedFileProperties] = []
        chunk_index = 0

        if is_jsonl:
            for line in file.content.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"JSONL 行解析失败: {line[:60]}...")
                    continue
                chunks.append(
                    self._make_chunk(
                        chunk_index,
                        item,
                        [],
                        file,
                    )
                )
                chunk_index += 1
            return chunks

        # 非 JSONL：解析整个文档
        try:
            data = json.loads(file.content)
        except json.JSONDecodeError:
            logger.warning(f"JSON 解析失败: {file.name}")
            return []

        if isinstance(data, list):
            for item in data:
                chunks.append(
                    self._make_chunk(
                        chunk_index,
                        item,
                        [],
                        file,
                    )
                )
                chunk_index += 1

        elif isinstance(data, dict):
            for key, value in data.items():
                chunks.append(
                    self._make_chunk(
                        chunk_index,
                        {key: value},
                        [],
                        file,
                    )
                )
                chunk_index += 1

        else:
            chunks.append(self._make_chunk(chunk_index, data, [], file))

        logger.info(f"JSON {file.name}: {len(chunks)} chunks")
        return chunks

    def _make_chunk(
        self,
        index: int,
        data: list[dict] | dict | object,
        headers: Sequence[str],
        file: ConvertedFile,
    ) -> ChunkedFileProperties:
        """构造单个 chunk（JSON 序列化 + 元数据）。

        Args:
            index: chunk 序号。
            data: 行数据/元素/键值对。
            headers: CSV 列名（metadata 用）。
            file: 转换后的文件。

        Returns:
            ChunkedFileProperties。
        """
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return ChunkedFileProperties(
            content=content,
            chunk_index=index,
            metadata={
                "create_time": datetime.now().isoformat(),
                "file_name": file.name,
                "file_path": str(file.path),
                "headers": headers,
                "row_count": len(data) if isinstance(data, list) else 1,
            },
        )
