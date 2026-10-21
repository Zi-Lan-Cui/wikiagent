"""compile 模式 prompt，compile 与 sync 路径共用同一套。

prompt 指令语言为英文以提升遵循度；所有产出文本的 prompt 均显式要求
"输出语言跟随资料语言"，防止指令语言把内容带偏。
"""

from __future__ import annotations

from wiki_agent.compiler.models import ExtractResult, PageTarget, SourceChunk

# prompt 内各内容段落的字符预算
_SEARCH_SUMMARY_CHARS = 8_000  # search: 文档摘要截断长度
_SEARCH_INDEX_CHARS = 15_000  # search: index 截断长度（按条目）
_ANALYZE_SUMMARY_CHARS = 8_000  # analyze: 文档摘要截断长度
_PLAN_ANALYSIS_CHARS = 12_000  # plan: 关系分析文本截断长度
_PLAN_SUMMARY_CHARS = 8_000  # plan: 文档摘要截断长度
_GEN_SUMMARY_CHARS = 15_000  # 新页面: 文档摘要截断长度
_UPDATE_EXISTING_CHARS = 10_000  # update: 已有页面截断长度
_UPDATE_SUMMARY_CHARS = 10_000  # update: 新信息截断长度

# rolling 模式全局摘要的目标长度（防膨胀）
DIGEST_TARGET_TOKENS = 1_500

# 附加在产出文本 prompt 末尾的语言锚——指令是英文，输出必须仍是资料的语言
_SOURCE_LANGUAGE = "\n- Write all output text in the same language as the source material; never translate into the language of these instructions."


def chunk_system() -> str:
    """chunk 摘要的固定段：角色与输出要求。

    位置/标题/原文等动态数据在 chunk_user；固定段在前，
    作为同一文件所有 chunk 共享的缓存前缀。

    Returns:
        system prompt 文本。
    """
    return (
        "You are a close-reading assistant for academic documents. Produce a"
        " faithful compressed summary of the given fragment.\n\n"
        "## Output requirements\n"
        "Summarize the fragment as a coherent natural-language paragraph that"
        " preserves its narrative logic and main point. No bullet lists, no"
        " section headings — just fluent prose."
        + _SOURCE_LANGUAGE
        + "\n\n## Anchors that must survive (woven into the prose, not listed)\n"
        "- Theme and conclusion: what the fragment is about, what it concludes\n"
        "- Key terms: terms defined or explained in depth, keep their original wording\n"
        "- Quantitative data: specific numbers, parameters, experimental results\n"
        "- Relationship cues: how the fragment connects to other concepts"
        " (contrast / dependency / extension)\n"
        "- Mark anything conflicting with common knowledge as {{可能矛盾}}\n\n"
        "## Constraints\n"
        "- Keep original technical terms and formula symbols; do not translate,"
        " do not rename proper nouns\n"
        "- No speculation, no outside knowledge — write only what actually"
        " appears in the fragment\n"
        "- Fold image caption information (if any) into the prose\n"
        "- Never inject common sense, training memory, or causality you find plausible\n"
        "- Omit numbers, dates, people, conclusions and causal links the text does"
        " not state explicitly\n"
        "- If the fragment lacks information, state '原文未说明' — do not fill gaps"
        " with outside knowledge"
    )


def chunk_user(chunk: SourceChunk) -> str:
    """chunk 摘要的动态段：位置信息 + 片段原文。

    Args:
        chunk: 待摘要片段。

    Returns:
        user prompt 文本。
    """
    pos = f"fragment {chunk.index + 1}/{chunk.total}"
    head = f", located at 『{chunk.heading_path}』" if chunk.heading_path else ""
    return f"Text to summarize: {pos}{head} (from {chunk.source_name}).\n\n{chunk.content}"


