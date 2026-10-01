"""维护轮次验收：单元提交口、批尾补链、执行五段、link 应用与闸。

LLM 参与的两段（路由、成文、link 清单）用 monkeypatch 假件，装配与
落盘判定全真（git=None 离线协议）。

直接运行:  .venv/bin/python -m pytest test/test_maintenance.py
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import make_job_service

from wiki_agent.application import wiki_ops as wiki_ops_mod
from wiki_agent.application.wiki_ops import WikiOpsHandler
from wiki_agent.compiler import restructure as rs
from wiki_agent.compiler.content_pages import all_content_slugs
from wiki_agent.compiler.link import apply_link_fixes
from wiki_agent.compiler.restructure import plan as plan_mod
from wiki_agent.compiler.restructure import prompts
from wiki_agent.compiler.restructure import route as route_mod
from wiki_agent.conversation import LLMResponse
from wiki_agent.jobs import (
    Kind,
    PipelineBusy,
    RestructureInProgress,
    Settlement,
    SyncBaselineLag,
)
from wiki_agent.jobs.service import JobService
from wiki_agent.jobs.worker import JobWorker
from wiki_agent.sync.state import SyncState
from wiki_agent.wiki.frontmatter import split_frontmatter
from wiki_agent.wiki.rules import count_unclosed_fences
from wiki_agent.wiki.sections import Section, page_sections

_FM = (
    "---\ntype: {type}\ntitle: \"{title}\"\nsummary: \"一个足够长的摘要信息\"\n"
    "goal: \"说明该页要解决的问题\"\nrelated: []\n---\n"
)
_TYPE = {"concepts": "concept", "entities": "entity", "topics": "topic"}


class _LLM:
    """只满足 async_invoke 一个面的假客户端，返回固定 JSON。

    自带 retry_config（提交口不再兜底默认）——退避 0，校验失败的用例
    重试也不拖慢测试。
    """

    retry_config = SimpleNamespace(llm_max_attempts=2, llm_base_delay_seconds=0.0)

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def async_invoke(self, messages, **kw) -> LLMResponse:
        return LLMResponse(content=json.dumps(self.payload))


def _page(wiki: Path, slug: str, title: str, body: str) -> None:
    path = wiki / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _FM.format(type=_TYPE[slug.split("/")[0]], title=title).replace(
            "related: []", f'related: []\nsources: ["源文件-{slug.rsplit("/", 1)[-1]}.md"]'
        )
        + f"# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def _seed_wiki(tmp: Path) -> Path:
    wiki = tmp / "wiki"
    wiki.mkdir(exist_ok=True)
    _page(wiki, "concepts/a", "甲", "## 甲主题\n\n甲的内容足够长，描述一个明确的主题，供合并使用。")
    _page(wiki, "concepts/b", "乙", "## 乙主题\n\n乙的内容足够长，与甲相邻但独立成页，应当合并。")
    _page(wiki, "concepts/c", "丙", "丙先讨论一些别的事情，然后引用了 [[concepts/b|乙]] 这一页的内容展开。")
    (wiki / "index.md").write_text(
        "- [[concepts/a]] — [concept] concepts/a.md — 甲 — s\n"
        "- [[concepts/b]] — [concept] concepts/b.md — 乙 — s\n"
        "- [[concepts/c]] — [concept] concepts/c.md — 丙 — s\n",
        encoding="utf-8",
    )
    return wiki


def _env(tmp: Path) -> tuple[JobService, JobWorker, Path]:
    wiki = _seed_wiki(tmp)
    materials = tmp / "materials"
    materials.mkdir(exist_ok=True)
    service = make_job_service(
        tmp / "ws",
        wiki_dir=wiki,
        sync_state=SyncState(tmp / "ws" / "watch" / "state.json"),
        materials_dir=materials,
    )
    handler = WikiOpsHandler(None, wiki_dir=wiki, source_records_dir=tmp / "prov", git=None)
    worker = JobWorker(service)
    handler.register_jobs(worker)
    return service, worker, wiki


def _merge_unit() -> dict:
    return {
        "in_pages": ["concepts/a", "concepts/b"],
        "out": [{"slug": "concepts/a", "intent": "甲乙合并页", "polish": True}],
        "reason": "重复主题",
    }


def _fake_route(assignment_builder):
    async def fake(llm, unit, sections):
        return assignment_builder(unit, sections), {}

    return fake


def _all_to_first(unit, sections):
    return {s.id: unit.out[0].slug for s in sections}


async def _fake_rewrite(llm, **kw):
    draft = kw["draft"]
    return _FM.format(type="concept", title="甲") + f"# 甲\n\n{draft}\n\n合并后的内容仍然足够长，可以通过质量检查。\n"


# —— 提交口：批形状与闸 ——


def test_submit_maintenance_enqueues_units_and_tail_links(tmp_path: Path):
    service, _worker, _wiki = _env(tmp_path)
    jobs = service.submit_maintenance([_merge_unit()])
    kinds = [j.kind for j in jobs]
    assert kinds[0] == Kind.RESTRUCTURE and kinds.count(Kind.LINK) == 2
    # 批尾范围 = 产出页(a) ∪ 消失页(b)的入链页(c)
    assert {j.payload["slug"] for j in jobs if j.kind == Kind.LINK} == {"concepts/a", "concepts/c"}
    assert len({j.payload["batch"] for j in jobs}) == 1, "整批同一 batch，revert-batch 一笔撤"


def test_submit_maintenance_blocked_by_other_writes(tmp_path: Path):
    service, _worker, _wiki = _env(tmp_path)
    service.submit(kind=Kind.COMPILE, resource="/x", mode="sync")
    with pytest.raises(PipelineBusy):
        service.submit_maintenance([_merge_unit()])


def test_restructure_self_clash_raises_dedicated_error(tmp_path: Path):
    service, _worker, _wiki = _env(tmp_path)
    service.submit(kind=Kind.RESTRUCTURE, resource="restructure:b1:0", mode="manual")
    with pytest.raises(RestructureInProgress):
        service.submit_maintenance([_merge_unit()])


def test_submit_maintenance_blocked_by_baseline_lag(tmp_path: Path):
    service, _worker, _wiki = _env(tmp_path)
    (tmp_path / "materials" / "new.md").write_text("未同步" * 10, encoding="utf-8")
    with pytest.raises(SyncBaselineLag):
        service.submit_maintenance([_merge_unit()])


def test_submit_rejects_unit_conflicting_with_disk(tmp_path: Path):
    service, _worker, _wiki = _env(tmp_path)
    bad = {  # out 撞未被本单元消费的现存页
        "in_pages": ["concepts/a"],
        "out": [{"slug": "concepts/c", "intent": "覆盖"}],
        "reason": "x",
    }
    with pytest.raises(rs.UnitError):
        service.submit_maintenance([bad])


def test_submit_link_batch_validates_roster(tmp_path: Path):
    service, _worker, _wiki = _env(tmp_path)
    with pytest.raises(ValueError):
        service.submit_link_batch(slugs=["concepts/nope"])
    jobs = service.submit_link_batch(slugs=["concepts/c"])
    assert [j.kind for j in jobs] == [Kind.LINK]


def test_resolve_drops_unit_sharing_page(tmp_path: Path):
    wiki = _seed_wiki(tmp_path)
    existing = set(all_content_slugs(wiki))
    u1 = rs.Unit(in_pages=["concepts/a", "concepts/b"], out=[rs.OutPage(slug="concepts/a", intent="合并")])
    u2 = rs.Unit(in_pages=["concepts/b"], out=[])  # b 已被 u1 消费
    clean, dropped = rs.resolve_unit_conflicts([u1, u2], existing)
    assert clean == [u1] and dropped and dropped[0][0] == u2


def test_take_conservation_rejects_unplaceable_section(tmp_path: Path):
    """take 未覆盖且无待分配页——守恒在 route_unit 单点拒绝（不重复预判）。"""
    wiki = _seed_wiki(tmp_path)
    sections = page_sections(wiki / "concepts/b.md", "concepts/b")
    assert len(sections) == 2, "页首 + 乙主题"
    unit = rs.Unit(
        in_pages=["concepts/b"],
        out=[rs.OutPage(slug="concepts/b", take=[rs.Take(from_slug="concepts/b", sections=["乙主题"])])],
    )
    with pytest.raises(rs.RouteError, match="无处安放"):
        asyncio.run(route_mod.route_unit(None, unit, sections))


# —— 执行体：五段落盘 ——


def test_restructure_merge_end_to_end(tmp_path: Path, monkeypatch):
    service, worker, wiki = _env(tmp_path)
    monkeypatch.setattr(plan_mod, "route_unit", _fake_route(_all_to_first))
    monkeypatch.setattr(wiki_ops_mod, "rewrite_unit_page", _fake_rewrite)

    async def empty_plan(llm, wiki_dir, slug):
        return []

    monkeypatch.setattr(wiki_ops_mod, "plan_link_fixes", empty_plan)
    jobs = service.submit_maintenance([_merge_unit()])

    def pump(n: int):
        for _ in range(n):
            asyncio.run(worker.run_once())

    pump(1)  # 单元 job
    unit_row = service.get(jobs[0].id)
    assert unit_row.status == "succeeded", unit_row.error
    assert unit_row.stage == "done"
    merged = (wiki / "concepts/a.md").read_text(encoding="utf-8")
    assert "乙主题" in merged and not (wiki / "concepts/b.md").exists()
    # 溯源并集：合并页 sources 含全部 in 页的来源文件（真实执行轮抓出的缺陷）
    assert "源文件-a.md" in merged and "源文件-b.md" in merged
    # 消失页的入链被机械转纯文本（保留显示文字），index 条目同步移除
    c_text = (wiki / "concepts/c.md").read_text(encoding="utf-8")
    assert "[[concepts/b" not in c_text and "乙" in c_text
    assert "[[concepts/b]]" not in (wiki / "index.md").read_text(encoding="utf-8")

    pump(2)  # 批尾两个 link（plan_link_fixes 未假件→需要 monkeypatch 为空清单）
    for j in jobs[1:]:
        assert service.get(j.id).status == "succeeded", service.get(j.id).error


def test_restructure_route_violation_fails_without_disk_change(tmp_path: Path, monkeypatch):
    service, worker, wiki = _env(tmp_path)

    async def dropping_route(llm, unit, sections):
        # 真 route_unit + 只分一章的假 LLM → 守恒校验抛 RouteError
        payload = {"assign": [{"section": sections[0].id, "to": unit.out[0].slug}]}
        return await route_mod.route_unit(_LLM(payload), unit, sections)

    monkeypatch.setattr(plan_mod, "route_unit", dropping_route)
    jobs = service.submit_maintenance([_merge_unit()])
    asyncio.run(worker.run_once())
    row = service.get(jobs[0].id)
    assert row.status == "failed" and "分配" in row.error
    assert (wiki / "concepts/b.md").exists(), "落盘前失败：盘面原样"


def test_single_page_rewrite_missing_settles_unit_missing(tmp_path: Path, monkeypatch):
    service, worker, wiki = _env(tmp_path)
    monkeypatch.setattr(plan_mod, "route_unit", _fake_route(_all_to_first))
    unit = {
        "in_pages": ["concepts/b"],
        "out": [{"slug": "concepts/b", "intent": "改写", "polish": True}],
        "reason": "改写",
    }
    jobs = service.submit_maintenance([unit])
    (wiki / "concepts/b.md").unlink()  # 排队期间消失
    asyncio.run(worker.run_once())
    row = service.get(jobs[0].id)
    assert row.status == "succeeded" and row.error == ""


def test_delete_unit_end_to_end(tmp_path: Path, monkeypatch):
    """删除单元（out 空）：路由短路、页面消失、入链转纯文本、index 清除。"""
    service, worker, wiki = _env(tmp_path)

    async def empty_plan(llm, wiki_dir, slug):
        return []

    monkeypatch.setattr(wiki_ops_mod, "plan_link_fixes", empty_plan)
    unit = {"in_pages": ["concepts/b"], "out": [], "reason": "空壳页删除"}
    jobs = service.submit_maintenance([unit])
    while service.count_in_flight() > 0:
        asyncio.run(worker.run_once())
    row = service.get(jobs[0].id)
    assert row.status == "succeeded", row.error
    assert not (wiki / "concepts/b.md").exists()
    c_text = (wiki / "concepts/c.md").read_text(encoding="utf-8")
    assert "[[concepts/b" not in c_text and "乙" in c_text
    assert "[[concepts/b]]" not in (wiki / "index.md").read_text(encoding="utf-8")


# —— link 执行 ——


def test_handle_link_applies_valid_fixes(tmp_path: Path, monkeypatch):
    service, worker, wiki = _env(tmp_path)

    async def fake_plan(llm, wiki_dir, slug):
        return [{"find": "这一页的内容展开", "replace": "并参见 [[concepts/a|甲]] 的合并后内容"}]

    monkeypatch.setattr(wiki_ops_mod, "plan_link_fixes", fake_plan)
    jobs = service.submit_link_batch(slugs=["concepts/c"])
    asyncio.run(worker.run_once())
    row = service.get(jobs[0].id)
    assert row.status == "succeeded", row.error
    assert "[[concepts/a|甲]]" in (wiki / "concepts/c.md").read_text(encoding="utf-8")


def test_handle_link_noop_settles_linked(tmp_path: Path, monkeypatch):
    service, worker, wiki = _env(tmp_path)

    async def empty_plan(llm, wiki_dir, slug):
        return []

    monkeypatch.setattr(wiki_ops_mod, "plan_link_fixes", empty_plan)
    before = (wiki / "concepts/c.md").read_text(encoding="utf-8")
    jobs = service.submit_link_batch(slugs=["concepts/c"])
    asyncio.run(worker.run_once())
    row = service.get(jobs[0].id)
    assert row.status == "succeeded"
    assert (wiki / "concepts/c.md").read_text(encoding="utf-8") == before, "空操作不写盘"


def test_apply_link_fixes_rejects_bad_items():
    content = "前文 目标词 后文，重复 目标词 两处。"
    fixes = [
        {"find": "目标词", "replace": "[[concepts/a|目标词]]"},  # 不唯一
        {"find": "后文", "replace": "[[missing/x|后文]]"},  # 目标不在名册
        {"find": "前文", "replace": "[[self|前文]]"},  # 自链
        {"find": "重复 目标词 两处", "replace": "重复 [[concepts/a]] 两处"},  # 裸链缺显示文字
    ]
    new, applied, skipped = apply_link_fixes(
        content, fixes, valid_slugs={"concepts/a"}, self_slug="self"
    )
    assert applied == [] and len(skipped) == 4 and new == content


def test_settlement_values_registered():
    assert Settlement.APPLIED == "applied"
    assert Settlement.LINKED == "linked"
    assert Settlement.UNIT_MISSING == "unit_missing"


# —— 成文形状校验与骨架兜底（真实模型跑出的两类失败） ——


def test_rewrite_links_for_vanished_strips_related(tmp_path: Path):
    """related 指向消失页必须整项移除——转纯文本会留死残项（真实扫描闸抓到）。"""
    from wiki_agent.compiler.restructure.apply import rewrite_links_for_vanished

    wiki = _seed_wiki(tmp_path)
    c = wiki / "concepts/c.md"
    c.write_text(
        c.read_text(encoding="utf-8").replace(
            "related: []",
            'related: ["[[concepts/b|乙]]", "concepts/b", "[[concepts/a]]"]',
        ),
        encoding="utf-8",
    )
    rewrite_links_for_vanished(wiki, ["concepts/b"])
    fm, body = split_frontmatter(c.read_text(encoding="utf-8"))
    assert str(fm["related"]) == '["[[concepts/a]]"]'
    assert "[[concepts/b" not in body and "乙" in body  # 正文 alias 转纯文本


def test_fix_fence_closes_trailing_open_block():
    """真实模型高频症状：正文末尾代码块缺闭合——文末机械补裸 ```。"""
    from wiki_agent.wiki.normalize import fix_markdown_fence

    open_page = "---\ntype: concept\ntitle: t\n---\n# t\n\n```python\nx = 1\n"
    fixed = fix_markdown_fence(open_page)
    assert fixed.count("```") % 2 == 0 and fixed.rstrip().endswith("```")
    good = "---\ntype: concept\ntitle: t\n---\n# t\n\n```python\nx = 1\n```\n"
    assert fix_markdown_fence(good) == good.strip()
    two_opens = "---\ntype: concept\ntitle: t\n---\n# t\n\n```python\nx = 1\n```python\ny = 2\n"
    assert count_unclosed_fences(fix_markdown_fence(two_opens)) == 0, "双开零闭须补两个闭合"


