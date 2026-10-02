"""文本切割器：按 Markdown 标题与段落边界切分。"""

from __future__ import annotations

import re
from datetime import datetime

from wiki_agent.documents.chunkers.base import BaseChunker, ChunkedFileProperties
from wiki_agent.documents.converters.base import ConvertedFile
from wiki_agent.log import get_logger

logger = get_logger("TEXT_CHUNKER")

# Markdown 标题行（# 至 ####）
_HEADING_PATTERN = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)

# 句子边界：中文 + 英文标点后跟空白或行尾
_SENTENCE_END = re.compile(r"[。！？.!?](\s+|$)")


class TextChunker(BaseChunker):
    """Markdown、纯文本 → 语义 chunk。

    处理 TEXT 模态的文件；csv、json、jsonl 由 StructuredChunker 优先处理。
    """

    def __init__(
        self,
        *,
        max_chunk_size: int = 1000,
        min_chunk_size: int = 50,
    ):
        self._max_chunk_size = max_chunk_size
        self._min_chunk_size = min_chunk_size

    def can_process(self, file: ConvertedFile) -> bool:
        """模态为 text 且内容非空即接受。

        dispatcher 默认把 StructuredChunker 排在前，csv、json、jsonl 不会到这里。

        Args:
            file: 转换后的文件。

        Returns:
            True 表示支持处理。
        """
        return file.modality == "text" and bool(file.content.strip())

    def chunk(self, file: ConvertedFile) -> list[ChunkedFileProperties]:
        """切割文件为语义 chunk。

        Args:
            file: 转换后的文件。

        Returns:
            chunk 列表（空内容返回 []）。
        """
        content = file.content
        if not content.strip():
            logger.warning(f"TextChunker 跳过空内容: {file.name}")
            return []

        sections = self._build_sections(content)
        raw_chunks = self._sections_to_chunks(sections)
        merged = self._merge_tail(raw_chunks)
        return [
            ChunkedFileProperties(
                content=chunk_text,
                chunk_index=i,
                metadata={
                    "file_name": file.name,
                    "file_path": str(file.path),
                    "create_time": datetime.now().isoformat(),
                    "heading_path": heading,
                },
            )
            for i, (chunk_text, heading) in enumerate(merged)
        ]

    def _build_sections(self, content: str) -> list[tuple[str, str]]:
        """将文本按标题拆分为 (section, heading_path) 列表。

        标题路径在切分时记录，下游不再重新解析，避免重新推断归属出错。
        无标题的纯文本返回单元素列表（heading 为空串）。

        Args:
            content: 文本内容。

        Returns:
            (section 文本, 标题路径) 列表。
        """
        if not re.search(r"^#{1,4}\s", content, re.MULTILINE):
            return [(content, "")]

        sections: list[tuple[str, str]] = []
        matches = list(_HEADING_PATTERN.finditer(content))

        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            depth = len(match.group(1))
            heading = match.group(2).strip()
            path = f"{'#' * depth} {heading}"
            sections.append((content[start:end].strip(), path))

        # 第一个标题之前的文本（如果有）作为独立 section
        if matches and matches[0].start() > 0:
            preamble = content[: matches[0].start()].strip()
            if preamble:
                sections.insert(0, (preamble, ""))

        return sections

    def _sections_to_chunks(
        self,
        sections: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """section → 段落累积 → (chunk, heading_path) 列表。

        并入前一块时标题保留第一个非空值（块归属其开头的 section）。

        Args:
            sections: (section 文本, 标题路径) 列表。

        Returns:
            (chunk 文本, 标题路径) 列表。
        """
        chunks: list[tuple[str, str]] = []

        for section, heading in sections:
            paragraphs = self._split_paragraphs(section)

            for para in paragraphs:
                if chunks and len(chunks[-1][0]) + len(para) + 2 <= self._max_chunk_size:
                    text = chunks[-1][0] + "\n\n" + para
                    h = chunks[-1][1] or heading
                    chunks[-1] = (text, h)
                elif len(para) > self._max_chunk_size:
                    # 超长段落拆分，子块共用 section 标题
                    sub_chunks = self._split_oversized(para)
                    chunks.extend((sc, heading) for sc in sub_chunks)
                else:
                    chunks.append((para, heading))

        return chunks

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        """按双换行切分段落。

        Args:
            text: 文本。

        Returns:
            非空段落列表。
        """
        parts = re.split(r"\n\s*\n", text)
        return [p.strip() for p in parts if p.strip()]

    def _split_oversized(self, text: str) -> list[str]:
        """单个内容过长时，先在句子边界切分；没有句子则折半。

        Args:
            text: 超长段落。

        Returns:
            子块列表。
        """
        chunks: list[str] = []
        current = ""
        # re.split 带捕获组时交替返回 [文本, 分隔符, ...]，需重组回完整句子
        parts = _SENTENCE_END.split(text)
        i = 0
        while i < len(parts):
            if i + 1 < len(parts) and _SENTENCE_END.match(parts[i + 1]):
                sentence = parts[i] + parts[i + 1]
                i += 2
            else:
                sentence = parts[i]
                i += 1

            if len(current) + len(sentence) <= self._max_chunk_size:
                current += sentence
            else:
                if current.strip():
                    chunks.append(current.strip())
                current = sentence

        if current.strip():
            chunks.append(current.strip())

        if not chunks or len(chunks) == 1:
            mid = len(text) // 2
            return [text[:mid].strip(), text[mid:].strip()]

        return chunks

    def _merge_tail(
        self,
        chunks: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """最后一个 chunk 太短则合并到前一个（标题保留前一个的）。

        Args:
            chunks: (chunk 文本, 标题路径) 列表。

        Returns:
            合并尾部后的列表。
        """
        if len(chunks) < 2:
            return chunks
        if len(chunks[-1][0]) >= self._min_chunk_size:
            return chunks
        text = chunks[-2][0] + "\n\n" + chunks[-1][0]
        chunks[-2] = (text, chunks[-2][1])
        return chunks[:-1]
