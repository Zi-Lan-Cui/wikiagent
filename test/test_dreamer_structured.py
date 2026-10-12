"""Dreamer 结构化协议测试——校验拒收、毒丸出口、游标、TTL、溯源。

脚本化 LLM，无真实模型依赖。

直接运行:  .venv/bin/python test/test_dreamer_structured.py
"""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from wiki_agent.config import MemoryConfig
from wiki_agent.conversation import LLMResponse, Session
from wiki_agent.memory.dreamer import Dreamer
from wiki_agent.memory.ops import MemoryOps
from wiki_agent.memory.store import MemoryStore


def _op(op: str, **kwargs) -> dict:
    """协议形态的单条 op：未涉及字段填 null/空数组。"""
    base = {
        "op": op,
        "target": None,
        "text": None,
        "kind": None,
        "persistence": None,
        "evidence": [],
        "reason": None,
        "by": None,
    }
    base.update(kwargs)
    return base


def _llm(responses: list[str]):
    prompts: list[str] = []

    async def invoke(messages, **kwargs):
        prompts.append(messages[0].content)
        return LLMResponse(content=responses[len(prompts) - 1], finish_reason="stop")

    llm = MagicMock()
    llm.async_invoke = AsyncMock(side_effect=invoke)
    return llm, prompts


def _history(store: MemoryStore, rows: list[tuple[str, str]]) -> None:
    for key, summary in rows:
        store.append_history(Session(key=key), summary)


def test_dream_applies_ops_and_derives_sources(tmp_path: Path):
    """成功批次：ops 落事件、memory.md 出投影、游标推进；sources 由程序自 evidence 推导。"""

    async def run():
        store = MemoryStore(workspace=tmp_path)
        _history(store, [("s1", "用户说喜欢登山"), ("s2", "用户说讨厌早起")])
        dreamer = Dreamer(workspace=tmp_path, memory_store=store)
        payload = json.dumps(
            {"ops": [_op("add", text="喜欢登山", kind="preference", persistence="durable", evidence=[1, 2])]},
            ensure_ascii=False,
        )
        llm, prompts = _llm([payload])
        await dreamer.dream(llm)

        events = store.read_events()
        assert events[0].event == "add"
        assert events[0].sources == ["s1", "s2"]  # 模型未提供 sources，程序回填
        assert store.get_dream_cursor() == 2
        md = store.memory_file.read_text(encoding="utf-8")
        assert "- 喜欢登山" in md
        assert "(0, 2]" in prompts[0]

    asyncio.run(run())


def test_hallucinated_target_rejected_rest_applied(tmp_path: Path):
    """单条幻觉（target 不存在）只丢该条进 rejected.jsonl，其余照常生效、游标推进。"""

    async def run():
        store = MemoryStore(workspace=tmp_path)
        _history(store, [("s1", "新的爱好是观鸟")])
        dreamer = Dreamer(workspace=tmp_path, memory_store=store)
        payload = json.dumps(
            {
                "ops": [
                    _op("revise", target="m_9999", text="幽灵条目"),
                    _op("add", text="爱好观鸟", kind="preference", persistence="durable", evidence=[1]),
                ]
            },
            ensure_ascii=False,
        )
        llm, _ = _llm([payload])
        await dreamer.dream(llm)

        rejected = [json.loads(x) for x in store.rejected_file.read_text(encoding="utf-8").splitlines()]
        assert rejected[0]["reason"] == "unknown_target"
        assert store.get_dream_cursor() == 1
        assert "爱好观鸟" in store.memory_file.read_text(encoding="utf-8")

    asyncio.run(run())


def test_reactivate_visible_invalid_and_restores(tmp_path: Path):
    """失效条目带标注仍在输入里；reactivate 把它救回投影。"""

    async def run():
        store = MemoryStore(workspace=tmp_path)
        ops = MemoryOps(store)
        ghost = ops.add("常驻北京", "identity", "durable", [], [1])
        ops.invalidate(ghost, None, "obsolete")
        _history(store, [("s1", "用户其实还长驻北京")])
        dreamer = Dreamer(workspace=tmp_path, memory_store=store)
        payload = json.dumps(
            {"ops": [_op("reactivate", target=ghost, evidence=[1])]}, ensure_ascii=False
        )
        llm, prompts = _llm([payload])
        await dreamer.dream(llm)

        assert "invalid since" in prompts[0]
        event_names = [e.event for e in store.read_events()]
        assert event_names == ["add", "invalidate", "reactivate"]
        assert "常驻北京" in store.memory_file.read_text(encoding="utf-8")
        assert store.get_dream_cursor() == 1

    asyncio.run(run())


