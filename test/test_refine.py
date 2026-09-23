"""refine 管线测试：prompt 契约、模式接线、只更新当前页的兜底过滤、全链集成。

无需真实 LLM：集成测试按阶段脚本化响应，验证 refine 模式的完整
数据流（index 排除自身 → 润色师 plan → 兜底过滤 → 只更新自己 →
不存 source 档案）。

直接运行:  .venv/bin/python test/test_refine.py
pytest 运行: pytest test/test_refine.py（纯同步测试函数 + asyncio.run）
"""

import asyncio
import tempfile
from pathlib import Path

from wiki_agent.compiler.integration.checks import check_plan_json
from wiki_agent.compiler.models import (
    Disposition,
    ExtractResult,
    IntegrationPlan,
    PageTarget,
)
from wiki_agent.compiler.prompts import compile as cp
from wiki_agent.compiler.prompts import refine as rp
from wiki_agent.compiler.workflows.ingest import CompilePipeline
from wiki_agent.compiler.workflows.refine import (
    build_index_excluding_self,
    refine_pages,
)
from wiki_agent.conversation import LLMResponse
from wiki_agent.wiki.rules import check_page_output

# unit: prompt 契约与校验


def test_refine_plan_is_independent_curator():
    """refine 的 plan 是独立实现（润色师），不继承 compile 的策展人。"""
    ext = ExtractResult(source_identity="concepts/x", document_summary="摘要")
    sys_text = rp.plan_system()
    user_text = rp.plan_user(ext, "分析文本", current_page="concepts/x")
    assert "润色师" in sys_text
    assert "只更新当前页面自己" in sys_text
    assert "concepts/x" in user_text  # 动态身份在 user 段
    # compile 的策展人文本不被继承
    c_sys = cp.plan_system()
    c_user = cp.plan_user(ext, "分析文本", index_content="- [[concepts/y]] — y")
    assert "润色师" not in c_sys
    assert "策展人" in c_sys
    assert "- [[concepts/y]]" in c_user  # index 动态数据在 user 段


def test_mode_contract_constants():
    """模式契约常量——prompt 文本与允许的操作同文件声明。"""
    assert rp.ALLOWED_DISPOSITIONS == {"update"}
    assert cp.ALLOWED_DISPOSITIONS == {"new", "update"}


def test_refine_prompt_requires_evidence_or_noop():
    text = rp.plan_system()
    assert "材料明确支持时才补内容" in text
    assert "合法 no-op" in text
    assert "不得凭常识补齐" in text
    assert "页面变长" in text


def test_check_blocks_new_in_refine():
    """refine 契约: new 被校验拦截（retry 报错），update 放行。

    注意 wiki_path 必须带合法目录前缀：路由校验在 disposition 校验
    之前拦截非法目录。
    """
    new_plan = (
        '{"page_targets": [{"wiki_path": "concepts/a.md", "title": "A", '
        '"disposition": "new", "reason": "x"}]}'
    )
    ok, err = check_plan_json(new_plan, allowed_dispositions=rp.ALLOWED_DISPOSITIONS)
    assert not ok and "update" in err

    upd_plan = (
        '{"page_targets": [{"wiki_path": "concepts/a.md", "title": "A", '
        '"disposition": "update", "reason": "x"}]}'
    )
    ok, err = check_plan_json(upd_plan, allowed_dispositions=rp.ALLOWED_DISPOSITIONS)
    assert ok, err

    # compile 契约: new 现在必须带 page_type（单一权威契约）
    ok, err = check_plan_json(new_plan, allowed_dispositions=cp.ALLOWED_DISPOSITIONS)
    assert not ok and "page_type" in err