def test_fill_frontmatter_synthesizes_when_absent():
    """拆分新页模型常返回纯正文片段：整页 frontmatter 由骨架合成。"""
    from wiki_agent.application.wiki_ops import _fill_frontmatter

    body = "# 标题\n\n正文足够长可以过质量检查的要求，描述明确主题。\n"
    out = _fill_frontmatter(body, {"type": "topic", "title": "T", "summary": "摘要", "goal": "目标"})
    fm, rest = split_frontmatter(out)
    assert fm["type"] == "topic" and fm["title"] == "T" and fm["summary"] == "摘要"
    assert rest == body


def test_gist_skips_code_blocks_and_takes_two_paragraphs():
    sec = Section(
        slug="concepts/x",
        heading="用法",
        body="```python\nx = [1]\nx += [2]\n```\n\n原地合并跳过右侧可迭代并逐元素追加，不创建新列表。\n\n第二段落补充共享引用下所有别名都可见的效果说明。",
    )
    g = sec.gist(160)
    assert not g.startswith("```") and "原地合并" in g and "第二段落" in g


def test_gist_top_includes_lead_prose():
    sec = Section(slug="concepts/x", heading="", body="# 标题\n\n导语一句定调。\n\n导语第二句展开。")
    g = sec.gist(160)
    assert g.startswith("导语一句") and "导语第二句" in g


