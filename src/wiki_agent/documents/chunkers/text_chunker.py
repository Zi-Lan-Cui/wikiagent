"""文本块切割器——Markdown 感知 + 段落边界优先。

适用于 Markdown、纯文本等由 MinerU / DataLoader 产出的文本内容。

**已处理**:

- 优先在 ``#`` / ``##`` 标题边界切分，保证每个 chunk 是完整 section
- section 内优先在段落边界（``\\n\\n``）切分
- 短 section 自动合并：累积到接近 ``max_chunk_size`` 再输出
- 尾部短块合并：最后一段如果太短，合并到前一个 chunk
- 单段落过长时在句子边界（``。！？.!?``）降级切分
- 依赖 ``_build_sections`` 将内容解析为 section 列表，所有后续切分操作基于此列表

**未处理**:

- 三级及以下标题（``###``）：当前不做为 section 边界，保留在上级 section 内
- 代码块（`` ``` ``）内的空行：可能会被误判为段落边界
- 表格（`` |...| ``）：表格行之间被 ``\\n`` 分隔，当前不保证表格不被切开
- 引用块（`` > ``）：不保留引用结构的连续性
- 嵌套列表的缩进结构：纯文本化后缩进丢失，靠空行保留分组
- LaTeX 公式块（``$$``）：不保证不被切分
- 图片引用（``![]()``）：不特殊处理，作为普通文本
- 中文 / 日文无空格文本：``len()`` 按字符数而非 token 数计算，高估 chunk 容量
- Token-aware 切分：当前用 ``len()`` 近似，不做 tiktoken 精确计算
- 跨 section 语义关联：不做相关的 section 合并（如 ``## 相关`` 应靠拢 ``# 主题``）
- 页眉 / 页脚残留：如果 MinerU 未清除，chunker 不单独过滤
"""

from __future__ import annotations

import re
from datetime import datetime

from wiki_agent.documents.chunkers.base import BaseChunker, ChunkedFileProperties
from wiki_agent.documents.converters.base import ConvertedFile
from wiki_agent.log import get_logger

logger = get_logger("TEXT_CHUNKER")

# 标题行正则：匹配 # / ## / ### / ####
_HEADING_PATTERN = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)

# 句子边界：中文 + 英文标点后跟空白或行尾
_SENTENCE_END = re.compile(r"[。！？.!?](\s+|$)")


class TextChunker(BaseChunker):
    """Markdown / 纯文本 → 语义 chunk。

    只处理纯文本格式（.txt, .md, .py, .json, .yaml 等 DataLoader TEXT 模态的文件）。
    不接受结构化格式（.csv, .xlsx），由 StructuredChunker 处理。
    """

    # 未处理项（已在 docstring 中说明，这里列出当前不覆盖的范围）
    # NOTE: 未来可加 _TABLE_PATTERN / _CODE_BLOCK_PATTERN 等局部优化

    def __init__(
        self,
        *,
        max_chunk_size: int = 1000,
        min_chunk_size: int = 50,
    ):
        self._max_chunk_size = max_chunk_size
        self._min_chunk_size = min_chunk_size

    # BaseChunker 接口

    def can_process(self, file: ConvertedFile) -> bool:
        """只要模态是 text 且内容非空就接受——兜底 chunker。

        StructuredChunker 在 dispatcher 中排前面，先拦截 csv/json/jsonl。

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

        # 1. 解析为 (section, heading_path) 列表
        sections = self._build_sections(content)

        # 2. section → 段落 → (chunk, heading_path)（累积 + 切分）
        raw_chunks = self._sections_to_chunks(sections)

        # 3. 尾部合并（同步标题）
        merged = self._merge_tail(raw_chunks)

        # 4. 包装
        return [
            ChunkedFileProperties(
                content=chunk_text,
                chunk_index=i,
                # 键一次写全（外层定义再 **展开的旧写法要两处拼图）
                metadata={
                    "file_name": file.name,
                    "file_path": str(file.path),
                    "create_time": datetime.now().isoformat(),
                    "heading_path": heading,
                },
            )
            for i, (chunk_text, heading) in enumerate(merged)
        ]

    # Section 构建

    def _build_sections(self, content: str) -> list[tuple[str, str]]:
        """将文本按 ``#`` / ``##`` 标题拆分为 (section, heading_path) 列表。

        标题路径是 chunk 的归属信息——源头就记录，下游不再重新解析
        （下游重解析可能猜错，源头永远正确）。
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
            # 标题路径: "# 章节 > ## 子节" 形式（.group(1) 是 # 的个数）
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

    # Section → Chunk

    def _sections_to_chunks(
        self,
        sections: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """section → 段落累积 → (chunk, heading_path) 列表。

        累积合并时保留第一个非空标题（合并块归属它开头的 section）。

        Args:
            sections: (section 文本, 标题路径) 列表。

        Returns:
            (chunk 文本, 标题路径) 列表。
        """
        chunks: list[tuple[str, str]] = []

        for section, heading in sections:
            paragraphs = self._split_paragraphs(section)

            for para in paragraphs:
                # 短段落：累积（标题保留第一个非空——当前块归属）
                if chunks and len(chunks[-1][0]) + len(para) + 2 <= self._max_chunk_size:
                    text = chunks[-1][0] + "\n\n" + para
                    h = chunks[-1][1] or heading
                    chunks[-1] = (text, h)
                # 超长段落：拆解（子块共用 section 标题）
                elif len(para) > self._max_chunk_size:
                    sub_chunks = self._split_oversized(para)
                    chunks.extend((sc, heading) for sc in sub_chunks)
                # 新开 chunk
                else:
                    chunks.append((para, heading))

        return chunks

    # 段落边界

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        """按双换行切分段落，保留空行分隔。

        Args:
            text: 文本。

        Returns:
            非空段落列表。
        """
        parts = re.split(r"\n\s*\n", text)
        return [p.strip() for p in parts if p.strip()]

    # 超长降级

    def _split_oversized(self, text: str) -> list[str]:
        """单个内容过长时，先在句子边界切分；没有句子则折半。

        Args:
            text: 超长段落。

        Returns:
            子块列表。
        """
        # 重组句子（因为 split 会丢弃分隔符部分）
        chunks: list[str] = []
        current = ""
        # re.split with capture groups gives alternating [text, sep, text, sep, ...]
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

        # 如果句子切分没产生任何效果（没有句子边界），折半
        if not chunks or len(chunks) == 1:
            mid = len(text) // 2
            return [text[:mid].strip(), text[mid:].strip()]

        return chunks

    # 尾部合并

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