def test_new_target_requires_page_type_consistent_with_directory():
    """new 页面必须由 plan 决策 page_type 且与目录一致（单一权威契约）。

    回归: plan 路由 entities/、generate 写 type=concept 的跨阶段脱节——
    类型与目录由同一次 plan 决策产出，generate 不再自行判断。
    """
    # 缺 page_type → 拒绝
    missing = (
        '{"page_targets": [{"wiki_path": "concepts/a.md", "title": "A", '
        '"disposition": "new", "reason": "x"}]}'
    )
    ok, err = check_plan_json(missing, allowed_dispositions=cp.ALLOWED_DISPOSITIONS)
    assert not ok and "page_type" in err

    # 类型与目录不一致 → 拒绝（entities/ 目录 + concept 类型）
    mismatched = (
        '{"page_targets": [{"wiki_path": "entities/a.md", "title": "A", '
        '"disposition": "new", "page_type": "concept", "reason": "x"}]}'
    )
    ok, err = check_plan_json(mismatched, allowed_dispositions=cp.ALLOWED_DISPOSITIONS)
    assert not ok and "目录不一致" in err

    # 一致 → 放行
    consistent = (
        '{"page_targets": [{"wiki_path": "concepts/a.md", "title": "A", '
        '"disposition": "new", "page_type": "concept", "reason": "x"}]}'
    )
    ok, err = check_plan_json(consistent, allowed_dispositions=cp.ALLOWED_DISPOSITIONS)
    assert ok, err

    # update 目标不需要 page_type（沿用已有页面 type）
    ok, err = check_plan_json(
        '{"page_targets": [{"wiki_path": "entities/a.md", "title": "A", '
        '"disposition": "update", "reason": "x"}]}',
        allowed_dispositions=cp.ALLOWED_DISPOSITIONS,
    )
    assert ok, err


def test_metadata_injection_overrides_type_from_plan():
    """系统注入: plan 的 page_type 强制覆盖 generate 写的 type。"""
    from wiki_agent.wiki.normalize import inject_metadata

    content = '---\ntype: concept\ntitle: A\nsummary: s\nsources: ["old.md"]\n---\n# A\n正文'
    out = inject_metadata(
        content,
        source_identity="new.md",
        today="2026-09-10",
        existing=None,
        page_type="entity",
    )
    assert "type: entity" in out.split("---")[1]
    assert "type: concept" not in out.split("---")[1]
    # 无 page_type 时不动 type（update 目标）
    out2 = inject_metadata(content, source_identity="new.md", today="2026-09-10", existing=None)
    assert "type: concept" in out2.split("---")[1]


def test_page_output_requires_frontmatter_fields():
    """页面输出总闸门: type/title/summary/goal 必填，缺失即 retry 报错。"""
    ok_page = (
        "---\n"
        'type: concept\ntitle: "X"\nsummary: "概述"\n'
        'goal: "讲清 X"\n'
        "---\n# X\n\n正文 [[concepts/y|Y页]]。\n"
    )
    ok, err = check_page_output(ok_page)
    assert ok, err

    no_goal = ok_page.replace('goal: "讲清 X"\n', "")
    ok, err = check_page_output(no_goal)
    assert not ok and "goal" in err

    no_fm = "# X\n\n没有 frontmatter。\n"
    ok, err = check_page_output(no_fm)
    assert not ok and "frontmatter" in err


def test_page_output_requires_body_content():
    """正文存在性进闸门（与 quality error 同语义）: 无正文/只有标题 → retry。"""
    no_body = '---\ntype: concept\ntitle: "X"\nsummary: "概述"\ngoal: "讲清 X"\n---\n'
    ok, err = check_page_output(no_body)
    assert not ok and "无正文" in err

    only_title = (
        '---\ntype: concept\ntitle: "X"\nsummary: "概述"\ngoal: "讲清 X"\n---\n# X\n\n## 小节\n'
    )
    ok, err = check_page_output(only_title)
    assert not ok and "只有标题" in err


def test_refine_inherits_shared_prompts():
    """除 plan 外所有 prompt 与 compile 同源（import 即继承）。"""
    assert rp.search_system is cp.search_system
    assert rp.search_user is cp.search_user
    assert rp.analyze_system is cp.analyze_system
    assert rp.analyze_user is cp.analyze_user
    assert rp.new_page_system is cp.new_page_system
    assert rp.new_page_user is cp.new_page_user
    assert rp.update_system is cp.update_system
    assert rp.update_user is cp.update_user
    assert rp.chunk_system is cp.chunk_system
    assert rp.chunk_user is cp.chunk_user
    assert rp.synthesis_prompt is cp.synthesis_prompt
    assert rp.rolling_system is cp.rolling_system
    assert rp.rolling_user is cp.rolling_user
    assert rp.plan_system is not cp.plan_system
    assert rp.plan_user is not cp.plan_user


