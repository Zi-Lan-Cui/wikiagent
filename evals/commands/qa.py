#!/usr/bin/env python3
"""评估一批已记录的 QA 回答；可选接入独立 LLM Judge。

records.json 格式：[{"case_id": "qa-process-states", "answer": "...",
"tool_calls": [{"name": "ReadFile", "arguments": {}, "result": "..."}],
"citations": ["concepts/process-state.md"]}]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from evals.core.paths import PROJECT_ROOT, require_directory, require_file
from evals.core.qa_harness import load_cases, load_manifest, score_answer, summarize
from evals.judges.qa_semantic_judge import judge_qa_case
from wiki_agent.agent import ReActAgent
from wiki_agent.config import load_config
from wiki_agent.events import AgentHook, RunContext
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.tools import Grep, ListDir, ReadFile, ToolRegistry


def _pages(wiki_dir: Path) -> tuple[set[str], dict[str, str]]:
    pages: dict[str, str] = {}
    for path in wiki_dir.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(wiki_dir).as_posix()
        pages[rel] = path.read_text(encoding="utf-8")[:12000]
    return set(pages), pages


async def main_async(args) -> int:
    manifest = {case.id: case for case in load_cases(args.manifest)}
    records = json.loads(args.records.read_text(encoding="utf-8"))
    if isinstance(records, dict) and isinstance(records.get("records"), list):
        records = records["records"]
    if not isinstance(records, list):
        raise ValueError("records 必须是 JSON 数组")
    page_names, pages = _pages(args.wiki_dir) if args.wiki_dir else (None, {})
    hard = []
    for record in records:
        case = manifest.get(record.get("case_id"))
        if case is None:
            hard.append({"case_id": record.get("case_id"), "error": "unknown_case"})
            continue
        hard.append(
            {
                "case_id": case.id,
                "score": score_answer(case, record, existing_pages=page_names).__dict__,
            }
        )
    output = {
        "hard": hard,
        "summary": summarize(
            [
                score_answer(manifest[r["case_id"]], r, existing_pages=page_names)
                for r in records
                if r.get("case_id") in manifest
            ]
        ),
    }
    if args.semantic:
        cfg = load_config(project_root=PROJECT_ROOT)
        client = create_llm(cfg.llm, cfg.retry)
        semantic = []
        for record in records:
            case = manifest.get(record.get("case_id"))
            if case is None:
                continue
            evidence = {name: pages[name] for name in record.get("citations", []) if name in pages}
            if case_item := next(
                (
                    x
                    for x in json.loads(args.manifest.read_text(encoding="utf-8"))["cases"]
                    if x["id"] == case.id
                ),
                None,
            ):
                if case_item.get("policy_evidence"):
                    evidence["[agent-policy]"] = case_item["policy_evidence"]
            else:
                continue
            semantic.append(
                {
                    "case_id": case.id,
                    "score": await judge_qa_case(client, case_item, record, evidence),
                }
            )
        output["semantic"] = semantic
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return (
        0
        if all(item.get("score", {}).get("passed", False) for item in hard if "score" in item)
        else 1
    )


def score_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("records", type=Path)
    parser.add_argument(
        "--manifest", type=Path, default=PROJECT_ROOT / "evals/templates/qa_manifest.json"
    )
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.records = require_file(args.records, kind="QA records")
    args.manifest = require_file(args.manifest, kind="QA manifest")
    if args.wiki_dir:
        args.wiki_dir = require_directory(args.wiki_dir, kind="Wiki 根目录")
    if args.output:
        args.output = args.output.expanduser().resolve()
    return asyncio.run(main_async(args))


class CaptureHook(AgentHook):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []
        self.answer = ""
        self.error = ""

    async def on_tool_call_start(
        self, context: RunContext, tool_name: str, tool_call_id: str, arguments: dict[str, Any]
    ) -> None:
        self.calls.append(
            {"id": tool_call_id, "name": tool_name, "arguments": arguments, "result": None}
        )

    async def on_tool_result(
        self, context: RunContext, tool_name: str, tool_call_id: str, result: Any
    ) -> None:
        for call in reversed(self.calls):
            if call["id"] == tool_call_id:
                call["result"] = str(result)
                break

    async def on_tool_error(
        self, context: RunContext, tool_name: str, tool_call_id: str, error: Any
    ) -> None:
        for call in reversed(self.calls):
            if call["id"] == tool_call_id:
                call["error"] = f"{type(error).__name__}: {error}"
                break

    async def on_run_end(self, context: RunContext) -> None:
        self.answer = context.final_content or ""

    async def on_run_error(self, context: RunContext) -> None:
        self.error = context.error or "unknown agent error"


def build_agent(wiki_dir: Path, workspace: Path) -> ReActAgent:
    cfg = load_config(project_root=PROJECT_ROOT)
    registry = ToolRegistry()
    registry.register(ReadFile(wiki_dir, workspace=workspace))
    registry.register(ListDir(wiki_dir))
    registry.register(Grep(wiki_dir))
    return ReActAgent(
        name="qa-eval",
        llm=create_llm(cfg.llm, cfg.retry),
        vlm=create_vlm(cfg.vlm, cfg.retry),
        tool_registry=registry,
        workspace=workspace,
        wiki_dir=wiki_dir,
        agent_config=cfg.agent,
        compile_config=cfg.compile,
        retry_config=cfg.retry,
    )


async def live_run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    workspace = (
        Path(args.workspace) if args.workspace else Path(tempfile.mkdtemp(prefix="wiki-agent-qa-"))
    )
    workspace.mkdir(parents=True, exist_ok=True)
    agent = build_agent(args.wiki_dir, workspace)
    records: list[dict[str, Any]] = []
    for item in manifest["cases"]:
        hook = CaptureHook()
        agent._hooks = hook
        session = f"qa-{item['id']}"
        try:
            for turn in item.get("conversation", [])[:-1]:
                await agent.run(session, turn, stream=False)
            await agent.run(session, item["question"], stream=False)
        except Exception as exc:
            hook.error = f"{type(exc).__name__}: {exc}"
        records.append(
            {
                "case_id": item["id"],
                "answer": hook.answer,
                "tool_calls": hook.calls,
                "citations": [
                    str(c.get("arguments", {}).get("file_path"))
                    for c in hook.calls
                    if isinstance(c.get("arguments"), dict)
                    and c.get("arguments", {}).get("file_path")
                ],
                "session_key": session,
                "agent_error": hook.error,
            }
        )
    output = {"wiki_dir": str(args.wiki_dir), "workspace": str(workspace), "records": records}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def live_main() -> int:
    parser = argparse.ArgumentParser(description="运行 QA Agent 并保存回答记录")
    parser.add_argument("--wiki-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument(
        "--manifest", type=Path, default=PROJECT_ROOT / "evals/templates/qa_manifest.json"
    )
    args = parser.parse_args(sys.argv[2:])
    args.wiki_dir = require_directory(args.wiki_dir, kind="Wiki 根目录")
    args.manifest = require_file(args.manifest, kind="QA manifest")
    args.output = args.output.expanduser().resolve()
    asyncio.run(live_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(live_main() if len(sys.argv) > 1 and sys.argv[1] == "live" else score_main())