def test_route_outline_degrades_beyond_threshold():
    sections = [
        Section(slug=f"concepts/p{i}", heading="节", body="甲" * 200 + "\n\n" + "乙" * 200) for i in range(21)
    ]
    lines = prompts.route_outline(sections).splitlines()
    assert len(lines) == 21
    # 退化档：每节 gist 不超过短预算（40）+ 拼接符
    for ln in lines:
        assert len(ln.split(" | ")[-1]) <= prompts.ROUTE_GIST_SHORT_CHARS + 3
    normal = prompts.route_outline(sections[:5])
    assert any(len(ln.split(" | ")[-1]) > 100 for ln in normal.splitlines())
    good = _FM.format(type="concept", title="甲") + "# 甲\n\n正文\n\n```python\nx = 1\n```\n"
    assert prompts.check_rewrite_page(good) == (True, "")
    no_summary = "---\ntype: concept\ntitle: \"甲\"\ngoal: \"g\"\n---\n# 甲\n\n正文\n"
    ok, reason = prompts.check_rewrite_page(no_summary)
    assert not ok and "summary" in reason
    open_fence = _FM.format(type="concept", title="甲") + "# 甲\n\n```python\nx = 1\n"
    ok, reason = prompts.check_rewrite_page(open_fence)
    assert not ok and "代码块未闭合" in reason
    # 双开零闭：奇偶计数判"已闭合"的假阴性形态，括号配对判据必须抓住
    two_opens = _FM.format(type="concept", title="甲") + "# 甲\n\n```python\nx = 1\n```python\ny = 2\n"
    ok, _ = prompts.check_rewrite_page(two_opens)
    assert not ok


def test_fill_frontmatter_backfills_skeleton_without_overwrite():
    from wiki_agent.application.wiki_ops import _fill_frontmatter

    text = "---\ntype: concept\ntitle: \"甲\"\nsummary: \"\"\nrelated: []\n---\n# 甲\n"
    out = _fill_frontmatter(text, {"summary": "骨架摘要", "goal": "骨架目标", "title": "不该用"})
    fm, _ = split_frontmatter(out)
    assert fm["summary"] == "骨架摘要" and fm["goal"] == "骨架目标"
    assert fm["title"] == "甲" and str(fm["related"]) == "[]"  # 已有值不覆盖、其余 key 不动
    ok = _FM.format(type="concept", title="甲") + "# 甲\n\n正文\n"
    assert _fill_frontmatter(ok, {"summary": "不该出现"}) == ok
