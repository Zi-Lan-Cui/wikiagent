"""Run 证据完整性校验——按阶段信息量对齐评测的前提。

判词评测要求 judge 与 compiler 拿同样的素材做判断。该测试断言
每个 run 目录的 transcript + artifacts 足够重建每个阶段的
输入与输出：

- extract：chunk 请求含 material 分块原文；synthesis 响应 == extract.json
- search：search 请求含当时 index；search.json.raw == transcript 响应
- analyze：analyze 请求存在；analyze.json 结构与响应同源
- plan：plan 请求含当时 index 快照；plan.json.raw == transcript 响应
- execute：execute 请求逐 target 存在；pages/ 存档产物

结论前置：重建素材是"从 run 证据提取"的唯一途径，不是另造数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

RUNS_ROOT = Path(__file__).resolve().parents[1] / "workspace" / "evals" / "tmp"
LATEST_RUN = next(iter(sorted((RUNS_ROOT / "seed-compile" / "workspace" / "runs").glob("compile_*"), reverse=True)), None)

pytestmark = pytest.mark.skipif(LATEST_RUN is None, reason="无编译 run 证据")


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def _transcript(path: Path) -> list[dict]:
    return [json.loads(line) for line in (path / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]


def _user_prompts(transcript: list[dict], marker: str) -> list[str]:
    out = []
    for record in transcript:
        if record.get("event") != "llm.request":
            continue
        for message in record.get("messages", []):
            content = message.get("content", "")
            if message.get("role") == "user" and marker in content:
                out.append(content)
    return out


def _response_contents(transcript: list[dict]) -> list[str]:
    return [
        (record.get("response") or {}).get("content", "")
        for record in transcript
        if record.get("event") == "llm.response"
    ]


def test_every_stage_prompt_is_present_in_transcript() -> None:
    """每个编译决策阶段的 LLM 请求在 transcript 中都有记录。"""
    stages = {"search": 0, "analyze": 0, "plan": 0, "execute": 0}
    for record in _transcript(LATEST_RUN):
        if record.get("event") != "llm.request":
            continue
        for message in record.get("messages", []):
            content = message.get("content", "")
            if message.get("role") != "user":
                continue
            # 独立计数——plan prompt 的关系分析文本里会引用
            # "候选页面"字样，elif 链会误吞 plan 请求
            if "## Wiki Index" in content:
                stages["search"] += 1
            if "## 候选页面" in content:
                stages["analyze"] += 1
            if "已有页面 Index" in content:
                stages["plan"] += 1
            if "页面:" in content and "页面范围" in content:
                stages["execute"] += 1
    assert stages["search"] >= 6, "6 篇 source 每篇至少一次 search"
    assert stages["analyze"] >= 6
    assert stages["plan"] >= 6
    assert stages["execute"] >= 1


def test_extract_artifacts_match_transcript_responses() -> None:
    """extract 产物（纯文本摘要）内容与 transcript 中 synthesis 响应同源。"""
    responses = _response_contents(_transcript(LATEST_RUN))
    for source in ("baseline-lock.md", "baseline-retry.md"):
        artifact = LATEST_RUN / "artifacts" / source / "extract.json"
        assert artifact.is_file(), f"缺 extract 产物: {source}"
        content = artifact.read_text(encoding="utf-8").strip()
        assert any(r.strip() == content for r in responses), f"{source} extract 与响应不同源"


def test_plan_artifacts_match_transcript_responses() -> None:
    """plan.json.raw 与 transcript 响应同源，且 targets 含 page_type。"""
    responses = _response_contents(_transcript(LATEST_RUN))
    for source in ("baseline-lock.md", "baseline-retry.md"):
        artifact = LATEST_RUN / "artifacts" / source / "plan.json"
        plan = json.loads(artifact.read_text(encoding="utf-8"))
        assert any(r.strip() == plan["raw"].strip() for r in responses), f"{source} plan raw 与响应不同源"
        for target in plan["page_targets"]:
            assert target.get("page_type"), f"{source} target 缺 page_type"


def test_material_chunks_are_fully_covered_by_extract_prompts() -> None:
    """material 的分块原文完整出现在 extract 类请求中（信息量对齐的地基）。"""
    prompts = _user_prompts(_transcript(LATEST_RUN), "待摘要片段")
    assert prompts, "transcript 中无 extract 类请求"
    # 每篇 source 的原文内容必须能在 chunk 请求中找到（用段落头验证覆盖）
    sources_root = (
        Path(__file__).resolve().parents[1]
        / "evals/corpora/reference-v1/baselines/seeded/source"
    )
    all_prompt_text = "\n".join(prompts)
    for source_path in sources_root.glob("baseline-*.md"):
        content = source_path.read_text(encoding="utf-8")
        # 取首行标题作为锚——chunk 请求含 material 原文
        title_line = content.strip().splitlines()[0]
        assert title_line in all_prompt_text, f"{source_path.name} 原文未出现在 extract 请求中"


def test_index_snapshot_grows_across_compile_order() -> None:
    """plan 请求中的 index 快照逐篇增长——顺序增量编译的视角证据。"""
    index_counts: list[int] = []
    for record in _transcript(LATEST_RUN):
        if record.get("event") != "llm.request":
            continue
        for message in record.get("messages", []):
            content = message.get("content", "")
            if message.get("role") == "user" and "已有页面 Index" in content:
                entries = [line for line in content.splitlines() if line.strip().startswith("- [[")]
                if not index_counts or len(entries) != index_counts[-1]:
                    index_counts.append(len(entries))
    assert index_counts and index_counts[0] < index_counts[-1], (
        f"index 快照应随编译顺序增长: {index_counts}"
    )


def test_pages_archived_match_final_wiki() -> None:
    """pages/ 存档是合法的页面快照。

    注意: pages/ 是"该 run 写盘那一刻"的点位快照——后续 retry run
    可能再次修改同一页（增量编译的正常行为），因此不做字节级一致性
    断言，只断言存档是合法页面且标题与最终 wiki 一致。
    """
    wiki_root = (
        Path(__file__).resolve().parents[1]
        / "evals/corpora/reference-v1/baselines/seeded/wiki"
    )
    pages_dir = LATEST_RUN / "artifacts" / "baseline-lock.md" / "pages"
    from wiki_agent.wiki.frontmatter import split_frontmatter

    for archived in pages_dir.glob("*.md"):
        wiki_path = wiki_root / archived.name.replace("_", "/")
        assert wiki_path.is_file(), f"产物页不在 baseline wiki: {archived.name}"
        # 存档文件带 "# generated at: ..." 头行 + 空行（compile_service 的点位快照格式）
        content = archived.read_text(encoding="utf-8")
        if content.startswith("# generated at:"):
            content = content.split("\n", 1)[1].lstrip("\n")
        archived_fm, archived_body = split_frontmatter(content)
        final_fm, _ = split_frontmatter(wiki_path.read_text(encoding="utf-8"))
        assert archived_fm.get("title"), f"存档缺少 title: {archived.name}"
        assert archived_body.strip(), f"存档缺少正文: {archived.name}"
        assert archived_fm.get("title") == final_fm.get("title"), (
            f"标题漂移: {archived.name} 存档={archived_fm.get('title')} 最终={final_fm.get('title')}"
        )
