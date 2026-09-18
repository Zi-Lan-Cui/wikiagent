"""真实编译中断后 resume 的端到端验收（live，默认跳过）。

流程：抽样真实 Markdown → 起真实 batch_compile → 定时 SIGINT → 校验 state 保留
中断态 → 同一 state --resume → 校验全部 source committed + Wiki Git 有提交。
不修改原始笔记目录。运行：

    RUN_LIVE_LLM_TESTS=1 WIKI_LIVE_CORPUS=/path/to/md-root \
      uv run pytest -s -q test/test_resume_live.py
"""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _sources(root: Path, count: int, seed: int) -> list[dict[str, str]]:
    files = sorted(p for p in root.rglob("*.md") if p.is_file())
    if len(files) < count:
        pytest.skip(f"语料不足 {count} 篇（实际 {len(files)}）")
    selected = random.Random(seed).sample(files, count)
    return [
        {"id": f"live-{i:03d}", "path": str(p.relative_to(root))} for i, p in enumerate(selected, 1)
    ]


@pytest.mark.live
def test_interrupt_then_resume() -> None:
    if os.getenv("RUN_LIVE_LLM_TESTS") != "1":
        pytest.skip("设置 RUN_LIVE_LLM_TESTS=1 才运行真实 LLM 端到端测试")
    root = os.getenv("WIKI_LIVE_CORPUS") or os.getenv("WIKI_NOTEBOOK_ROOT")
    if not root:
        pytest.skip("需设 WIKI_LIVE_CORPUS 指向真实 Markdown 根目录")
    root_path = Path(root).expanduser().resolve()

    sample_size, interrupt_after, resume_timeout = 3, 8.0, 900
    with tempfile.TemporaryDirectory(prefix="wiki-agent-live-resume-") as directory:
        workspace = Path(directory)
        manifest = workspace / "manifest.json"
        wiki = workspace / "wiki"
        state = workspace / "state.json"
        work = workspace / "sources"
        manifest.write_text(
            json.dumps({"sources": _sources(root_path, sample_size, 20260831)}), encoding="utf-8"
        )
        base = [
            sys.executable, "-m", "wiki_agent.application.batch_compile",
            "--root", str(root_path), "--manifest", str(manifest), "--wiki-dir", str(wiki),
            "--batch-size", "1", "--commit-scope", "source", "--state", str(state),
            "--work-dir", str(work), "--init-git",
        ]
        proc = subprocess.Popen(
            base, cwd=ROOT, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=os.environ.copy(),
        )
        time.sleep(interrupt_after)
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=120)

        assert state.is_file(), "中断后未生成 state 文件"
        interrupted = json.loads(state.read_text(encoding="utf-8"))
        statuses = [i.get("status") for i in interrupted.get("source_state", {}).values()]
        assert any(s in {"running", "interrupted", "pending"} for s in statuses), (
            f"中断时未留下可恢复状态: {statuses}"
        )

        resumed = subprocess.run(
            base + ["--resume"], cwd=ROOT, text=True, capture_output=True, timeout=resume_timeout
        )
        assert resumed.returncode == 0, f"resume 返回码 {resumed.returncode}: {resumed.stdout[-500:]}"
        final = json.loads(state.read_text(encoding="utf-8"))
        final_statuses = [i.get("status") for i in final.get("source_state", {}).values()]
        assert final_statuses and all(s == "committed" for s in final_statuses), (
            f"resume 后状态不完整: {final_statuses}"
        )
