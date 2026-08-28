#!/usr/bin/env python3
"""在真实 ReAct Agent 上运行 QA golden 集并保存回答/工具轨迹。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from evals.qa_harness import load_manifest
from wiki_agent.agent import ReActAgent
from wiki_agent.config import load_config
from wiki_agent.hook import AgentHook, RunContext
from wiki_agent.llm.factory import create_llm, create_vlm
from wiki_agent.tools import Grep, ListDir, ReadFile, ToolRegistry


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


async def run(args: argparse.Namespace) -> dict[str, Any]:
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
            if item.get("conversation"):
                for turn in item["conversation"][:-1]:
                    await agent.run(session, turn, stream=False)
            await agent.run(session, item["question"], stream=False)
        except Exception as exc:
            hook.error = f"{type(exc).__name__}: {exc}"
        citations = [
            str(call.get("arguments", {}).get("file_path"))
            for call in hook.calls
            if isinstance(call.get("arguments"), dict)
            and call.get("arguments", {}).get("file_path")
        ]
        records.append(
            {
                "case_id": item["id"],
                "answer": hook.answer,
                "tool_calls": hook.calls,
                "citations": citations,
                "session_key": session,
                "agent_error": hook.error,
            }
        )
        print(
            f"[{len(records)}/{len(manifest['cases'])}] {item['id']}: "
            f"answer={len(hook.answer)} chars tools={len(hook.calls)}"
            + (f" error={hook.error[:120]}" if hook.error else ""),
            flush=True,
        )
    output = {"wiki_dir": str(args.wiki_dir), "workspace": str(workspace), "records": records}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wiki-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument(
        "--manifest", type=Path, default=PROJECT_ROOT / "evals/golden/qa_manifest.json"
    )
    args = parser.parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