def synthesis_prompt() -> str:
    """文档级概述的 system prompt——合成阶段固定段。

    Returns:
        system prompt 文本。
    """
    return "\n".join(
        [
            "Based on all fragment summaries of one document, produce a"
            " document-level overview as natural-language prose; no fixed number"
            " of paragraphs is required."
            + _SOURCE_LANGUAGE,
            "",
            "## Use the section-structure cues",
            "Every fragment summary is labeled with its position in the original"
            " (heading path) and its index. Use these cues to reconstruct the"
            " document's section skeleton — the overview should follow how the"
            " original progresses, not just list fragments flat.",
            "",
            "## Content organization (coarse to refined)",
            "- State a document type (note / tutorial / paper / report) only when"
            " the text says so explicitly; otherwise just the concrete topic",
            "- Then unfold the core: which knowledge points or methods, and how"
            " they relate logically",
            "- Mention key terms or named entities only when the source defines,"
            " explains or repeatedly discusses them; never recommend follow-up"
            " directions from experience",
            "- Symbols in formulas, fields in parameter tables and their"
            " correspondences may be stated as facts when the source says so"
            " explicitly; if only strongly implied contextually, mark them as"
            " field conventions ('通常按该领域记号') — never as certain facts",
            "- Never soften an unfounded guess with '通常/一般/可能/往往'; omit"
            " what lacks support",
            "",
            "## What to highlight",
            "- Views, methods and data unique to this document — what sets it"
            " apart from similar writing",
            "- Concepts revisited from multiple angles — their central role here",
            "- Cross-links between knowledge points survive only when the"
            " fragments state them; never add links for convenience",
            "",
            "## What to downplay",
            "- Transitional statements, repeated argumentation, auxiliary"
            " examples — carry them naturally, do not expand",
            "- Existing image/table references: if the alt text already describes"
            " the content, a brief mention suffices",
            "",
            "## Error handling",
            "- When the content itself has an obvious known error (grammar/logic/"
            " misused term), annotate it where relevant: {原文有误}",
            "- Annotating an error does not negate the rest; do not skip correct"
            " content because of it",
            "",
            "## Misc",
            "- Keep original technical terms and proper nouns; do not force translation",
            "- When fragments contradict each other, mark {内部矛盾}",
            "- Every fact, term, number and conclusion must trace back to the"
            " given fragment summaries",
            "- Never supplement the summaries with common sense or training"
            " memory; omit what cannot be confirmed",
            "- Never infer from titles or heading names a conclusion the body"
            " does not state",
            "- Never turn the mere co-occurrence of fragments into causal,"
            " dependency or temporal order",
            "  Unless a fragment explicitly supports it, avoid background guesses"
            " like '可能涉及', '通常', '常见', '属于某领域', '未来可以'",
            "- Before outputting, check sentence by sentence: subject, numbers,"
            " terms, conclusions and causality each have fragment support",
            "- Even for a very short or structureless document, summarize the"
            " facts the source states; do not refuse due to low information",
            "- Never output process talk like '根据提供的片段摘要',"
            " '无法生成文档级概述', '请提供更多片段'",
            "- Never mention 'fragment summaries', 'input material' or your own"
            " analysis process; state the document's facts directly",
        ]
    )


def rolling_system() -> str:
    """滚动压缩的固定段——合并指令（跨文档共享缓存前缀）。

    Returns:
        system prompt 文本。
    """
    return (
        "## Merge instructions\n"
        "Fold the current fragment into the global digest and **output the"
        " fully rewritten digest** (not an increment — rewrite it every round):\n"
        f"1. Coherent narrative — keep the document's main thread and logical"
        " flow; no section structure\n"
        "2. No repetition of what is already in — the digest's semantics are"
        " 'say it once'\n"
        "3. New information folds in naturally — new terms, data and conclusions"
        " embedded in the prose, keeping the original wording\n"
        "4. Contradiction markers — when new content conflicts, keep both sides"
        " and tag {{矛盾}}\n"
        f"5. Length control — keep the global digest under {DIGEST_TARGET_TOKENS}"
        " tokens; when over budget, compress details of early fragments first,"
        " preserving the main thread, key terms and the newest fragments'"
        " details\n"
        "6. Output only the new global digest itself — no explanations or prefaces\n"
        "7. Never use outside knowledge or training memory to supplement what"
        " the current fragment and the existing digest do not explicitly support\n"
        "8. Keep only facts jointly supported by the current fragment and the"
        " existing digest; do not add anything the fragment does not state\n"
        "9. Do not assert causality, dependency or temporal order just because"
        " two concepts co-occur; if the text does not say it, do not infer it"
        + _SOURCE_LANGUAGE
    )


