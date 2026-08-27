"""Execute 阶段——并行生成/更新页面 + 失败隔离 + 死链兜底。"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

from wiki_agent.compiler.integration.common import extract_slugs_from_index, load_valid_slugs
from wiki_agent.compiler.integration.parse import _normalize_wiki_path
from wiki_agent.compiler.integration.plan import filter_plan_refs
from wiki_agent.compiler.models import (
    _NO_THINKING,
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
)
from wiki_agent.compiler.wiki.frontmatter import split_frontmatter
from wiki_agent.compiler.wiki.normalize import extract_related, fix_wikilinks, normalize_page
from wiki_agent.compiler.wiki.rules import _check_page_output
from wiki_agent.errors import IngestError, IngestStage
from wiki_agent.llm.llm import LLMClient
from wiki_agent.llm.retry import async_invoke_with_retry
from wiki_agent.log import emit_event, get_logger
from wiki_agent.message import Message

logger = get_logger("STAGES")

_UPDATE_TOKENS = 8_000
# 页面生成总尝试次数（retry 层语义: 总尝试，原 1 = 零重试）
_PAGE_GEN_RETRIES = 2

__all__ = ["Executor", "extract_slugs_from_index", "filter_plan_refs"]


class Executor:
    """execute 阶段: 并行生成/更新页面 + 失败隔离 + 死链兜底。"""

    def __init__(self, llm: LLMClient, wiki_dir: str | Path, prompts):
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._prompts = prompts

    async def _read_page(self, wiki_path: str) -> str:
        """读 wiki 页面全文。

        Args:
            wiki_path: 页面相对路径。

        Returns:
            页面内容；页面不存在返回空串。
        """
        try:
            return (self._wiki_dir / wiki_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    async def _write_page(self, wiki_path: str, content: str) -> None:
        """落盘——内容处理链在 execute 内完成，这里只写文件。

        Args:
            wiki_path: 页面相对路径（会做规范化）。
            content: 页面完整内容。
        """
        wiki_path = _normalize_wiki_path(wiki_path)
        full = self._wiki_dir / wiki_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    async def _generate_page(
        self,
        target: PageTarget,
        existing: str,
        extract: ExtractResult,
    ) -> str:
        """按 disposition 生成页面——new 从零生成 / update 合并已有页。

        校验不过 → 重试；穷尽后仍不过 → raise（质量闸门，不许静默落盘）。

        Args:
            target: 页面目标（disposition/path/references）。
            existing: 已有页面内容（update 时作为合并基底）。
            extract: 源文档抽取结果。

        Returns:
            生成的页面原始内容。

        Raises:
            IngestError: 重试穷尽后校验仍失败。
        """
        if not existing:
            system_prompt = self._prompts.new_page_system()
            user_prompt = self._prompts.new_page_user(target, extract)
        else:
            system_prompt = self._prompts.update_system()
            user_prompt = self._prompts.update_user(target, existing, extract)
        response = await async_invoke_with_retry(
            self._llm,
            [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_prompt),
            ],
            max_tokens=_UPDATE_TOKENS,
            check=_check_page_output,
            extra_body=_NO_THINKING,
            max_retries=_PAGE_GEN_RETRIES,
        )
        if not response.check_ok:
            raise IngestError(
                IngestStage.EXECUTE,
                f"页面生成校验失败（重试后仍失败）: {response.check_reason}",
                source=target.wiki_path,
                raw=response.content,
                error_code="output_validation",
                error_class="transient",
                retry_policy="auto_retry",
            )
        return response.content

    async def execute(
        self,
        plan: IntegrationPlan,
        extract: ExtractResult,
    ) -> list[PageTarget]:
        """执行 plan——并行处理每个 target，失败隔离 + 死链兜底。

        Args:
            plan: 集成计划。
            extract: 源文档抽取结果。

        Returns:
            成功落盘的 target 列表（失败的 target 返回 None 被过滤，
            不会混入成功结果）。
        """
        if not plan.page_targets:
            return []

        today = date.today().isoformat()
        source = extract.source_identity

        # 组装有效 slug 集合：已有 + 本次要创建的新页
        valid_slugs = load_valid_slugs(self._wiki_dir)
        for t in plan.page_targets:
            if t.disposition == Disposition.NEW:
                valid_slugs.add(t.wiki_path.replace(".md", ""))

        failed_paths: set[str] = set()
        failed_details: list[dict[str, str]] = []

        async def _execute_target(target: PageTarget) -> PageTarget | None:
            try:
                existing = await self._read_page(target.wiki_path)
                existing_fm = split_frontmatter(existing)[0] if existing else None
                raw_content = await self._generate_page(target, existing, extract)

                content, issues = normalize_page(
                    raw_content,
                    path=target.wiki_path,
                    valid_slugs=valid_slugs,
                    source_identity=source,
                    today=today,
                    existing=existing_fm,
                )
                for issue in issues:
                    if issue.level == "error":
                        raise ValueError(f"页面质量不合格: {issue}")

                await self._write_page(target.wiki_path, content)
                logger.info("  ✓ %s", target.wiki_path)
                return target
            except Exception as exc:
                failed_paths.add(_normalize_wiki_path(target.wiki_path))
                failed_details.append(
                    {
                        "path": target.wiki_path,
                        "error": str(exc),
                        "raw": getattr(exc, "raw", ""),
                    }
                )
                logger.error("  ✗ %s 生成失败: %s", target.wiki_path, str(exc)[:200])
                emit_event("page_generation", path=target.wiki_path, status="error", error=str(exc))
                # 失败返回 None——不能 return target（update 目标旧页仍存在，
                # 会骗过调用方 exists() 过滤，把失败算成成功）
                return None

        results = await asyncio.gather(*[_execute_target(t) for t in plan.page_targets])
        results = [r for r in results if r is not None]

        # 兜底死链清理: 仅当有页面生成失败时触发
        if failed_paths:
            actual_slugs = load_valid_slugs(self._wiki_dir)
            actual_slugs.update(
                t.wiki_path.replace(".md", "")
                for t in plan.page_targets
                if (self._wiki_dir / _normalize_wiki_path(t.wiki_path)).exists()
            )
            for t in plan.page_targets:
                full = self._wiki_dir / _normalize_wiki_path(t.wiki_path)
                if not full.exists():
                    continue
                content = full.read_text(encoding="utf-8")
                fixed = fix_wikilinks(content, valid_slugs=actual_slugs)
                fixed = extract_related(fixed, valid_slugs=actual_slugs)
                if fixed != content:
                    full.write_text(fixed, encoding="utf-8")
                    logger.info("  死链清理: %s 已更新（引用了生成失败的页面）", t.wiki_path)

        if failed_details:
            # 页面级失败升级为 source 级失败：边界统一入队并从 source 起点重试。
            raise IngestError(
                IngestStage.EXECUTE,
                f"{len(failed_details)} 个页面生成失败",
                source=source,
                raw=json.dumps(failed_details, ensure_ascii=False),
                error_code="page_generation_failed",
                error_class="transient",
                retry_policy="auto_retry",
            )

        return results
