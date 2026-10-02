"""Search 阶段：LLM 从 index 初筛候选页面，各模式共用同一逻辑。"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.compiler.checked_call import invoke_checked
from wiki_agent.compiler.integration.checks import check_paths_json
from wiki_agent.compiler.integration.common import load_valid_slugs
from wiki_agent.compiler.integration.parse import parse_search_result
from wiki_agent.compiler.models import JSON_MODE, NO_THINKING, ExtractResult, SearchResult
from wiki_agent.conversation import Message
from wiki_agent.errors import IngestStage
from wiki_agent.llm.llm import LLMClient
from wiki_agent.log import emit_event, get_logger
from wiki_agent.wiki.pages import CONTENT_DIRS

logger = get_logger("STAGES")

_SEARCH_TOKENS = 2_048

__all__ = ["Searcher", "load_valid_slugs"]


def _filter_search_paths(
    paths: list[str],
    wiki_dir: str | Path,
) -> tuple[list[str], list[str]]:
    """过滤 Search 候选中的非法目录、路径穿越和指向不存在文件的页面。

    LLM 输出解析后的运行时检查：路径格式合法不代表文件存在、可供
    下游读取。无效候选丢弃并记入日志；合法候选为空只是说明没有相关
    已有页面，不算阶段失败。
    """
    root = Path(wiki_dir).resolve()
    valid: list[str] = []
    invalid: list[str] = []
    for raw in paths:
        path = Path(raw)
        parts = path.parts
        candidate = (root / path).resolve()
        safe = (
            not path.is_absolute()
            and len(parts) >= 2
            and parts[0] in CONTENT_DIRS
            and path.suffix == ".md"
            and ".." not in parts
            and root in candidate.parents
            and candidate.is_file()
        )
        if safe:
            valid.append(path.as_posix())
        else:
            invalid.append(raw)
    return valid, invalid


class Searcher:
    """search 阶段：LLM 从 index 选候选页面。"""

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def search(self, extract: ExtractResult, index_content: str) -> SearchResult:
        """LLM 从 index 选出候选页面。

        Args:
            extract: 源文档抽取结果。
            index_content: index.md 全文。

        Returns:
            候选页面列表的 SearchResult。

        Raises:
            IngestError: 重试后校验仍失败；不静默返回 0 候选。
        """
        response = await invoke_checked(
            self._llm,
            stage=IngestStage.SEARCH,
            action="search",
            source=extract.source_identity,
            messages=[
                Message(role="system", content=self._prompts.search_system()),
                Message(role="user", content=self._prompts.search_user(extract, index_content)),
            ],
            max_tokens=_SEARCH_TOKENS,
            check=check_paths_json,
            extra_body=NO_THINKING,
            max_attempts=2,
            response_format=JSON_MODE,
        )
        paths = parse_search_result(response.content)
        paths, invalid_paths = _filter_search_paths(paths, self._wiki_dir)
        if invalid_paths:
            logger.warning(
                "  search 过滤 %d 个非法候选: %s",
                len(invalid_paths),
                ", ".join(invalid_paths[:6]),
            )
            emit_event(
                "search_candidates_filtered",
                source=extract.source_identity,
                invalid_paths=invalid_paths,
                kept_count=len(paths),
            )
        logger.info("  search: %d 个候选页面", len(paths))
        return SearchResult(rel_paths=paths, raw=response.content)
