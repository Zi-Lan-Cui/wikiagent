"""重编译驱动：把源文件逐篇编译进全新 wiki，当场以 CompileHook 落 run-evidence。

产物是 source-eval 直接可评的语料目录（baselines/seeded/run-evidence 布局，
落 dev 或 test）。证据由管线现场记录：index 快照与编译时树取自 batch_start/plan
窗口，update 前像在 pre_execute 窗口读（execute 即覆盖），落 update-baselines/ 真记录。

编译逐篇串行——页与页的先后关系本身就是增量编译要考察的对象，
顺序取文件名字典序并全程不变。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from wiki_agent.compiler.models import AnalysisResult, ExtractResult, IntegrationPlan, SearchResult
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.log import begin_trace, setup_event_log
from wiki_agent.materials.loader import FileModality, RawFileProperties

_INDEX_ENTRY = re.compile(
    r"^- \[\[(?P<slug>[^\]]+)\]\] — \[(?P<type>[^\]]*)\] \S+ — (?P<rest>.+)$", re.MULTILINE
)


def parse_index_lines(index_content: str) -> list[dict[str, str]]:
    """index.md 行 → 编译时树 existing_pages 条目（path/type/title/summary/goal）。

    行由 wiki.pages.index_line 构造：`- [[slug]] — [type] slug.md — title — summary — 使命: goal`。
    summary 缺失时该行没有对应段；title 之后按首个分隔切，剩余归 summary（使命段先摘出）。
    """
    pages: list[dict[str, str]] = []
    for match in _INDEX_ENTRY.finditer(index_content):
        rest = match.group("rest")
        goal = ""
        if " — 使命: " in rest:
            rest, goal = rest.split(" — 使命: ", 1)
        title, _, summary = rest.partition(" — ")
        pages.append(
            {
                "path": f"{match.group('slug')}.md",
                "type": match.group("type"),
                "title": title,
                "summary": summary,
                "goal": goal,
            }
        )
    return pages


def _page_target_dicts(plan: IntegrationPlan) -> list[dict[str, Any]]:
    return [
        {
            "wiki_path": target.wiki_path,
            "title": target.title,
            "disposition": target.disposition,
            "page_type": target.page_type,
            "reason": target.reason,
            "references": target.references,
        }
        for target in plan.page_targets
    ]


def _analysis_dicts(analysis: AnalysisResult) -> dict[str, Any]:
    from dataclasses import asdict

    return {
        "entities": analysis.entities,
        "concepts": analysis.concepts,
        "relationships": [asdict(rel) for rel in analysis.relationships],
    }


class EvidenceSink:
    """CompileHook 实现：把阶段事件写成 run-evidence 布局文件。

    写入即完成，不缓存到批末（batch_end 除外——页面与 targets 要等 execute
    落盘才能拷贝）。钩子异常由机制层隔离，此处不吞。
    """

    def __init__(self, evidence_dir: Path):
        self._root = evidence_dir
        self._index_content = ""

    def on_compile_event(self, event: Any) -> None:
        handler = {
            "batch_start": self._on_batch_start,
            "extract": self._on_extract,
            "search": self._on_search,
            "analyze": self._on_analyze,
            "plan": self._on_plan,
            "pre_execute": self._on_pre_execute,
            "batch_end": self._on_batch_end,
        }.get(event.stage)
        if handler is not None:
            handler(event)

    def _artifacts(self, source: str) -> Path:
        directory = self._root / "artifacts" / source
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _archived(wiki_path: str) -> str:
        return wiki_path.replace("/", "_")

    @staticmethod
    def _strip_wiki_prefix(wiki_path: str) -> str:
        path = wiki_path.strip()
        while path.startswith("wiki/"):
            path = path[len("wiki/") :]
        return path

    def _copy_with_stamp(self, wiki_file: Path, target: Path) -> None:
        """存档页带 `# generated at:` 标记行，与既有 run-evidence 存档一致。"""
        content = wiki_file.read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# generated at: {datetime.now().isoformat()}\n\n{content}", encoding="utf-8")

    def _on_batch_start(self, event: Any) -> None:
        self._index_content = event.index_content or ""
        stem = event.source.removesuffix(".md")
        self._write_json(
            self._root / "index-snapshots" / f"{stem}.index.json",
            {"source": event.source, "index_content": self._index_content},
        )

    def _on_extract(self, event: Any) -> None:
        extract: ExtractResult = event.outcome.extract
        (self._artifacts(event.source) / "extract.txt").write_text(
            extract.document_summary, encoding="utf-8"
        )

    def _on_search(self, event: Any) -> None:
        search: SearchResult = event.outcome.search
        self._write_json(
            self._artifacts(event.source) / "search.json",
            {"source": event.source, "rel_paths": search.rel_paths, "raw": search.raw},
        )

    def _on_analyze(self, event: Any) -> None:
        analysis: AnalysisResult = event.outcome.analysis
        self._write_json(
            self._artifacts(event.source) / "analyze.json",
            {
                "source": event.source,
                "analysis_text": analysis.analysis_text,
                "raw": analysis.raw_analysis,
                **_analysis_dicts(analysis),
            },
        )

    def _on_plan(self, event: Any) -> None:
        plan: IntegrationPlan = event.outcome.plan
        source = event.source
        self._write_json(
            self._artifacts(source) / "plan.json",
            {
                "source": source,
                "page_targets": _page_target_dicts(plan),
                "raw": plan.raw,
            },
        )
        stem = source.removesuffix(".md")
        self._write_json(
            self._root / "compile-time-trees" / f"{stem}.tree.json",
            {
                "source": source,
                "schema": "compile-time-tree-v1",
                "existing_pages": parse_index_lines(self._index_content),
                "planned_targets": [
                    {
                        "path": target["wiki_path"],
                        "type": target["page_type"],
                        "title": target["title"],
                        "summary": "",
                        "goal": target["reason"],
                    }
                    for target in _page_target_dicts(plan)
                ],
            },
        )

    def _on_pre_execute(self, event: Any) -> None:
        """update 目标的前像只有这个窗口是「编译时所见」——之后即被覆盖。"""
        plan: IntegrationPlan = event.outcome.plan
        for target in plan.page_targets:
            if target.disposition != "update":
                continue
            live = event.wiki_dir / self._strip_wiki_prefix(target.wiki_path)
            if not live.is_file():
                print(
                    f"{event.source}: update 目标 {target.wiki_path} 现场无页，前像缺失",
                    file=sys.stderr,
                )
                continue
            self._copy_with_stamp(
                live, self._artifacts(event.source) / "update-baselines" / self._archived(target.wiki_path)
            )

    def _on_batch_end(self, event: Any) -> None:
        plan: IntegrationPlan | None = event.outcome.plan
        if plan is not None:
            # pages 只采本轮真正写出的页；失败批 pages_written 为空，磁盘上残留的
            # update 旧页不作本轮产物，否则评测会把旧内容当新内容打分
            written = set(event.outcome.pages_written)
            for target in _page_target_dicts(plan):
                archived = self._archived(target["wiki_path"])
                self._write_json(
                    self._artifacts(event.source) / "targets" / (archived.removesuffix(".md") + ".json"),
                    {"source": event.source, "target": target},
                )
                if target["wiki_path"] not in written:
                    continue
                live = event.wiki_dir / self._strip_wiki_prefix(target["wiki_path"])
                if live.is_file():
                    self._copy_with_stamp(live, self._artifacts(event.source) / "pages" / archived)
        if event.error is not None:
            self._write_json(
                self._artifacts(event.source) / "error.json",
                {"source": event.source, "error": str(event.error), "type": type(event.error).__name__},
            )


async def _compile_all(sources: list[Path], corpus: Path, wiki: Path, resume: bool) -> list[str]:
    config = load_config(project_root=Path.cwd())
    # VLM 只在 api_key 与 model_id 都配齐时构造：源文件图片极少，缺配不阻断编译
    vlm = create_vlm(config.vlm, config.retry) if config.vlm.api_key.strip() and config.vlm.model_id.strip() else None
    evidence = corpus / "baselines" / "seeded" / "run-evidence"
    pipeline = CompilePipeline(
        llm=create_llm(config.llm),
        vlm=vlm,
        wiki_dir=wiki,
        source_records_dir=wiki.parent / "source-records",
        compile_config=config.compile,
        hooks=[EvidenceSink(evidence)],
    )
    setup_event_log(evidence / "events.jsonl")
    begin_trace(f"recompile-{corpus.name}")
    failures: list[str] = []
    try:
        total = len(sources)
        for index, source_file in enumerate(sources, start=1):
            source_dir = evidence / "artifacts" / source_file.name
            if resume and source_dir.is_dir():
                if (source_dir / "error.json").is_file():
                    shutil.rmtree(source_dir)  # 上次失败的证据含被误拷的旧页，清掉重编
                    print(f"[{index}/{total}] {source_file.name} 上次失败，清证据重编")
                else:
                    print(f"[{index}/{total}] {source_file.name} 已有证据，跳过")
                    continue
            raw = RawFileProperties(
                name=source_file.name,
                ext="md",
                path=source_file,
                modality=FileModality.TEXT,
                content=source_file.read_text(encoding="utf-8", errors="replace"),
                size_bytes=source_file.stat().st_size,
                encoding="utf-8",
            )
            try:
                outcome = await pipeline.ingest_one(raw)
            except Exception as exc:
                failures.append(source_file.name)
                print(f"[{index}/{total}] {source_file.name} 失败: {exc}", file=sys.stderr)
                continue
            status = "无操作" if outcome.noop else f"{len(outcome.pages_written)} 页"
            print(f"[{index}/{total}] {source_file.name} → {status}")
    finally:
        setup_event_log(None)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="重编译语料并落 run-evidence")
    parser.add_argument("--sources-dir", type=Path, required=True, help="原始 .md 文件目录")
    parser.add_argument("--corpus", type=Path, required=True, help="产出的语料目录（可被 source-eval --corpus 评）")
    parser.add_argument("--wiki", type=Path, default=None, help="缺省 workspace/recompile/<语料名>/wiki")
    parser.add_argument("--inventory", type=Path, default=None, help="出处清单，复制为语料目录 PROVENANCE.tsv")
    parser.add_argument("--resume", action="store_true", help="保留既有 wiki 与证据，跳过已编译 source")
    parser.add_argument("--only", action="append", default=None, help="只编译名字含此子串的 source，可重复")
    parser.add_argument("--limit", type=int, default=None, help="最多编译 N 篇")
    args = parser.parse_args()

    source_files = sorted(args.sources_dir.glob("*.md"))
    if args.only:
        source_files = [f for f in source_files if any(needle in f.name for needle in args.only)]
    if args.limit:
        source_files = source_files[: args.limit]
    if not source_files:
        print("sources-dir 没有匹配文件", file=sys.stderr)
        return 1

    seeded = args.corpus / "baselines" / "seeded"
    evidence = seeded / "run-evidence"
    if evidence.exists() and any(evidence.iterdir()) and not args.resume:
        print(f"语料已有证据目录，用 --resume 续跑或换目录: {evidence}", file=sys.stderr)
        return 1
    seeded_source = seeded / "source"
    seeded_source.mkdir(parents=True, exist_ok=True)
    for directory in ("index-snapshots", "compile-time-trees"):
        (evidence / directory).mkdir(parents=True, exist_ok=True)
    for source_file in source_files:
        target = seeded_source / source_file.name
        if not target.exists():
            target.write_bytes(source_file.read_bytes())
    if args.inventory is not None and args.inventory.is_file():
        (args.corpus / "PROVENANCE.tsv").write_text(
            args.inventory.read_text(encoding="utf-8"), encoding="utf-8"
        )

    wiki = args.wiki or Path.cwd() / "workspace" / "recompile" / args.corpus.name / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "index.md").touch(exist_ok=True)

    failures = asyncio.run(_compile_all(source_files, args.corpus, wiki, args.resume))
    print(f"完成 {len(source_files) - len(failures)}/{len(source_files)}；失败: {failures or '无'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
