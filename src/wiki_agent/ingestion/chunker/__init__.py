from wiki_agent.ingestion.chunker.base import BaseChunker, ChunkedFileProperties
from wiki_agent.ingestion.chunker.dispatcher import Chunker
from wiki_agent.ingestion.chunker.structured_chunker import StructuredChunker

# 兼容旧 import 路径
from wiki_agent.ingestion.chunker.text_chunker import TextChunker  # noqa: F811