def rolling_user(
    chunk: SourceChunk,
    previous_digest: str,
    total: int,
) -> str:
    """滚动压缩的动态段：digest + 当前片段（每轮变化在尾部）。

    Args:
        chunk: 当前片段。
        previous_digest: 上一轮全局摘要。
        total: 片段总数。

    Returns:
        user prompt 文本。
    """
    pos = f"fragment {chunk.index + 1}/{total}"
    head = f"（{chunk.heading_path}）" if chunk.heading_path else ""
    return "\n\n".join(
        [
            f"## Previous global digest\n{previous_digest or '（无——这是第一个片段）'}",
            f"## Current fragment {pos}{head}, from {chunk.source_name}\n{chunk.content}",
        ]
    )


def search_system() -> str:
    """search 固定段——角色 + 入选/排除规则（跨文件共享缓存前缀）。

    Returns:
        system prompt 文本。
    """
    return "\n\n".join(
        [
            "You are a wiki relevance filter. From the existing index, select"
            " pages related to the new document, based on its summary.",
            'Output a JSON object {"paths": ["entities/redis.md",'
            ' "concepts/cache.md"]} — paths are wiki-relative page paths;'
            " output an empty array when nothing is related.",
            "",
            "## What qualifies (inclusion rules)",
            "1. Mergeable: an existing page's topic heavily overlaps the new"
            " document → the new content should fold into it",
            "2. Referencable: a person/model/tool/method on an existing page is"
            " also discussed in the new document → bidirectional [[wikilink]] needed",
            "3. Complementable: an existing page is related but incomplete →"
            " the new document can fill the gap",
            "4. Connectable: different facets of one parent topic → a topic"
            " page may aggregate them",
            "",
            "## What must never appear (exclusion rules)",
            "1. Same name, different thing: coincidental name match is not relevance",
            "2. Weak relation: merely mentioning the same word without substantive"
            " discussion",
            "3. Single passing citation: a reference/footnote mention does not count",
            '4. Too generic: super-concepts every document touches ("Python",'
            ' "programming")',
            "5. Volume control: at most 8, and only pages that pass the inclusion"
            " rules above; better miss one than pad the list. Zero is fine too",
        ]
    )