def test_prompt_cache_separation():
    """prompt cache 拆分契约: 固定段无动态数据，动态段承载全部数据。

    静态前移/动态后移——固定段跨文件共享前缀，动态段变化在尾部。
    """
    ext = ExtractResult(source_identity="concepts/x", document_summary="摘要X")

    # search: 动态数据（doc 摘要/index）只在 user 段
    assert "摘要X" not in cp.search_system()
    assert "摘要X" in cp.search_user(ext, "- [[concepts/y]] — y")

    # analyze: 候选页 meta 只在 user 段（system 不含"候选页面"数据标题）
    assert "## 候选页面" not in cp.analyze_system()
    user = cp.analyze_user(ext, "[[concepts/y]] 元信息")
    assert "摘要X" in user and "[[concepts/y]]" in user

    # chunk: 位置/原文只在 user 段（system 不含 chunk 身份）
    from wiki_agent.compiler.models import SourceChunk

    ck = SourceChunk(content="原文ABC", index=2, total=5, heading_path="x/y")
    assert "原文ABC" not in cp.chunk_system()
    assert "原文ABC" in cp.chunk_user(ck)
    assert "第 3/5 个片段" in cp.chunk_user(ck)

    # plan: 分析文本/index 只在 user 段
    assert "分析文本XYZ" not in cp.plan_system()
    assert "分析文本XYZ" in cp.plan_user(ext, "分析文本XYZ")


# unit: refine 编排与 index 排除


def _make_wiki(tmp: Path) -> Path:
    """构造最小 wiki 结构，返回 wiki 目录。"""
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "entities").mkdir()
    (wiki / "topics").mkdir()
    (wiki / "sources").mkdir()  # 不应被 refine_pages 收集
    (wiki / "index.md").write_text(
        "- [[concepts/x]] — [concept] concepts/x.md — X\n"
        "- [[concepts/y]] — [concept] concepts/y.md — Y\n"
        "- [[entities/e]] — [entity] entities/e.md — E\n",
        encoding="utf-8",
    )
    (wiki / "purpose.md").write_text("个人知识库\n", encoding="utf-8")
    (wiki / "schema.md").write_text("# schema\n", encoding="utf-8")
    return wiki


def test_index_reader_excludes_self():
    """index 排除自身条目，其他条目保留。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    reader = build_index_excluding_self(wiki)
    view = reader(wiki / "concepts" / "x.md")
    assert "[[concepts/x]]" not in view
    assert "[[concepts/y]]" in view
    assert "[[entities/e]]" in view
    # index 缺失 → 空串（首次运行）
    reader2 = build_index_excluding_self(tmp / "nonexistent")
    assert reader2(tmp / "f.md") == ""


def test_refine_pages_scope():
    """refine 输入范围 = 三目录，sources/系统文件排除。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    (wiki / "concepts" / "x.md").write_text("x", encoding="utf-8")
    (wiki / "concepts" / "y.md").write_text("x", encoding="utf-8")
    (wiki / "entities" / "e.md").write_text("x", encoding="utf-8")
    (wiki / "sources" / "s.md").write_text("x", encoding="utf-8")
    pages = refine_pages(wiki)
    rels = {str(p.relative_to(wiki)) for p in pages}
    assert rels == {"concepts/x.md", "concepts/y.md", "entities/e.md"}


# unit: 模式接线与兜底过滤


class _MockLLM:
    model_id = "mock"


def test_mode_wiring():
    """mode 一处声明——四阶段按模式组装（工厂），refine 用 PolisherPlanner。"""
    from wiki_agent.compiler.integration.plan import CuratorPlanner, PolisherPlanner

    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    p = CompilePipeline(llm=_MockLLM(), vlm=object(), wiki_dir=wiki, mode="refine")
    assert p._mode == "refine"
    assert p._save_sources is False
    assert p._extractor._save_sources is False
    assert isinstance(p._integrator._planner, PolisherPlanner)
    assert p._integrator._planner.needs_current_page is True
    assert p._index_reader is not None

    p2 = CompilePipeline(llm=_MockLLM(), vlm=object(), wiki_dir=wiki)
    assert p2._mode == "compile"
    assert p2._save_sources is True
    assert isinstance(p2._integrator._planner, CuratorPlanner)
    assert p2._integrator._planner.needs_current_page is False
    assert p2._index_reader is None

    try:
        CompilePipeline(llm=_MockLLM(), vlm=object(), wiki_dir=wiki, mode="bad")
        assert False, "非法 mode 应报错"
    except ValueError:
        pass


def test_filter_refine_targets_keeps_only_self():
    """只拿不放兜底（PolisherPlanner）: 保留 self-update，丢弃 other-update 与 new。"""
    from wiki_agent.compiler.integration.plan import PolisherPlanner
    from wiki_agent.compiler.prompts import refine as rp

    tmp = Path(tempfile.mkdtemp())
    planner = PolisherPlanner(_MockLLM(), _make_wiki(tmp), rp)

    plan = IntegrationPlan(
        page_targets=[
            PageTarget("concepts/self.md", "S", Disposition.UPDATE, "x"),
            PageTarget("concepts/other.md", "O", Disposition.UPDATE, "x"),
            PageTarget("concepts/newpage.md", "N", Disposition.NEW, "x"),
        ]
    )
    planner._filter_self_updates(plan, "concepts/self")
    assert [t.wiki_path for t in plan.page_targets] == ["concepts/self.md"]


