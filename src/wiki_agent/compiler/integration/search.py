"""Search 阶段——L1 索引初筛（LLM 从 index 选候选页面，模式间零差异）。"""

from __future__ import annotations

from pathlib import Path

from openai.types.shared_params import ResponseFormatJSONObject

from wiki_agent.compiler.integration.checks import check_paths_json
from wiki_agent.compiler.integration.common import load_valid_slugs
from wiki_agent.compiler.integration.parse import _parse_search_result
from wiki_agent.compiler.models import _NO_THINKING, ExtractResult, SearchResult
from wiki_agent.conversation import Message
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.llm.llm import LLMClient
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import emit_event, get_logger

logger = get_logger("STAGES")

_SEARCH_TOKENS = 2_048
_SEARCH_PAGE_DIRS = {"concepts", "entities", "topics"}
# API 级 JSON 模式——输出必为合法 JSON 对象，格式噪声重试（fence/前言）归零；
# check/parse 仍宽容顶层数组，防端点静默忽略该参数
_JSON_MODE: ResponseFormatJSONObject = {"type": "json_object"}

__all__ = ["Searcher", "_filter_search_paths", "load_valid_slugs"]


def _filter_search_paths(
    paths: list[str],
    wiki_dir: str | Path,
) -> tuple[list[str], list[str]]:
    """过滤 Search 候选中的非法目录、路径穿越和幽灵页面。

    这是 LLM 输出解析后的运行时后处理：格式合法不代表候选可供
    Analyzer 读取。无效候选被丢弃并保留在调用方日志中；合法候选为空
    仍是正常的“没有相关已有页面”，不升级为阶段失败。
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
            and parts[0] in _SEARCH_PAGE_DIRS
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
    """search 阶段: LLM 从 index 选候选页面。模式间零差异。"""

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def search(self, extract: ExtractResult, index_content: str) -> SearchResult:
        """执行 search——LLM 从 index 选出候选页面。

        Args:
            extract: 源文档抽取结果。
            index_content: index.md 全文。

        Returns:
            候选页面列表的 SearchResult。

        Raises:
            IngestError: 校验穷尽后仍失败（不许静默降级 0 候选）。
        """
        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=self._prompts.search_system()),
                Message(role="user", content=self._prompts.search_user(extract, index_content)),
            ],
            max_tokens=_SEARCH_TOKENS,
            check=check_paths_json,
            extra_body=_NO_THINKING,
            max_attempts=2,
            response_format=_JSON_MODE,
        )
        # 校验穷尽后仍失败 → 显式 raise，不许静默降级成"0 候选"。
        if not response.check_ok:
            raise IngestError(
                IngestStage.SEARCH,
                f"search 输出校验失败（重试后仍失败）: {response.check_reason}",
                source=extract.source_identity,
                raw=response.content,
                error_code="output_validation",
                error_class="transient",
                retry_policy="auto_retry",
            )
        paths = _parse_search_result(response.content)
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
