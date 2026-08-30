#!/usr/bin/env python3
"""真实编译中断后 resume 的端到端验收入口。

流程：抽样真实 Markdown → 启动真实 compile_manifest → 定时发送 SIGINT
→ 检查 state 保留中断状态 → 使用同一 state 执行 --resume → 验证全部
source committed 和 Wiki Git 有提交。脚本不修改原始笔记目录。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _sources(root: Path, count: int, seed: int) -> list[dict[str, str]]:
    files = sorted(path for path in root.rglob("*.md") if path.is_file())
    if len(files) < count:
        raise ValueError(f"Markdown 文件不足 {count} 个，实际只有 {len(files)} 个")
    selected = random.Random(seed).sample(files, count)
    return [
        {"id": f"live-{index:03d}", "path": str(path.relative_to(root))}
        for index, path in enumerate(selected, 1)
    ]


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(command))
    return subprocess.run(
        command, cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=timeout
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="真实 Markdown 根目录")
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument(
        "--interrupt-after", type=float, default=8.0, help="运行多少秒后发送 SIGINT"
    )
    parser.add_argument("--resume-timeout", type=int, default=900, help="resume 最长等待秒数")
    args = parser.parse_args()
    if args.sample_size < 1 or args.interrupt_after <= 0 or args.resume_timeout <= 0:
        parser.error("sample-size、interrupt-after、resume-timeout 必须大于 0")

    root = args.root.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="wiki-agent-live-resume-") as directory:
        workspace = Path(directory)
        manifest = workspace / "manifest.json"
        wiki = workspace / "wiki"
        state = workspace / "state.json"
        work = workspace / "sources"
        manifest.write_text(
            json.dumps({"sources": _sources(root, args.sample_size, args.seed)}), encoding="utf-8"
        )
        base = [
            sys.executable,
            str(PROJECT_ROOT / "scripts/compile_manifest.py"),
            "--root",
            str(root),
            "--manifest",
            str(manifest),
            "--wiki-dir",
            str(wiki),
            "--batch-size",
            "1",
            "--commit-scope",
            "source",
            "--state",
            str(state),
            "--work-dir",
            str(work),
            "--init-git",
        ]
        print("开始真实编译，等待中断窗口…")
        process = subprocess.Popen(
            base,
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        time.sleep(args.interrupt_after)
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=120)
        print(output)
        if not state.is_file():
            print("[FAIL] 中断后没有生成 state 文件")
            return 1
        interrupted = json.loads(state.read_text(encoding="utf-8"))
        statuses = [item.get("status") for item in interrupted.get("source_state", {}).values()]
        if not any(status in {"running", "interrupted", "pending"} for status in statuses):
            print("[FAIL] 中断发生时没有留下可恢复 source 状态")
            return 1
        print(f"[PASS] 中断后 state 已保存: {statuses}")

        resumed = _run(base + ["--resume"], args.resume_timeout)
        print(resumed.stdout)
        if resumed.stderr:
            print(resumed.stderr, file=sys.stderr)
        if resumed.returncode != 0:
            print(f"[FAIL] resume 返回码: {resumed.returncode}")
            return 1
        final = json.loads(state.read_text(encoding="utf-8"))
        final_statuses = [item.get("status") for item in final.get("source_state", {}).values()]
        if not final_statuses or not all(status == "committed" for status in final_statuses):
            print(f"[FAIL] resume 后 source 状态不完整: {final_statuses}")
            return 1
        print("[PASS] resume 后全部 source committed")
        log = subprocess.run(
            ["git", "-C", str(wiki), "log", "--oneline", "-5"],
            text=True,
            capture_output=True,
            check=True,
        )
        print(log.stdout)
    print("live compile → interrupt → resume: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