def search_user(extract: ExtractResult, index: str) -> str:
    """search 动态段：文档摘要 + index（每文件不同，放尾部）。

    Args:
        extract: 文档摘要结果。
        index: index.md 全文。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        [
            f"## Document summary ({extract.source_identity})\n"
            f"{extract.document_summary[:_SEARCH_SUMMARY_CHARS]}",
            f"## Wiki Index\n{_truncate_index_by_entries(index, _SEARCH_INDEX_CHARS)}",
        ]
    )


def analyze_system() -> str:
    """analyze 固定段——角色 + 两段式输出结构 + 字段说明（跨文件共享）。

    Returns:
        system prompt 文本。
    """
    return "\n\n".join(
        [
            "You are the knowledge base's relationship analyst. Your job is to"
            " **analyze** — no decisions, no planning. Questions like 'what next'"
            " (create pages, cross-reference) belong to the curator that runs"
            " after you; you only analyze 'what is' thoroughly.",
            "",
            "## Method (think first, then write)",
            "1. **Think fully in your head first** — read the document summary"
            " and every candidate page; get commonalities, differences and the"
            " evidence for each relationship straight",
            "2. **Write only after that** — organize the result into the two-"
            " part output below",
            "",
            "## Output structure (two parts)",
            "Part 1: free analysis — write your thinking out completely and"
            " in order; Part 2: structured tail — condense the free analysis"
            " into JSON for downstream programs.",
            "",
            "## Part 1: free analysis",
            "Systematically cover at least:",
            "- The document's core perspective: what the document is about and"
            " how its knowledge structure unfolds; entities, concepts and"
            " conclusions may come only from the document summary — never"
            " back-flow facts from candidate pages",
            "- Candidate pages one by one: state each page's actual relation to"
            " the document — what is common, what differs, what grounds the"
            " relation. Be concrete about content; 'both talk about iterators'"
            " is worthless. **Only goal/gaps/summary/body text that appears"
            " literally in the candidate input may serve as evidence**; never"
            " guess what a candidate contains beyond what is shown or what it"
            " lacks. Without direct evidence, write '证据不足'.",
            "- Notable connections: echoes, contrasts and dependencies among the"
            " document's own knowledge points and between them and candidate"
            " pages — raw material for later cross-references",
            "",
            "## Part 2: structured tail (condensation of Part 1, machine-consumed)",
            "```json",
            "{",
            '  "entities": [',
            '    {"name": "functools.partial", "type": "函数", "description":'
            ' "functools 模块中通过固定部分参数生成新函数的工具", "importance": "核心"},',
            "    ...",
            "  ],",
            '  "concepts": [',
            '    {"name": "偏函数应用", "description": "函数式编程中固定部分参数、'
            '降低调用复杂度的技术", "importance": "核心"},',
            "    ...",
            "  ],",
            '  "relationships": [',
            '    {"from": "entities/functools.md", "to": "current-doc",'
            ' "relation": "extends", "detail": "已有页面介绍 functools 模块全貌但只提及'
            ' partial 的简单用法；本文深入剖析了 partial 的参数绑定机制、与 lambda 的'
            '取舍依据，以及预填默认参数等具体场景——本文可实质性补充已有页面"},',
            "    ...",
            "  ]",
            "}",
            "```"
            + _SOURCE_LANGUAGE,
            "",
            "## Field notes",
            "- importance: repeatedly discussed / argued from several angles → 核心;"
            " a passing mention → 边缘. Mark 核心 only when the item carries the"
            " document's main thread, is discussed in multiple places, or is a"
            " required premise of other conclusions. Importance on candidate pages"
            " cannot upgrade importance for document entities.",
            "- **from/to denote direction** — both ends are paths:",
            "  from: where the relation starts (which side's content informs the other)",
            "  to:   where it ends",
            '  Existing pages use their path (entities/xxx.md); the current'
            ' document uses the fixed id "current-doc".',
            '  Direction semantics: from\'s content can serve as supplement /'
            ' contrast / counterparty for to\'s content.',
            "- relation enum:",
            "  duplicate   = heavily overlapping, the same thing"
            " (identical goal/mission of two candidates = strong duplicate signal)",
            "  extends     = one side can substantially supplement the other",
            "  related     = related but distinct — shared ground and clear difference",
            "  contradicts = contents conflict",
            "  unrelated   = search false positive; this is the default — upgrade to"
            " related only with direct evidence",
            "- **Relation evidence bar**: same wording, same field, or imaginable"
            " links are not enough. You must state concrete evidence on both ends,"
            " the commonality or difference, and why the relation is needed. If"
            " no evidence can be pointed at, use unrelated and say so in detail.",
            "- **detail must be specific** — like the example: what each side"
            " states, common ground, the difference or supplement, and the"
            " direction of information flow. Say inside detail whether evidence"
            " comes from '文档摘要' or '候选页面'; never state methods, numbers,"
            " performance or causality absent from the input.",
            "- relationships must cover every candidate page — no omissions",
            "- With no candidates or uniformly weak evidence, entities/concepts"
            " still extract document facts only, and relationships is an empty"
            " array or all unrelated; never widen the topic to pad relationships.",
        ]
    )


def analyze_user(extract: ExtractResult, candidates: str) -> str:
    """analyze 动态段：文档摘要 + 候选页 meta（每文件不同，放尾部）。

    Args:
        extract: 文档摘要结果。
        candidates: 候选页面元信息文本。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        [
            f"## Document summary (the only source of source facts:"
            f" {extract.source_identity})\n{extract.document_summary[:_ANALYZE_SUMMARY_CHARS]}",
            "## Candidate pages (only for judging existing-page relations,"
            f" not a source of facts)\n{candidates or '（无候选页面——空库首跑的正常状态，不代表 source 无价值）'}",
            "## Analysis boundary\nKeep document facts apart from candidate-page"
            " facts. Whatever a candidate page does not state explicitly is"
            " unknown; never fill gaps with common sense, training memory or the"
            " page's name.",
        ]
    )