def test_invalidate_by_alias_to_batch_add(tmp_path: Path):
    """by 用 #下标 引用本批 add：程序解析为分配后的 id。"""

    async def run():
        store = MemoryStore(workspace=tmp_path)
        ops = MemoryOps(store)
        old = ops.add("项目用 Vue", "project", "durable", [], [1])
        _history(store, [("s1", "项目已迁移到 React")])
        dreamer = Dreamer(workspace=tmp_path, memory_store=store)
        payload = json.dumps(
            {
                "ops": [
                    _op("add", text="项目用 React", kind="project", persistence="durable", evidence=[1]),
                    _op("invalidate", target=old, by="#0", reason="superseded"),
                ]
            },
            ensure_ascii=False,
        )
        llm, _ = _llm([payload])
        await dreamer.dream(llm)

        last = store.read_events()[-1]
        assert last.event == "invalidate" and last.by == "m_0002"
        md = store.memory_file.read_text(encoding="utf-8")
        assert "项目用 React" in md and "项目用 Vue" not in md

    asyncio.run(run())


def test_poison_batch_force_advances(tmp_path: Path):
    """连续 max_attempts 次整批非法：归档后强制推进游标——坏批次不许无限重放。"""

    async def run():
        store = MemoryStore(workspace=tmp_path)
        _history(store, [("s1", "无法消化的记录")])
        dreamer = Dreamer(
            workspace=tmp_path, memory_store=store, config=MemoryConfig(max_attempts=3)
        )
        llm, _ = _llm(["非JSON", "非JSON", "非JSON"])
        await dreamer.dream(llm)

        assert llm.async_invoke.call_count == 3
        assert store.get_dream_cursor() == 1
        lines = [json.loads(x) for x in store.rejected_file.read_text(encoding="utf-8").splitlines()]
        assert [r["reason"] for r in lines] == ["schema", "schema", "schema", "poison_batch"]

    asyncio.run(run())


def test_empty_batch_rebuilds_projection(tmp_path: Path):
    """无新 history：事件流零新增，投影仍被幂等重建（memory.md 删除后可恢复）。"""

    async def run():
        store = MemoryStore(workspace=tmp_path)
        ops = MemoryOps(store)
        ops.add("既有偏好", "preference", "permanent", [], [1])
        dreamer = Dreamer(workspace=tmp_path, memory_store=store)
        n_before = len(store.read_events())

        store.memory_file.unlink(missing_ok=True)
        llm, _ = _llm([])
        await dreamer.dream(llm)

        assert len(store.read_events()) == n_before
        assert "既有偏好" in store.memory_file.read_text(encoding="utf-8")

    asyncio.run(run())


def test_sweep_expires_out_of_window(tmp_path: Path):
    """TTL 从 invalid_at 起算；出窗条目 expire，退出全部视图并触发投影重渲染。"""

    async def run():
        past = datetime(2025, 1, 1, 12, 0, 0)
        store = MemoryStore(workspace=tmp_path)
        ops = MemoryOps(store, clock=lambda: past)
        ghost = ops.add("临时期中目标", "task_state", "ephemeral", [], [1])
        ops.invalidate(ghost, None, "obsolete")

        future = past + timedelta(days=15)
        dreamer = Dreamer(
            workspace=tmp_path,
            memory_store=store,
            config=MemoryConfig(ttl_days={"ephemeral": 14}),
            clock=lambda: future,
        )
        assert await dreamer.sweep() == 1
        assert store.read_events()[-1].event == "expire"
        assert "临时期中目标" not in store.memory_file.read_text(encoding="utf-8")
        assert MemoryOps(store, clock=lambda: future).all_items() == []

    asyncio.run(run())


if __name__ == "__main__":
    root = Path(tempfile.mkdtemp())
    tests = [
        ("apply_sources", test_dream_applies_ops_and_derives_sources),
        ("hallucinate", test_hallucinated_target_rejected_rest_applied),
        ("reactivate", test_reactivate_visible_invalid_and_restores),
        ("by_alias", test_invalidate_by_alias_to_batch_add),
        ("poison", test_poison_batch_force_advances),
        ("empty_rebuild", test_empty_batch_rebuilds_projection),
        ("sweep_ttl", test_sweep_expires_out_of_window),
    ]
    for i, (name, fn) in enumerate(tests):
        fn(root / f"d{i}")
        print("PASS", name)
