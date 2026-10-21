"""按 source 生成评测记录：确定性校验 + 分档评分 + 聚合落盘。

一次运行对一篇 source 产出一份 SourceEvalRecord
（runs/<run-id>/source-eval-<source>.json），外加横向 _summary.json。

信息面对齐是硬要求：judge 拿到的输入文件按 rubric 各阶段的
visible_inputs_rule 组装与被评程序编译时可见的一致；gold 永不进 prompt。
未评阶段（missing_artifact）与故障阶段（judge_failed）不进总分分母，
但必须留在记录里——汇报「这篇几分」必须同时说清哪些阶段缺席。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from evals.commands.reference_corpus import _sha256
from evals.core.deterministic import run_checks
from evals.core.judge_client import resolve_judge_llm
from evals.core.reference_models import (
    GraderDefinition,
    SourceEvalRecord,
    StageEvalRecord,
    load_grader,
)
from evals.core.scoring import evaluate_gate, source_score, stage_score
from evals.judges.rubric_judge import judge_rubric
from wiki_agent.config import load_config
from wiki_agent.llm.factory import create_llm
from wiki_agent.log import begin_trace, emit_event, setup_event_log
from wiki_agent.wiki.frontmatter import split_frontmatter

_CORPUS_ROOT = Path(__file__).resolve().parents[1] / "corpora" / "dev"
_GRADER_PATH = Path(__file__).resolve().parents[1] / "rubrics" / "stage-rubrics-v1.json"
_EVIDENCE = "baselines/seeded/run-evidence"
# 按真实管线序排列：convert→chunk→extract→search→analyze→plan→execute→index
_STAGES = ("extract", "search", "analyze", "plan", "execute")
_INDEX_LINE = re.compile(r"^- \[\[.+?\]\] — \[", re.MULTILINE)


def available_sources(corpus_root: Path) -> list[str]:
    artifacts = corpus_root / _EVIDENCE / "artifacts"
    return sorted(p.name for p in artifacts.iterdir() if p.is_dir())


def _artifact_files(corpus_root: Path, source: str) -> dict[str, list[Path]]:
    """各阶段被评产物路径（只列真实存在的；空列表 = 该阶段产物缺失）。"""
    artifacts = corpus_root / _EVIDENCE / "artifacts" / source
    pages_dir = artifacts / "pages"
    candidates = {
        "extract": [artifacts / "extract.txt"],
        "analyze": [artifacts / "analyze.json"],
        "plan": [artifacts / "plan.json"],
        "search": [artifacts / "search.json"],
        "execute": sorted(pages_dir.glob("*.md")) if pages_dir.is_dir() else [],
    }
    return {stage: [p for p in paths if p.is_file()] for stage, paths in candidates.items()}


def _input_files(corpus_root: Path, source: str) -> dict[str, list[Path]]:
    """各阶段编译时信息面——与 rubric.visible_inputs_rule 一一对应。"""
    stem = source.removesuffix(".md")
    artifacts = corpus_root / _EVIDENCE / "artifacts" / source
    extract = artifacts / "extract.txt"
    analyze = artifacts / "analyze.json"
    plan = artifacts / "plan.json"
    index_snapshot = corpus_root / _EVIDENCE / "index-snapshots" / f"{stem}.index.json"
    tree = corpus_root / _EVIDENCE / "compile-time-trees" / f"{stem}.tree.json"
    original = corpus_root / "baselines" / "seeded" / "source" / source
    targets_dir = artifacts / "targets"
    target_files = sorted(targets_dir.glob("*.json")) if targets_dir.is_dir() else []
    candidates = {
        "extract": [original],
        "analyze": [extract, index_snapshot],
        "plan": [extract, analyze, index_snapshot, tree],
        "search": [extract, index_snapshot],
        "execute": [extract, analyze, plan, *target_files, *_update_baselines(corpus_root, source)],
    }
    return {stage: [p for p in paths if p.is_file()] for stage, paths in candidates.items()}


def _blocks(corpus_root: Path, files: list[Path]) -> list[tuple[str, str]]:
    return [(str(p.relative_to(corpus_root)), p.read_text(encoding="utf-8")) for p in files]


def _page_frontmatter(content: str) -> dict:
    """从页面文本取 frontmatter——run-evidence 存档页在 frontmatter 前带
    `# generated at:` 标记行，直传 split_frontmatter 会解析为空 dict。"""
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "---":
            return split_frontmatter("\n".join(lines[index:]))[0]
    return {}


def _det_artifact(stage: str, files: list[Path]) -> Any:
    """确定性校验的产物形态：文本 str / JSON dict / {"pages": [frontmatter dict]}。"""
    if stage == "extract":
        return files[0].read_text(encoding="utf-8")
    if stage == "execute":
        return {"pages": [_page_frontmatter(p.read_text(encoding="utf-8")) for p in files]}
    return json.loads(files[0].read_text(encoding="utf-8"))


def _artifact_digest(files: list[Path]) -> str:
    if len(files) == 1:
        return _sha256(files[0])
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@cache
def _index_page_count(corpus_root: Path, source: str) -> int:
    snapshot = corpus_root / _EVIDENCE / "index-snapshots" / f"{source.removesuffix('.md')}.index.json"
    content = json.loads(snapshot.read_text(encoding="utf-8"))["index_content"]
    return len(_INDEX_LINE.findall(content))


@cache
def _updated_paths(corpus_root: Path, source: str) -> frozenset[str]:
    plan_path = corpus_root / _EVIDENCE / "artifacts" / source / "plan.json"
    if not plan_path.is_file():
        return frozenset()
    targets = json.loads(plan_path.read_text(encoding="utf-8")).get("page_targets", [])
    return frozenset(
        str(t["wiki_path"]) for t in targets if t.get("disposition") == "update" and t.get("wiki_path")
    )


def _update_baselines(corpus_root: Path, source: str) -> list[Path]:
    """update 目标页在编译时刻的基线版本——execute 当时能看到旧页全文，
    评测侧必须同样拿到，否则 X1 会把保留基线误判成无据添加。

    优先取真记录：重编译驱动（evals run.py recompile）在 pre_execute 窗口
    读旧页存入 artifacts/<source>/update-baselines/，该文件即编译时现场。
    取不到记录时退化为重建：
    候选写入者取页数最接近本篇的存档版本；页数非严格单调（同页数并列=零新增
    编译，先后未知），闭区间 [writer, 本篇] 内任何动过该页的 source 都令先后
    存疑，一律 fail-closed 跳过——给错基线（旧版冒充当时状态）比不给更坏。
    """
    plan_path = corpus_root / _EVIDENCE / "artifacts" / source / "plan.json"
    if not plan_path.is_file():
        return []
    targets = json.loads(plan_path.read_text(encoding="utf-8")).get("page_targets", [])
    updates = [str(t["wiki_path"]) for t in targets if t.get("disposition") == "update" and t.get("wiki_path")]
    if not updates:
        return []
    sources = available_sources(corpus_root)
    current_n = _index_page_count(corpus_root, source)
    found: list[Path] = []
    for wiki_path in updates:
        archived_name = wiki_path.replace("/", "_")
        recorded = corpus_root / _EVIDENCE / "artifacts" / source / "update-baselines" / archived_name
        if recorded.is_file():
            found.append(recorded)
            continue
        best: tuple[int, str, Path] | None = None
        for writer in sources:
            if writer == source:
                continue
            candidate = corpus_root / _EVIDENCE / "artifacts" / writer / "pages" / archived_name
            if candidate.is_file():
                n = _index_page_count(corpus_root, writer)
                if n < current_n and (best is None or n > best[0]):
                    best = (n, writer, candidate)
        if best is None:
            print(f"{source}: update 目标 {wiki_path} 无更早存档，基线不给（fail-closed）", file=sys.stderr)
            continue
        n_writer, writer, path = best
        ambiguous = [
            mid
            for mid in sources
            if mid not in (source, writer)
            and wiki_path in _updated_paths(corpus_root, mid)
            and n_writer <= _index_page_count(corpus_root, mid) <= current_n
        ]
        if ambiguous:
            print(
                f"{source}: {wiki_path} 在 {writer} 与本篇之间还有写入者 {ambiguous} 且先后不可证，"
                "基线不给（fail-closed）",
                file=sys.stderr,
            )
            continue
        found.append(path)
    return found


async def eval_source(
    client: Any,
    corpus_root: Path,
    source: str,
    grader: GraderDefinition,
    run_id: str,
) -> SourceEvalRecord:
    """评一个 source，返回记录（不落盘）。"""
    artifact_files = _artifact_files(corpus_root, source)
    input_files = _input_files(corpus_root, source)
    stage_records: list[StageEvalRecord] = []
    for stage in _STAGES:
        rubric = grader.stage_rubrics[stage]
        files = artifact_files[stage]
        if not files:
            stage_records.append(
                StageEvalRecord(
                    stage=stage,
                    artifact_path="",
                    status="missing_artifact",
                    deterministic=[],
                    dimensions=[],
                )
            )
            continue
        context: dict[str, Any] = {}
        if stage == "search":
            context["index_pages"] = _index_page_count(corpus_root, source)
        checks = run_checks(stage, _det_artifact(stage, files), grader.deterministic_checks.get(stage, []), context)
        gate_failed = any(not result.passed for result in checks if result.gate)
        try:
            judged = await judge_rubric(
                client, stage, _blocks(corpus_root, input_files[stage]), _blocks(corpus_root, files), rubric
            )
            dimensions = judged["dimensions"]
            status = "scored"
        except Exception as exc:
            print(f"{source}/{stage} 分档评分失败: {exc}", file=sys.stderr)
            emit_event("rubric_judge_failed", source=source, stage=stage, error=str(exc))
            dimensions = []
            status = "judge_failed"
        stage_score_value = None if gate_failed else stage_score(dimensions, rubric)
        emit_event(
            "stage_judged",
            source=source,
            stage=stage,
            status=status,
            stage_score=stage_score_value,
            deterministic_failed=[r.rule_id for r in checks if r.gate and not r.passed],
        )
        stage_records.append(
            StageEvalRecord(
                stage=stage,
                artifact_path=str(files[0].relative_to(corpus_root)) if len(files) == 1 else str(files[0].parent.relative_to(corpus_root)),
                artifact_sha256=_artifact_digest(files),
                status=status,
                deterministic=checks,
                dimensions=dimensions,
                stage_score=stage_score_value,
            )
        )
    gate_config = grader.aggregation["gate"]
    total = source_score(
        {record.stage: record.stage_score for record in stage_records},
        grader.aggregation["stage_weights"],
    )
    record = SourceEvalRecord(
        schema_version="wiki-agent-source-eval-v1",
        run_id=run_id,
        source=source,
        rubric_version=grader.schema_version,
        judge_model=str(getattr(client, "model_id", "")),
        stages=stage_records,
        source_score=total,
        gate={},
        verdict="incomplete",
    )
    record.gate = evaluate_gate(
        record,
        threshold=float(gate_config["source_threshold"]),
        min_dim=int(gate_config["min_dimension_score"]),
    )
    # 量表未经抽样人标校准时 gate 结论不生效：verdict 保持 incomplete，passed 仅供观察
    if gate_config.get("calibrated"):
        record.verdict = "pass" if record.gate["passed"] else "fail"
    return record


def _prepare_run_dir(out_dir: Path, run_id: str) -> Path:
    run_dir = out_dir / run_id
    if run_dir.exists():
        raise FileExistsError(f"run 目录已存在，拒绝覆盖: {run_dir}")
    run_dir.mkdir(parents=True)
    return run_dir


def write_record(record: SourceEvalRecord, run_dir: Path) -> Path:
    path = run_dir / f"source-eval-{record.source}.json"
    path.write_text(
        json.dumps(record.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _make_run_id() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    sha = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return f"{datetime.now():%Y%m%d-%H%M%S}-{sha or 'nogit'}"


def main() -> int:
    parser = argparse.ArgumentParser(description="按 source 生成评测记录")
    parser.add_argument("--source", action="append", default=None, help="可重复；缺省为全部 source")
    parser.add_argument("--ci", action="store_true", help="任一 source 判 fail 或确定性 gate 不过 → 退出码 1")
    parser.add_argument("--out", type=Path, default=None, help="缺省落在所评语料目录的 runs/ 下")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=_CORPUS_ROOT,
        help="按 run-evidence 布局组织的语料目录；candidate 重编译产物同布局即可评",
    )
    args = parser.parse_args()
    out_dir = args.out or args.corpus / "runs"

    grader = load_grader(_GRADER_PATH)
    config = load_config(project_root=Path.cwd())
    client = create_llm(resolve_judge_llm(config))
    run_id = _make_run_id()
    run_dir = _prepare_run_dir(out_dir, run_id)
    targets = args.source or available_sources(args.corpus)

    # 日志接线：本 run 的结构化事件与产物同目录。llm_attempt 等 LLM 层 span
    # 事件经全局 EventLog 自动流入，无需本模块重复记录。
    setup_event_log(run_dir / "events.jsonl")
    begin_trace(f"source-eval-{run_id}")
    emit_event(
        "source_eval_started",
        run_id=run_id,
        judge_model=str(getattr(client, "model_id", "")),
        rubric_version=grader.schema_version,
        gate_calibrated=bool(grader.aggregation["gate"].get("calibrated")),
        sources=len(targets),
    )

    rows: list[dict[str, Any]] = []
    blocked: list[str] = []
    try:
        for source in targets:
            record = asyncio.run(eval_source(client, args.corpus, source, grader, run_id))
            write_record(record, run_dir)
            emit_event(
                "source_evaluated",
                source=source,
                source_score=record.source_score,
                verdict=record.verdict,
                gate_passed=record.gate["passed"],
            )
            rows.append(
                {
                    "source": source,
                    "source_score": record.source_score,
                    "verdict": record.verdict,
                    "gate": record.gate,
                    "stages": {r.stage: {"status": r.status, "stage_score": r.stage_score} for r in record.stages},
                }
            )
            print(f"{source}: source_score={record.source_score} verdict={record.verdict}")
            if record.verdict == "fail" or not record.gate["deterministic_all_pass"]:
                blocked.append(source)
    finally:
        setup_event_log(None)

    (run_dir / "_summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "judge_model": str(getattr(client, "model_id", "")),
                "rubric_version": grader.schema_version,
                "gate_calibrated": bool(grader.aggregation["gate"].get("calibrated")),
                "sources": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if blocked:
        print(f"确定性拦截/判 fail: {blocked}", file=sys.stderr)
        if args.ci:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