# 模式契约——compile 允许 new + update。
# plan() 用它约束 check_plan_json。
ALLOWED_DISPOSITIONS = {"new", "update"}


def plan_system(
    *,
    schema: str = "",
    purpose: str = "",
) -> str:
    """plan 决策 prompt 的固定段（compile 模式）。

    schema/purpose 是 wiki 级常量（同一 run 内逐文件相同），留在
    system 跨文件共享；分析文本/文档摘要/index 逐文件变化，放 plan_user。

    Args:
        schema: 目录规范文本。
        purpose: 知识库使命文本。

    Returns:
        system prompt 文本。
    """
    parts: list[str] = [
        "You are the knowledge base's curator. Make the final autonomous"
        " decisions based on the relationship analysis.",
        "",
        "## Your role (decider, not executor)",
        "- The analyst only tells you 'what is' — new/update is yours to call",
        "- Page creation is yours: pick from the entities/concepts the analysis"
        " extracted using the three-question test (below)",
        "- Cross-references are yours to derive: from the analysis' from/to"
        " directions and your page decisions, work out who each page should"
        " link to — this output is yours; the analyst does not do it for you",
        "- Output contains only page operations to execute. When nothing is"
        " worth operating, output an empty array — never emit 'no-op' entries",
        purpose and f"## Wiki purpose\n{purpose}",
        schema and f"## Directory schema\n{schema}",
        (
            "## Routing rules\n"
            "- entities/ → named entities\n"
            "- concepts/ → abstract concepts\n"
            "- topics/   → topic aggregation pages (create only with ≥3 related"
            " entities/concepts)\n"
            "sources/ is not wiki content; never produce it.\n"
            "\n## Granularity test (three questions)\n"
            "1. Standalone identity — understandable without its parent context?\n"
            "2. Standalone relations — does it have its own questions, bounds or"
            " conclusions, rather than just being one step of a parent topic?\n"
            "3. Sufficient content — does the source give at least two"
            " independent facts, steps or a full explanation, enough for more"
            " than a one-line stub body?\n"
            "→ new is possible only when all three hold; any miss → no page.\n"
            "\n## New-page priority & anti-over-splitting (mandatory)\n"
            "- Default to updating existing pages; when an existing page can"
            " naturally host this content, new is forbidden.\n"
            "- A single definition, parameter, mention, step, example or"
            " conclusion cannot justify a page alone.\n"
            "- Never create a page because a topic name sounds professional,"
            " sounds related, or you can imagine future content for it.\n"
            "- Sub-topics in short documents default to sections of the parent"
            " topic, unless the source holds enough independent content and no"
            " existing page can host it.\n"
            "- A new page's reason must state explicitly: what the standalone"
            " topic is, which at least two concrete facts in the source support"
            " it, and why existing pages cannot host it. Missing any of these →"
            " update or no-op.\n"
            "- With only a short source summary, rather create fewer pages than"
            " mint stubs to cover every term.\n"
            "\n## Source-coverage check (do this before outputting)\n"
            "Internally list every major topic in the source and give each one"
            " verdict: new, update or skip.\n"
            "No major topic may be silently missed; a skip must have a stated"
            " reason (background-only mention, insufficient content, or already"
            " covered). The plan output contains real operations only, so do"
            " not write skips into page_targets — but use them to double-check"
            " you omitted nothing that should be handled.\n"
            "\n## Relation → decision mapping (guidance, not a hard rule)\n"
            "- extends (existing → current-doc) → update the existing page with"
            " the document's content\n"
            "- duplicate (heavy overlap) → update the existing page, newer"
            " wording replaces older\n"
            "- related → do not merge — create or skip, but state the link in"
            " reason/references\n"
            "- contradicts → update and mark the dispute on the page\n"
            "- unrelated → no page\n"
            "\n## Link format (important)\n"
            "[[wikilink]] uses path slugs, never titles. Correct forms:\n"
            "- [[entities/functools-wraps]] ✓\n"
            "- [[concepts/partial-application]] ✓\n"
            "- [[functools.wraps]] ✗\n"
            "- [[Partial Application]] ✗\n"
            "\n## disposition\n"
            "- new:    create a page. title is plain text (no [[]]); reason"
            " states where content comes from and which points belong.\n"
            "- update: an existing page. reason states the concrete operation:"
            " what content to add into which section.\n"
            "- Pages needing nothing stay out of the output — with nothing to"
            " build, page_targets is an empty array.\n"
            "\n## page_type (required for new — routing and type single authority)\n"
            "New pages must set page_type: concept / entity / topic, matching"
            " the wiki_path directory exactly:\n"
            "- concepts/ → page_type=concept (abstract concepts, mechanisms,"
            " principles, strategies)\n"
            "- entities/  → page_type=entity (named entities, concrete"
            " components, technical solutions)\n"
            "- topics/    → page_type=topic (aggregation pages, only with ≥3"
            " related entities/concepts)\n"
            "Whether a topic is 'an abstract concept' or 'a named entity' is"
            " decided here and inherited verbatim by generation — the writer"
            " does not re-decide.\n"
            "update targets need no page_type (they keep the old page's).\n"
            "\n## references (important — separate from reason)\n"
            "reason describes the operation; references describes the link"
            " relationships — giving the writer an exact cross-reference list.\n"
            'format: "references": [{"slug": "entities/xxx", "reason": "对照参照"}]\n'
            "Derive from the analysis' from/to directions — the document citing"
            " an existing page (→ that page's slug goes into references),"
            " mutual links among this batch's new pages (→ those targets go in).\n"
            "Example:\n"
            '  reason: "从文档提取Python迭代器特征创建独立页"\n'
            '  references: [{"slug": "entities/cpp-iterator", "reason": "对照基准"},'
            ' {"slug": "concepts/iterable", "reason": "区分用法"}]\n'
            "\n## ⚠️ Constraint\n"
            "reference slugs may only point at: existing pages (see the index"
            " below) or new pages in this same plan. When unsure, leave the"
            " cross-reference out.\n"
            "\n## Output\n"
            'Output a JSON object {"page_targets": [{"wiki_path": "wiki/...",'
            ' "title": "...", "disposition": "...", "page_type": "concept",'
            ' "reason": "concrete operation instruction", "references":'
            ' [{"slug": "entities/xxx", "reason": "对比参照"}]}'
            + _SOURCE_LANGUAGE
        ),
    ]
    return "\n\n".join(p for p in parts if p)