def test_filter_refine_targets_all_violations_turn_noop():
    """全部违规 → 空 targets（execute 会按 noop 处理）。"""
    from wiki_agent.compiler.integration.plan import PolisherPlanner
    from wiki_agent.compiler.prompts import refine as rp

    tmp = Path(tempfile.mkdtemp())
    planner = PolisherPlanner(_MockLLM(), _make_wiki(tmp), rp)
    plan = IntegrationPlan(
        page_targets=[
            PageTarget("concepts/other.md", "O", Disposition.UPDATE, "x"),
        ]
    )
    planner._filter_self_updates(plan, "concepts/self")
    assert plan.page_targets == []


def test_search_paths_contract_lenient_both_shapes():
    """json_object 新契约 {"paths":[...]}；顶层数组（旧契约/端点忽略参数）宽容。"""
    from wiki_agent.compiler.integration.checks import check_paths_json
    from wiki_agent.compiler.integration.parse import parse_search_result

    assert check_paths_json('{"paths": ["concepts/y.md"]}') == (True, "")
    assert check_paths_json('["concepts/y"]')[0] is True
    assert check_paths_json('{"paths": "不是数组"}')[0] is False
    assert check_paths_json('{"unexpected": 1}')[0] is False
    assert check_paths_json("随便写点什么的")[0] is False

    assert parse_search_result('{"paths": ["wiki/concepts/y"]}') == ["concepts/y.md"]
    assert parse_search_result('["concepts/y"]') == ["concepts/y.md"]
    assert parse_search_result('{"paths": "notalist"}') == []


# integration: ingest_one 全链（脚本化 LLM）


class ScriptedLLM:
    """按 prompt 内容路由的脚本化 LLM——记录每次调用的 system 内容。"""

    model_id = "mock"
    _PAGE = (
        "---\n"
        'type: concept\ntitle: "X"\nsummary: "概述"\ntags: [x]\n'
        'goal: "讲清 X 概念及其用法"\n'
        'gaps: "未覆盖的内容"\n'
        "---\n"
        "# X\n\n"
        "正文内容足够长，超过八十个字符，包含 [[concepts/y|Y页]] 的"
        "交叉引用，用于验证 related 提取和页面更新。\n"
    )

    def __init__(self, plan_json: str):
        self._plan_json = plan_json
        self.calls: list[str] = []

    async def async_invoke(
        self,
        messages,
        tools=None,
        max_tokens=None,
        temperature=0.5,
        extra_body=None,
        response_format=None,
    ):
        # chunk/synthesis 是 system+user 双消息——扫描全部消息，
        # 只读 messages[-1] 会拿到 user 的 chunk 正文，该分支不会命中
        content = "\n".join(m.content for m in messages)
        self.calls.append(content)
        if "精读助手" in content:
            return LLMResponse(content="片段摘要：X 概念与 Y 相关。")
        if "基于一份文档的所有片段摘要" in content:
            return LLMResponse(content="文档摘要：讲 X 概念，与 Y 相关。")
        if "你是 Wiki 相关度过滤器" in content:
            return LLMResponse(content='{"paths": ["concepts/y"]}')
        if "你是知识库的关系分析师" in content:
            return LLMResponse(
                content=(
                    "自由分析：本文档与候选页 Y 相关。\n"
                    "```json\n"
                    '{"entities": [], "concepts": [], "relationships": ['
                    '{"from": "current-doc", "to": "concepts/y", '
                    '"relation": "extends", "detail": "本文档补充 Y 页"}]}'
                    "\n```"
                )
            )
        if "润色师" in content:
            return LLMResponse(content=self._plan_json)
        if "你是知识库的编辑" in content:
            return LLMResponse(content=self._PAGE)
        return LLMResponse(content="")


def _page_content(wiki: Path, rel: str) -> str:
    return (wiki / rel).read_text(encoding="utf-8")


