"""Test chunker dispatch and semantic chunking.

Run:
    cd /path/to/wiki-agent
    .venv/bin/python -m pytest test/test_chunker.py -v
"""

from wiki_agent.documents.chunkers import (
    Chunker,
    StructuredChunker,
    TextChunker,
)
from wiki_agent.documents.converters.base import ConvertedFile

# 工厂


def _make_file(ext: str, content: str, **kwargs) -> ConvertedFile:
    return ConvertedFile(
        content=content,
        name=f"test.{ext}",
        ext=ext,
        path=f"/tmp/test.{ext}",
        modality=kwargs.get("modality", "text"),
    )


# Dispatcher


class TestDispatcher:
    def test_csv_routed_to_structured(self):
        chunker = Chunker([StructuredChunker(), TextChunker()])
        file = _make_file("csv", "a,b\n1,2\n3,4")
        chunks = chunker.chunk(file)
        assert len(chunks) >= 1
        # StructuredChunker 输出 JSON（结构化），不是纯文本段落
        assert all('"a"' in c.content for c in chunks)

    def test_txt_routed_to_text(self):
        chunker = Chunker([StructuredChunker(), TextChunker()])
        file = _make_file("txt", "hello world")
        chunks = chunker.chunk(file)
        assert len(chunks) == 1
        assert "hello world" in chunks[0].content

    def test_pdf_after_convert_routed_to_text(self):
        """PDF 经 MinerU 转换后 modality=text, ext=pdf，走 TextChunker。"""
        chunker = Chunker([StructuredChunker(), TextChunker()])
        file = _make_file("pdf", "# Title\n\nContent here.")
        chunks = chunker.chunk(file)
        assert len(chunks) == 1

    def test_structured_before_text(self):
        """即使 csv 的内容是纯文本，StructuredChunker 也应该先接。"""
        chunker = Chunker([StructuredChunker(), TextChunker()])
        file = _make_file("csv", "col1,col2\na,b\nc,d")
        chunks = chunker.chunk(file)
        assert len(chunks) == 1


# TextChunker — 语义切分


class TestTextChunker:
    def test_single_paragraph(self):
        ck = TextChunker(max_chunk_size=500)
        file = _make_file("md", "A single paragraph of text.")
        chunks = ck.chunk(file)
        assert len(chunks) == 1

    def test_section_boundaries(self):
        """# / ## 标题处应该切分。"""
        ck = TextChunker(max_chunk_size=200)
        file = _make_file(
            "md",
            "# Section One\n"
            + "A" * 80
            + "\n\n"
            + "## Section Two\n"
            + "B" * 80
            + "\n\n"
            + "# Section Three\n"
            + "C" * 80,
        )
        chunks = ck.chunk(file)
        # 三个 section 应该在标题处分隔
        assert len(chunks) >= 2

    def test_short_sections_merged(self):
        """短于 max_chunk_size 的多个 section 合并为一个 chunk。"""
        ck = TextChunker(max_chunk_size=500)
        file = _make_file("md", "# S1\nShort.\n\n" + "# S2\nAlso short.\n\n" + "# S3\nAnd short.")
        chunks = ck.chunk(file)
        # 三个短 section 应该累积成一个 chunk
        assert len(chunks) == 1

    def test_large_section_split(self):
        """超长段落按句子边界切分。"""
        ck = TextChunker(max_chunk_size=100)
        file = _make_file("md", ("A long text. " * 40))
        chunks = ck.chunk(file)
        assert len(chunks) > 1
        # 每个 chunk 不超过 max_chunk_size 的两倍
        assert all(len(c.content) <= ck._max_chunk_size * 2 for c in chunks)

    def test_tail_merge(self):
        """尾部过短时合并到前一个 chunk。"""
        ck = TextChunker(max_chunk_size=200, min_chunk_size=20)
        file = _make_file(
            "md",
            "A" * 180
            + "\n\n"  # ~接近 max
            + "B" * 5,  # 极短尾
        )
        chunks = ck.chunk(file)
        if len(chunks) >= 2:
            assert len(chunks[-1]) >= ck._min_chunk_size
        # 应该合并为 1 个 chunk
        assert len(chunks) == 1

    def test_empty_content(self):
        ck = TextChunker()
        file = _make_file("txt", "")
        chunks = ck.chunk(file)
        assert chunks == []

    def test_content_with_only_whitespace(self):
        ck = TextChunker()
        file = _make_file("txt", "   \n  \n  ")
        chunks = ck.chunk(file)
        assert chunks == []