def plan_user(
    extract: ExtractResult,
    analysis_text: str,
    *,
    index_content: str = "",
) -> str:
    """plan 决策的动态段：分析文本/文档摘要/index（逐文件变化）。

    Args:
        extract: 文档摘要结果。
        analysis_text: 关系分析文本。
        index_content: index.md 全文。

    Returns:
        user prompt 文本。
    """
    parts: list[str] = [
        f"## Relationship analysis\n{analysis_text[:_PLAN_ANALYSIS_CHARS]}",
        f"## Document summary ({extract.source_identity})\n"
        f"{extract.document_summary[:_PLAN_SUMMARY_CHARS]}",
        index_content
        and "## Existing pages index (reference slugs must come from here or"
        f" from this plan's new pages)\n{_truncate_index_by_entries(index_content, _SEARCH_INDEX_CHARS)}",
    ]
    return "\n\n".join(p for p in parts if p)


def new_page_system() -> str:
    """新页生成的固定段——编辑规则 + frontmatter 规范（跨页面共享）。

    Returns:
        system prompt 文本。
    """
    return "\n\n".join(
        [
            "You are the knowledge base's editor. **Output the page directly —"
            " no explanations, no prefaces.** The first characters must be"
            " `-` (the start of the frontmatter); nothing before them.",
            "## Frontmatter spec",
            "```yaml\n---\n"
            "type: concept          # concept / entity / topic / source\n"
            'title: "标题"           # standard technical name\n'
            'summary: "≤50字概述"     # for the index\n'
            "tags: [tag1, tag2]      # 至少一个，不留空\n"
            'goal: "本页使命"         # see the goal notes below\n'
            'gaps: "一句话说明本页尚未覆盖的内容——后续文档可据此补缺"\n'
            "# created/updated/sources are maintained by the system — do not write them\n"
            "---\n```\n"
            + _SOURCE_LANGUAGE
            + "\n## goal (the page's mission — its evolution anchor)\n"
            "goal states what this page **is to become**: a checkable topic"
            " range, e.g. '系统覆盖 Python 闭包的机制、应用场景与内存管理，服务初学者的完整参考'.\n"
            "It is not a content inventory (that is gaps' job). Too broad"
            " ('讲好 Python') or too narrow ('覆盖 print 用法') both fail.\n"
            "## gaps (important — always filled)\n"
            "gaps declares this page's coverage status against its goal. Later"
            " compilations read it to decide whether new documents should be"
            " inserted into this page — so it is never left blank.\n"
            "Record only gaps the current source explicitly names but does not"
            " expand; never invent related topics from training memory or"
            " common sense. When the source names no gap, state that status"
            " instead of leaving it empty, e.g."
            " '来源已覆盖本页 goal 所列主题，暂无待补缺口'.\n"
            "## Body: # 标题 → overview → ## sections\n"
            "[[wikilink]] always carries | display text: [[slug|显示文本]].\n"
            "- ✓ [[entities/wraps|wraps]]\n"
            "- ✓ [[concepts/partial-application|偏函数应用]]\n"
            "- ✗ [[entities/wraps]]          ← missing | display text\n"
            "- ✗ [[wraps]]                    ← missing path prefix\n"
            "Link only pages that really exist. When unsure, no [[]].\n"
            "## Content boundary: the page contains only facts explicitly"
            " supported by this run's content source and the page's reason;"
            " never add common sense, typical usage, performance claims,"
            " causality or unstated numbers. Code blocks must be paired"
            " (every ``` opened gets a ``` closed).",
        ]
    )


