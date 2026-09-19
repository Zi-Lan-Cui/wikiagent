"""真实 LLM 的 extract prompt 试验。

默认跳过，显式运行：

    RUN_LIVE_LLM_TESTS=1 uv run pytest -s -q test/test_extract_live.py

这个文件只报告模型行为，不把具体模型的措辞差异硬编码成脆弱断言。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from wiki_agent.compiler.models import SourceChunk
from wiki_agent.compiler.prompts import compile as prompts
from wiki_agent.config import load_config
from wiki_agent.conversation import Message
from wiki_agent.llm.factory import create_llm
from wiki_agent.llm.retry import async_invoke_with_retry

ROOT = Path(__file__).resolve().parents[1]


CASES = [
    {
        "name": "asyncio 技术概念",
        "source": (
            "Python asyncio 使用 event loop 调度 coroutine。\n"
            "await 会暂停当前 coroutine，直到等待的异步结果可用。"
        ),
        "must_contain": ["event loop", "coroutine"],
        "must_not_contain": ["多线程保证并行", "超光速"],
    },
    {
        "name": "数字与实验结果",
        "source": "实验使用 20 个样本，准确率为 86%。没有报告其他数据。",
        "must_contain": ["20", "86"],
        "must_not_contain": [
            "2000",
            "99.9",
            "所有数据集",
            "证明普遍有效",
            "根据提供的片段摘要",
        ],
    },
    {
        "name": "代码与重试参数",
        "source": (
            "客户端最多重试 3 次。退避等待时间依次为 1 秒、2 秒、4 秒。\n"
            "超过 3 次后将错误交给调用方。"
        ),
        "must_contain": ["3", "1", "2", "4"],
        "must_not_contain": ["5 次", "无限重试", "保证成功"],
    },
    {
        "name": "表格与公式",
        "source": (
            "| 参数 | 值 |\n| learning_rate | 0.1 |\n| batch_size | 32 |\n"
            "梯度下降更新公式为 θ = θ - α∇J(θ)。"
        ),
        "must_contain": ["0.1", "32"],
        "must_not_contain": [
            "learning_rate 必须是 0.01",
            "batch_size 必须是 64",
            "α 明确等于学习率",
            "α 确定为学习率",
        ],
    },
    {
        "name": "不确定信息不得补全",
        "source": (
            "这是一份内部草稿，只说明服务将在下周评估。\n文档没有给出上线日期、负责人或性能指标。"
        ),
        "must_contain": ["下周", "没有给出"],
        "must_not_contain": [
            "已经上线",
            "负责人是",
            "性能达到",
            "确定于",
            "根据提供的片段摘要",
        ],
    },
]


def _run(coro):
    return asyncio.run(coro)


async def _call(llm, messages: list[Message]) -> str:
    # extract.py 的真实路径是 async_invoke_with_retry + thinking disabled；
    # live 测试必须复现这条路径，而不是复现 CLI 的 stream=True。
    response = await async_invoke_with_retry(
        llm,
        messages,
        max_tokens=700,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
        max_attempts=2,
        base_delay=0.2,
    )
    return (response.content or "").strip()


@pytest.mark.live
def test_real_llm_extract_prompt_report():
    """调用真实 LLM，打印摘要结果与事实约束命中情况供人工评判。"""
    if os.getenv("RUN_LIVE_LLM_TESTS") != "1":
        pytest.skip("设置 RUN_LIVE_LLM_TESTS=1 才运行真实 LLM 测试")

    # 超时通过 LLM_TIMEOUT 环境变量覆盖；不要使用当前 RootConfig 的
    # 浅层 overrides={"llm": {...}}，否则会覆盖整个 llm 子配置并丢失 API key。
    cfg = load_config(project_root=ROOT)
    llm = create_llm(cfg.llm)

    reports: list[str] = []
    selected = os.getenv("LIVE_CASES", "")
    selected_ids = (
        {int(item.strip()) for item in selected.split(",") if item.strip()}
        if selected
        else set(range(1, len(CASES) + 1))
    )
    for i, case in enumerate(CASES, 1):
        if i not in selected_ids:
            continue
        chunk = SourceChunk(
            content=case["source"],
            index=0,
            total=1,
            source_name=f"live_case_{i}.md",
        )
        chunk_summary = _run(
            _call(
                llm,
                [
                    Message(role="system", content=prompts.chunk_system()),
                    Message(role="user", content=prompts.chunk_user(chunk)),
                ],
            )
        )
        synthesis_input = f"## Chunk 0（{case['name']}）\n{chunk_summary}\n"
        document_summary = _run(
            _call(
                llm,
                [
                    Message(role="system", content=prompts.synthesis_prompt()),
                    Message(role="user", content=synthesis_input),
                ],
            )
        )

        required_hits = [
            item for item in case["must_contain"] if item.lower() in document_summary.lower()
        ]
        forbidden_hits = [
            item for item in case["must_not_contain"] if item.lower() in document_summary.lower()
        ]
        reports.append(
            f"\n=== Case {i}: {case['name']} ===\n"
            f"SOURCE:\n{case['source']}\n\n"
            f"CHUNK SUMMARY:\n{chunk_summary}\n\n"
            f"DOCUMENT SUMMARY:\n{document_summary}\n\n"
            f"REQUIRED: {required_hits}/{case['must_contain']}\n"
            f"FORBIDDEN: {forbidden_hits}"
        )

    print("\n".join(reports))
    # 这里只保证真实调用返回内容；语义质量和违禁项命中由上面的报告人工评判。
    assert all("DOCUMENT SUMMARY:\n\n" not in report for report in reports)