def _run_refine_ingest(plan_json: str):
    """构造 refine 环境并 ingest concepts/x.md，返回 (wiki, llm, outcome)。"""
    tmp = Path(tempfile.mkdtemp())
    wiki = _make_wiki(tmp)
    (wiki / "concepts" / "x.md").write_text(
        '---\ntype: concept\ntitle: "X"\nsummary: "旧概述"\n---\n# X\n\n旧正文。\n',
        encoding="utf-8",
    )
    (wiki / "concepts" / "y.md").write_text(
        '---\ntype: concept\ntitle: "Y"\n---\n# Y\n\nY 的正文，不会被 refine 修改。\n',
        encoding="utf-8",
    )
    (wiki / "entities" / "e.md").write_text(
        '---\ntype: entity\ntitle: "E"\n---\n# E\n\nE 的正文。\n',
        encoding="utf-8",
    )
    y_before = _page_content(wiki, "concepts/y.md")
    index_before = (wiki / "index.md").read_text(encoding="utf-8")

    llm = ScriptedLLM(plan_json)
    pipeline = CompilePipeline(llm=llm, vlm=object(), wiki_dir=wiki, mode="refine")

    from wiki_agent.documents.loader import DataLoader

    loader = DataLoader()
    summary = loader.load([wiki / "concepts" / "x.md"])
    assert summary.files, "加载失败"

    outcome = asyncio.run(pipeline.ingest_one(summary.files[0]))
    return {
        "wiki": wiki,
        "llm": llm,
        "outcome": outcome,
        "y_before": y_before,
        "index_before": index_before,
    }


def test_ingest_refine_full_chain():
    """全链集成: self-update 落盘、other-update 被丢弃、不存 source 页。"""
    plan_json = (
        '{"page_targets": ['
        '{"wiki_path": "wiki/concepts/x", "title": "X", '
        '"disposition": "update", "reason": "补充交叉引用"}, '
        '{"wiki_path": "wiki/concepts/y", "title": "Y", '
        '"disposition": "update", "reason": "并入内容"}]}'
    )
    r = _run_refine_ingest(plan_json)
    wiki, llm, outcome = r["wiki"], r["llm"], r["outcome"]

    # 阶段齐全: 均匀分配摘要(1 chunk 1 次) + synthesis(1) + search(1)
    #           + analyze(1) + plan(1) + update(仅 self 一次) = 6
    assert len(llm.calls) == 6, [c[:30] for c in llm.calls]
    search_call = next(c for c in llm.calls if "Wiki 相关度过滤器" in c)
    plan_call = next(c for c in llm.calls if "润色师" in c)
    update_calls = [c for c in llm.calls if "你是知识库的编辑" in c]
    # search 的 index 视图排除了自身
    assert "[[concepts/x]]" not in search_call
    assert "[[concepts/y]]" in search_call
    # plan 拿到润色师 prompt + 当前页身份
    assert "concepts/x" in plan_call
    # update 只发生一次（rogue other-update 被兜底丢弃，没进 execute）
    assert len(update_calls) == 1

    # 只拿不放: outcome 只剩 self-update，页面真的更新了
    assert not outcome.noop
    assert [t.wiki_path for t in outcome.plan.page_targets] == ["concepts/x.md"]
    x_after = _page_content(wiki, "concepts/x.md")
    assert "Y页" in x_after  # 新内容落盘
    assert "updated:" in x_after  # 系统字段注入
    assert 'related: ["[[concepts/y]]"]' in x_after  # related 代码提取
    # other 页分毫未动
    assert _page_content(wiki, "concepts/y.md") == r["y_before"]
    # 不存 source 档案页
    assert not (wiki / "sources").exists() or not list((wiki / "sources").iterdir())
    # index 不变（x 已在 index，无新条目）
    assert (wiki / "index.md").read_text(encoding="utf-8") == r["index_before"]


def test_ingest_refine_all_rogue_turns_noop():
    """plan 全是 other-update（契约放行但兜底过滤）→ noop，什么都不碰。"""
    plan_json = (
        '{"page_targets": ['
        '{"wiki_path": "wiki/concepts/y", "title": "Y", '
        '"disposition": "update", "reason": "并入内容"}]}'
    )
    r = _run_refine_ingest(plan_json)
    wiki, llm, outcome = r["wiki"], r["llm"], r["outcome"]

    assert outcome.noop is True
    # 没有 update 阶段的 LLM 调用（extract 2 + search + analyze + plan 后就停）
    update_calls = [c for c in llm.calls if "你是知识库的编辑" in c]
    assert len(update_calls) == 0
    # 两个页面都未动
    assert _page_content(wiki, "concepts/x.md") != ""  # x 原样（含"旧正文"）
    assert "旧正文" in _page_content(wiki, "concepts/x.md")
    assert _page_content(wiki, "concepts/y.md") == r["y_before"]


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception:
            failed += 1
            print(f"  ✗ {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