def new_page_user(target: PageTarget, extract: ExtractResult) -> str:
    """新页生成的动态段：页面/原因/references/内容源（逐页面变化）。

    Args:
        target: 页面目标（标题/路径/原因/references）。
        extract: 文档摘要结果。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        [
            f"## Page: {target.title} (path: {target.wiki_path})",
            "## Page scope (mandatory)\nThis page may cover only the scope"
            f" the plan's reason demands: {target.reason}\nIf the content"
            " source does not support a detail, omit it — never complete with"
            " outside knowledge.",
            (
                "## Page type (already decided by the plan; write it verbatim"
                f" into the frontmatter)\ntype: {target.page_type}\nDo not"
                " re-decide or change it — path/type consistency is"
                " system-checked." if target.page_type else ""
            ),
            _format_references(target.references),
            f"## The single source of truth ({extract.source_identity})\nThe"
            " summary below is the only allowed source of new facts:\n"
            f"{extract.document_summary[:_GEN_SUMMARY_CHARS]}",
            "## ⚠️ Reminder: output no explanation, no preface, no wrapping ```"
            " fences. Start directly with `---`.",
        ]
    )


def update_system() -> str:
    """页面更新的固定段——融入规则 + goal/gaps 保守更新（跨页面共享）。

    Returns:
        system prompt 文本。
    """
    return "\n\n".join(
        [
            "You are the knowledge base's editor. Merge new information into an"
            " existing page. **Output the page directly, no explanation.** The"
            " first characters must be `---` (the three dashes opening the"
            " frontmatter), not a single `-` YAML list item — never output a"
            " `- ---` list-wrapped form.",
            "## The two inputs have distinct duties (must keep them apart)\n"
            "The existing page is the retention baseline, not a source of this"
            " run's new facts; unless it conflicts with the new information,"
            " keep its facts, but never pass them off as additions from this"
            " run. This run's new information is the only allowed source of"
            " new facts, numbers, causality and conclusions. The output is"
            " still the full page, but anything new may only come from the new"
            " information or from directly correcting existing content.",
            "## Rules: 1. keep existing facts 2. when the same fact gains a"
            " better wording, use it 3. fold only increments the new"
            " information supports into the existing skeleton 4. dedupe links"
            " 5. dedupe tags 6. [[wikilink]] always carries | display text:"
            " [[slug|显示文本]] ✓, [[slug]] ✗ 7. code blocks must be paired",
            "## No expansion\nNever add new sections from common sense,"
            " training memory or the candidate page's name; never write related"
            " but unstated topics into the body. Details the new information"
            " does not cover stay as they are or are omitted — do not guess.",
            "## goal (update conservatively)\ngoal is the page's mission —"
            " **by default leave it untouched**. Revise it only when this"
            " update substantially changes the page's topic range (not merely"
            " fills content); when the frontmatter lacks goal, add one (a"
            " checkable topic range, not a content inventory).",
            "## gaps update\nRemove only the old gaps this run's new information"
            " explicitly covers; keep the rest. Never invent new gaps from"
            " outside knowledge. If nothing remains uncovered, state the no-gap"
            " status rather than blanking it, e.g."
            " '来源已覆盖 goal 所列主题，暂无待补缺口' — gaps is never left empty.",
            "## Conflicts: when disputed, mark:",
            "> **Status: Disputed**",
            "> - 版本A (已有): ...",
            "> - 版本B (新): ...",
            "## Output: the complete page (frontmatter + body), starting with"
            " `---` (three dashes, not a list `-`)" + _SOURCE_LANGUAGE,
        ]
    )


def update_user(target: PageTarget, existing: str, extract: ExtractResult) -> str:
    """页面更新的动态段：原因/references/已有页/新信息（逐页面变化）。

    Args:
        target: 页面目标。
        existing: 已有页面内容。
        extract: 文档摘要结果。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        [
            "## This update's scope (plan constraint)\nExecute only the"
            f" planned operation: {target.reason}\nIf the plan's reason asks"
            " for no split or expansion, do not invent new page topics.",
            _format_references(target.references),
            f"## Existing page (retention baseline, not new evidence)\n"
            f"{existing[:_UPDATE_EXISTING_CHARS]}",
            "## This run's new information (the only source of new facts,"
            f" source={extract.source_identity})\n{extract.document_summary[:_UPDATE_SUMMARY_CHARS]}",
            "## Update check\nBefore outputting confirm: every added fact has"
            " backing inside 'This run's new information'; drop what has none;"
            " conflicting content keeps both sides under Disputed.",
            "## ⚠️ Reminder: write no preface. Start directly with `---`.",
        ]
    )