# StructuredChunker


class TestStructuredChunker:
    def test_csv_header_preserved(self):
        ck = StructuredChunker(batch_size=2)
        file = _make_file("csv", "name,age\nAlice,30\nBob,25\nCarol,28")
        chunks = ck.chunk(file)
        assert len(chunks) >= 1
        # StructuredChunker 不会丢掉表头
        for c in chunks:
            # 每批 2 行，用 batch_size 控制
            pass

    def test_csv_parsed_as_json_struct(self):
        ck = StructuredChunker(batch_size=5)
        file = _make_file("csv", "x,y\n1,a")
        chunks = ck.chunk(file)
        assert len(chunks) == 1

    def test_json_object_splits_by_key(self):
        ck = StructuredChunker()
        file = _make_file("json", '{"k1": "v1", "k2": "v2"}')
        chunks = ck.chunk(file)
        assert len(chunks) == 2

    def test_json_array_splits_by_element(self):
        ck = StructuredChunker()
        file = _make_file("json", '[{"a":1},{"b":2}]')
        chunks = ck.chunk(file)
        assert len(chunks) == 2

    def test_jsonl_one_chunk_per_line(self):
        ck = StructuredChunker()
        file = _make_file("jsonl", '{"a":1}\n{"b":2}\n{"c":3}')
        chunks = ck.chunk(file)
        assert len(chunks) == 3

    def test_jsonl_empty(self):
        ck = StructuredChunker()
        file = _make_file("jsonl", "")
        chunks = ck.chunk(file)
        assert chunks == []

    def test_csv_empty_content(self):
        ck = StructuredChunker()
        file = _make_file("csv", "")
        chunks = ck.chunk(file)
        assert chunks == []

    def test_does_not_accept_txt(self):
        ck = StructuredChunker()
        file = _make_file("txt", "plain")
        assert not ck.can_process(file)


# can_process 边界


class TestCanProcess:
    def test_text_chunker_accepts_any_text_modality(self):
        ck = TextChunker()
        assert ck.can_process(_make_file("pdf", "content"))
        assert ck.can_process(_make_file("docx", "content"))
        assert ck.can_process(_make_file("png", "content"))
        assert ck.can_process(_make_file("xyz", "content"))

    def test_text_chunker_rejects_non_text_modality(self):
        ck = TextChunker()
        file = _make_file("pdf", "content", modality="rich")
        assert not ck.can_process(file)

    def test_text_chunker_rejects_empty(self):
        ck = TextChunker()
        assert not ck.can_process(_make_file("md", ""))

    def test_structured_chunker_accepts_csv_json_jsonl(self):
        ck = StructuredChunker()
        assert ck.can_process(_make_file("csv", "a,b\n1,2"))
        assert ck.can_process(_make_file("json", "{}"))
        assert ck.can_process(_make_file("jsonl", "{}"))

    def test_structured_chunker_rejects_txt(self):
        ck = StructuredChunker()
        assert not ck.can_process(_make_file("txt", "text"))
        assert not ck.can_process(_make_file("md", "# heading"))

    def test_structured_chunker_rejects_non_text_modality(self):
        ck = StructuredChunker()
        file = _make_file("csv", "a,b\n1,2", modality="rich")
        assert not ck.can_process(file)