def _truncate_index_by_entries(index: str, max_chars: int) -> str:
    """按条目截断 index——保证不切断任何一行（LLM 不会看到半个 slug）。

    头部（标题行）始终保留；条目逐行累积直到接近 max_chars。

    Args:
        index: index.md 内容。
        max_chars: 截断上限。

    Returns:
        截断后的文本（含省略提示）。
    """
    lines = index.split("\n")
    if len(index) <= max_chars:
        return index

    header, entries = [], []
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("#") or line.startswith("- [["):
            if line.startswith("#"):
                header.append(line)
            else:
                entries.append(line)
        else:
            # 非条目行按 header 对待：index 结构是 header+条目，header 顺序无关
            header.append(line)

    kept_entries: list[str] = []
    used = sum(len(h) + 1 for h in header)
    for entry in entries:
        cost = len(entry) + 1
        if used + cost > max_chars:
            break
        kept_entries.append(entry)
        used += cost

    omitted = len(entries) - len(kept_entries)
    result = "\n".join(header + kept_entries)
    if omitted > 0:
        result += (
            f"\n... ({omitted} more pages hidden — if the document may touch"
            " these topics, confirm by Grep)"
        )
    return result


def _format_references(refs: list[dict[str, str]]) -> str:
    """格式化 references 列表为 prompt 可用片段。

    Args:
        refs: 引用列表。

    Returns:
        prompt 片段文本；空列表返回空串。
    """
    if not refs:
        return ""
    lines = [
        "## Suggested references (from the planning stage — these slugs are"
        " confirmed to exist; [[wikilink]] to them is safe)"
    ]
    for r in refs:
        lines.append(f"- [[{r['slug']}]] — {r.get('reason', '')}")
    return "\n".join(lines)
