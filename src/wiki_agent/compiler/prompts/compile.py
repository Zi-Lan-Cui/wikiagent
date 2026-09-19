"""compile 模式 prompt——全量共享（watch 模式同用）。

refine 模式继承本模块、只覆写 plan（见 refine.py）。
签名约定见包 docstring——同名函数同签名，Integrator/Extractor 统一调用。
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


# extract 阶段


def chunk_system() -> str:
    """chunk 摘要的固定段——角色 + 输出要求（跨 chunk 共享缓存前缀）。

    位置/标题/来源/原文是动态数据，在 chunk_user 里（prompt cache:
    固定段前移，动态段后移——同一文件所有 chunk 共享本段）。

    Returns:
        system prompt 文本。
    """
    return (
        "你是学术文献的精读助手。对给定片段做保真压缩摘要。\n\n"
        "## 输出要求\n"
        "用连贯的自然语言段落概括本片段，保持原文的叙述逻辑和主旨。"
        "不要列表、不要分节标题——就是一段通顺的叙述。\n\n"
        "## 必须保留的锚点（自然融入叙述，不要列表化）\n"
        "- 主题与结论: 本片段讲了什么、得出了什么结论\n"
        "- 关键术语: 片段中定义或深入解释的术语，保留原文表述\n"
        "- 定量数据: 具体的数字、参数、实验结果\n"
        "- 关系线索: 本片段与其他概念的联系（对比/依赖/延伸）\n"
        "- 与通常认知不符的内容标注 {{可能矛盾}}\n\n"
        "## 约束\n"
        "- 保留原文技术术语和公式符号，不翻译、不改写专有名词\n"
        "- 不推测、不引入外部知识，只写片段中实际出现的内容\n"
        "- 图片描述信息（如有）纳入叙述\n"
        "- 不要把常识、训练记忆或你认为合理的因果关系写进摘要\n"
        "- 原文没有明确给出的数字、时间、人物、结论和因果关系必须省略\n"
        "- 如果片段信息不足，明确写‘原文未说明’，不要用外部知识补全"
    )


def chunk_user(chunk: SourceChunk) -> str:
    """chunk 摘要的动态度——位置信息 + 片段原文。

    Args:
        chunk: 待摘要片段。

    Returns:
        user prompt 文本。
    """
    pos = f"第 {chunk.index + 1}/{chunk.total} 个片段"
    head = f"，位于『{chunk.heading_path}』" if chunk.heading_path else ""
    return f"待摘要片段: {pos}{head}（来自 {chunk.source_name}）。\n\n{chunk.content}"


def synthesis_prompt() -> str:
    """文档级概述的 system prompt——合成阶段固定段。

    Returns:
        system prompt 文本。
    """
    return "\n".join(
        [
            "基于一份文档的所有片段摘要，生成文档级概述。自然语言段落输出，不要求固定段数。",
            "",
            "## 使用章节结构线索",
            "每个片段摘要前标注了它在原文中的章节位置（标题路径）和序号。",
            "用这些线索还原文档的章节骨架——概述应该体现原文的章节推进逻辑，而不是平铺罗列各片段。",
            "",
            "## 内容组织（从粗到精）",
            "- 只有原文明确说明文档类型时，才给出笔记/教程/论文/报告等分类；否则只描述明确主题",
            "- 然后展开核心内容：主要讲了哪些知识点或方法，它们之间的逻辑关系是什么",
            "- 关键术语或命名实体只在原文有定义、解释或反复讨论时提及；不要凭经验推荐后续方向",
            "- 公式中的符号、参数表中的字段和它们之间的对应关系，原文明确说明时可作为事实；"
            "若上下文有强关联但原文未明说，只能用‘通常按该领域记号’等措辞标为惯例推断，不能写成确定事实",
            "- 不要用‘通常’、‘一般’、‘可能’、‘往往’掩饰没有上下文依据的猜测；没有依据的内容直接省略",
            "",
            "## 什么该突出",
            "- 本文独有的观点、方法、数据——区别于同类文档的差异部分",
            "- 被反复讨论或多种角度论证的概念——说明它在本文中的核心地位",
            "- 与其他知识点的交叉关联只有在片段明确表达时才保留，不要为了方便建链接而补充关联",
            "",
            "## 什么该淡化",
            "- 可在段落中自然携带但不必展开：过渡性陈述、重复论证、辅助性示例",
            "- 文中已有的图片/表格引用：如果 alt text 已描述内容，简要提及即可",
            "",
            "## 错误处理",
            "- 如果内容本身存在已知的明显错误（语法/逻辑/术语误用），在涉及该内容时标注 {原文有误}",
            "- 不因为存在错误而忽略正确部分。标注错误≠否定全文",
            "",
            "## 其他",
            "- 保留原文技术术语和专有名词，不强行翻译",
            "- 如果片段之间存在矛盾，标注 {内部矛盾}",
            "- 每个事实、术语、数字和结论都必须能回溯到给定片段摘要",
            "- 不要用常识或训练记忆补充片段摘要中没有的信息；无法确认时省略",
            "- 不要凭标题或章节名推断正文没有表达的结论",
            "- 不要把多个片段的相邻出现改写成它们存在因果、依赖或时间先后关系",
            "- 除非片段明确支持，否则不要使用‘可能涉及’、‘通常’、‘常见’、‘属于某领域’、‘未来可以’等背景推断",
            "- 输出前逐句检查：主语、数字、术语、结论和因果关系是否都能在片段摘要中找到依据",
            "- 即使文档很短或没有章节结构，也要直接概括原文明确给出的事实；不要因为信息少而拒绝生成摘要",
            "- 不要输出‘根据提供的片段摘要’、‘无法生成文档级概述’、‘请提供更多片段’等过程性话语",
            "- 不要提及‘片段摘要’、‘输入材料’、‘提供的内容’或模型自己的分析过程；直接陈述文档事实",
        ]
    )


def rolling_system() -> str:
    """滚动压缩的固定段——合并指令（跨文档共享缓存前缀）。

    Returns:
        system prompt 文本。
    """
    return (
        "## 合并指令\n"
        "把当前片段的信息融入全局摘要，**输出重写后的完整全局摘要**"
        "（不是增量追加，是每轮重写全文）:\n"
        "1. 连贯叙述——保持文档的主旨流和逻辑关系，不用分节结构\n"
        "2. 已有内容不重复——全局摘要的语义是'已经包含的就不再写'\n"
        "3. 新信息自然融入——新术语、新数据、新结论嵌入叙述，保持原文表述\n"
        "4. 矛盾标注——新信息与之前矛盾时保留双方并标 {{矛盾}}\n"
        f"5. 长度控制——全局摘要保持在 {DIGEST_TARGET_TOKENS} tokens 以内；"
        "超限时优先压缩早期片段的细节，保留主旨、关键术语和最新片段细节\n"
        "6. 只输出全局摘要本身，不要任何解释或前言\n"
        "7. 不要使用外部知识或训练记忆补充当前片段和已有摘要没有明确支持的内容\n"
        "8. 只保留当前片段和已有摘要共同支持的事实；当前片段没有明确说明的内容不要补写\n"
        "9. 不要因为两个概念同时出现就断言它们存在因果、依赖或时间关系；原文没有说就不推断"
    )


def rolling_user(
    chunk: SourceChunk,
    previous_digest: str,
    total: int,
) -> str:
    """滚动压缩的动态度——digest + 当前片段（每轮变化在尾部）。

    Args:
        chunk: 当前片段。
        previous_digest: 上一轮全局摘要。
        total: 片段总数。

    Returns:
        user prompt 文本。
    """
    pos = f"第 {chunk.index + 1}/{total} 个片段"
    head = f"（{chunk.heading_path}）" if chunk.heading_path else ""
    return "\n\n".join(
        [
            f"## 之前的全局摘要\n{previous_digest or '（无——这是第一个片段）'}",
            f"## 当前片段 {pos}{head}，来自 {chunk.source_name}\n{chunk.content}",
        ]
    )


# integrate 阶段


def search_system() -> str:
    """search 固定段——角色 + 入选/排除规则（跨文件共享缓存前缀）。

    Returns:
        system prompt 文本。
    """
    return "\n\n".join(
        [
            "你是 Wiki 相关度过滤器。基于文档摘要和已有 index，选出与新文档相关的已有页面。",
            '输出纯 JSON 字符串数组: ["entities/redis.md", "concepts/cache.md"]，不要其他内容。',
            "",
            "## 什么应该入选（选入规则）",
            "1. 可合并: 已有页面主题与新文档高度重叠 → 新内容应并入旧页",
            "2. 可引用: 已有页面提到的人/模型/工具/方法在新文档中也被讨论 → 需要双向 [[wikilink]]",
            "3. 可补充: 已有页面有相关但不完全的内容 → 新文档可补缺口",
            "4. 可关联: 同一个父主题下的不同方面 → 需要 topic 页来汇总",
            "",
            "## 什么坚决不要（排除规则）",
            "1. 同名但无关: 不要因为名字碰巧一样就入选",
            "2. 弱相关: 仅是提到了同一个词但不是实质性讨论",
            "3. 只出现一次: 参考文献/脚注中一笔带过的引用不算",
            '4. 太泛: "Python"、"编程" 这类每个文档都会有的超级概念不选',
            "5. 数量控制: 选最重要的 3-8 个，宁可遗漏也不凑数。0 个也可以",
        ]
    )


def search_user(extract: ExtractResult, index: str) -> str:
    """search 动态度——文档摘要 + index（每文件不同，放尾部）。

    Args:
        extract: 文档摘要结果。
        index: index.md 全文。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        [
            f"## 文档摘要 ({extract.source_identity})\n{extract.document_summary[:_SEARCH_SUMMARY_CHARS]}",
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
            "你是知识库的关系分析师。你的职责是**分析**，不做决策、不做规划。",
            "新建页面、交叉引用这些'接下来怎么办'的问题由后续的策展人决定——"
            "你只负责把'是什么'分析透彻。",
            "",
            "## 工作方式（先思考，再动笔）",
            "1. **先在内心充分思考**——通读文档摘要和每个候选页面，"
            "把共同点、差异、关系依据全部想清楚",
            "2. **想清楚后再动笔**——把思考的结果系统整理成下面的两段式输出",
            "",
            "## 输出结构（两段式）",
            "第一段: 自由分析——把思考的结果完整、有条理地写出来；",
            "第二段: 结构化尾巴——把自由分析浓缩成供下游程序消费的 JSON。",
            "",
            "## 第一段: 自由分析",
            "系统整理你的思考，至少覆盖:",
            "- 文档的核心视角: 这篇文档的主体是什么，围绕它展开的知识结构如何；"
            "实体、概念和结论只能来自文档摘要，不要从候选页面倒灌事实",
            "- 候选页面的逐个剖析: 对每个候选页面，讲清楚它和文档的实际关系——"
            "共同点是什么、差异是什么、关系成立的依据是什么。"
            "要具体到内容层面，不要'都讲迭代器'这种废话。"
            "**只有候选页输入中明确出现的 goal/gaps/summary/正文信息才可作为候选证据**；"
            "不要猜测候选页没有展示的内容或缺口。若没有直接证据，写明'证据不足'。",
            "- 值得注意的关联点: 文档内部各知识点之间、以及它们与候选页面之间的"
            "呼应、对比、依赖关系——这是后续建立交叉引用的原材料",
            "",
            "## 第二段: 结构化尾巴（自由分析的浓缩，供程序消费）",
            "```json",
            "{",
            '  "entities": [',
            '    {"name": "functools.partial", "type": "函数", "description": "functools 模块中通过固定部分参数生成新函数的工具", "importance": "核心"},',
            "    ...",
            "  ],",
            '  "concepts": [',
            '    {"name": "偏函数应用", "description": "函数式编程中固定部分参数、降低调用复杂度的技术", "importance": "核心"},',
            "    ...",
            "  ],",
            '  "relationships": [',
            '    {"from": "entities/functools.md", "to": "current-doc", "relation": "extends", "detail": "已有页面介绍 functools 模块全貌但只提及 partial 的简单用法；本文深入剖析了 partial 的参数绑定机制、与 lambda 的取舍依据，以及预填默认参数等具体场景——本文可实质性补充已有页面"},',
            "    ...",
            "  ]",
            "}",
            "```",
            "",
            "## 字段说明",
            "- importance: 被反复讨论/多角度论证 → 核心；一笔带过 → 边缘",
            "  仅在文档中承担主线、被多处讨论或是其他结论的必要前提时标为核心；"
            "只出现一次、只是示例或背景时标为边缘。候选页中的重要性不能改变文档实体的重要性。",
            "- **from/to 表示关系的方向**——关系两端都用路径表达:",
            "  from: 关系起点（信息从谁流向谁）",
            "  to:   关系终点",
            '  已有页面用它的路径（entities/xxx.md）；当前文档用固定标识 "current-doc"。',
            '  方向语义: "from" 的内容可以作为 "to" 的内容的补充/对照/矛盾方。',
            "- relation 枚举:",
            "  duplicate   = 高度重叠，讲的是同一件事"
            "（两个候选的 goal 使命相同 = duplicate 强信号）",
            "  extends     = 一方的内容可以被另一方实质性补充",
            "  related     = 相关但不同——有关联点也有区分点",
            "  contradicts = 内容互相矛盾",
            "  unrelated   = search 误判，实际无关；这是默认关系，除非有直接证据才升级为 related",
            "- **关系判定门槛**: 同词、同领域、可想象的联系都不够。"
            "必须同时说明 source/候选两端的具体证据、共同点或差异，以及为什么需要该关系。"
            "无法指出证据时使用 unrelated，detail 写明证据不足。",
            "- **detail 必须写详细**——像示例那样写明: 两端各自讲了什么、"
            "共同点是什么、差异或补充点是什么、信息流动的方向依据。"
            "并在 detail 中标明证据来自'文档摘要'还是'候选页面'；"
            "不得写没有出现在输入中的方法、数字、性能或因果结论。",
            "- relationships 必须覆盖每一个候选页面，不能遗漏",
            "- 候选为空或全部证据不足时，entities/concepts 仍只提取文档事实，"
            "relationships 输出空数组或全部 unrelated；不要为了凑关系而扩展主题。",
        ]
    )


def analyze_user(extract: ExtractResult, candidates: str) -> str:
    """analyze 动态度——文档摘要 + 候选页 meta（每文件不同，放尾部）。

    Args:
        extract: 文档摘要结果。
        candidates: 候选页面元信息文本。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        [
            f"## 文档摘要（唯一的 source 事实来源：{extract.source_identity}）\n"
            f"{extract.document_summary[:_ANALYZE_SUMMARY_CHARS]}",
            f"## 候选页面（仅用于判断已有页面关系，不是 source 事实来源）\n{candidates or '（无候选页面——空库首跑的正常状态，不代表 source 无价值）'}",
            "## 分析边界\n"
            "请把 source 事实与候选页面事实分开。候选页面没有明确提供的内容视为未知；"
            "不能用常识、训练记忆或候选页面名称补全。",
        ]
    )


# 模式契约——compile 允许 new + update。
# plan() 用它约束 check_plan_json（refine 模块声明自己的 {"update"}）。
ALLOWED_DISPOSITIONS = {"new", "update"}


def plan_system(
    *,
    schema: str = "",
    purpose: str = "",
) -> str:
    """策展人决策 prompt 的固定段（compile 模式）。

    schema/purpose 是 wiki 级常量（一次 run 内逐文件相同）——留在
    system 跨文件共享。分析文本/文档摘要/index 逐文件变化，在
    plan_user。

    Args:
        schema: 目录规范文本。
        purpose: 知识库使命文本。

    Returns:
        system prompt 文本。
    """
    parts: list[str] = [
        "你是知识库的策展人。基于关系分析的结论，做最终的自主决策。",
        "",
        "## 你的职责（决策者，不是执行者）",
        "- 关系分析师只告诉你'是什么'——new/update 由你决定",
        "- 新建页面的选择由你做: 从分析提取的实体/概念里挑，三问法判断（见下）",
        "- 交叉引用由你派生: 从关系分析的 from/to 方向和你的建页决策里，"
        "推出每页该引用谁——这是你的产出，分析师不代劳",
        "- 输出只含要执行的页面操作。没有值得操作的页面时输出空数组——不要输出'不操作'的条目",
        purpose and f"## Wiki 用途\n{purpose}",
        schema and f"## 目录规范\n{schema}",
        (
            "## 路由规则\n"
            "- entities/ → 命名实体\n"
            "- concepts/ → 抽象概念\n"
            "- topics/   → 主题汇集页（≥3 个相关实体/概念时才创建）\n"
            "sources/ 不属于 Wiki 内容目录，禁止生成。\n"
            "\n## 粒度判断（三问法）\n"
            "1. 独立身份 — 脱离父上下文能独立被理解吗？\n"
            "2. 独立关系 — 它是否有自己的问题、边界或结论，而不只是父主题中的一个步骤？\n"
            "3. 足够内容 — source 是否提供至少两个独立事实、步骤或完整解释，能写出超过一句 stub 的正文？\n"
            "→ 三个都满足才可能 new；任意一项不满足 → 不建页。\n"
            "\n## 新建优先级与反过度拆分（必须遵守）\n"
            "- 默认优先 update 已有页面；已有页面能够自然容纳本次信息时，禁止 new。\n"
            "- 只有一个定义、一个参数、一次提及、一个步骤、一个示例或一条结论，不能单独建页。\n"
            "- 不能因为主题名称看起来专业、与其他主题有关联，或可以想象出后续内容，就创建页面。\n"
            "- 短文档中的子主题默认作为父主题的小节处理；除非 source 对它有足够独立内容，且已有页面确实无法容纳。\n"
            "- new 的 reason 必须明确写出：独立主题是什么、source 中至少两个具体事实是什么、"
            "为什么现有页面不能承载。缺少任一项时改为 update 或不操作。\n"
            "- 如果只有很短的 source 摘要，宁可少建页面，也不要为了覆盖名词而制造 stub 页面。\n"
            "\n## source 覆盖检查（输出前执行）\n"
            "先在内部列出 source 中的每个主要主题，并为每个主题作出唯一处置：new、update 或 skip。\n"
            "任何主要主题都不能无故遗漏；skip 必须有明确原因（例如仅背景提及、内容不足或已有页已覆盖）。\n"
            "计划只输出实际操作项，因此不要把 skip 写入 page_targets，但必须据此检查是否漏掉了应处理的主题。\n"
            "\n## 关系 → 决策的映射（参考，不是死规则）\n"
            "- extends（已有页 → current-doc）→ update 已有页，补充文档内容\n"
            "- duplicate（高度重叠）→ update 已有页，新表述替换旧表述\n"
            "- related → 不合并——新建页或不建，但 reason/references 写明关联\n"
            "- contradicts → update 并在页面标注争议\n"
            "- unrelated → 不建页\n"
            "\n## 链接格式（重要）\n"
            "[[wikilink]] 使用路径 slug，不是页面标题。正确格式:\n"
            "- [[entities/functools-wraps]] ✓\n"
            "- [[concepts/partial-application]] ✓\n"
            "- [[functools.wraps]] ✗\n"
            "- [[Partial Application]] ✗\n"
            "\n## disposition\n"
            "- new:    创建新页。title 是纯文本（不含 [[]]），reason 写明从哪里提取内容、应包含哪些关键点。\n"
            "- update: 已有页面。reason 写明具体操作: 补充什么内容到哪个章节。\n"
            "- 不操作的页面不要写进输出——没有要建的页时 page_targets 为空数组。\n"
            "\n## page_type（new 页面必填——路由与类型的单一权威）\n"
            "new 页面必须给出 page_type: concept / entity / topic，且必须与 wiki_path 的目录一致:\n"
            "- concepts/ → page_type=concept（抽象概念、机制、原则、策略）\n"
            "- entities/  → page_type=entity（命名实体、具体组件、技术方案）\n"
            "- topics/    → page_type=topic（主题汇集页，≥3 个相关实体/概念时才创建）\n"
            "同一主题是『抽象概念』还是『命名实体』在这里决定，生成阶段会直接沿用、不再自行判断。\n"
            "update 目标不需要 page_type（沿用已有页面）。\n"
            "\n## references 字段（重要——与 reason 分离）\n"
            "reason 负责描述操作。references 负责描述引用关系——为生成阶段提供精确的交叉引用列表。\n"
            '格式: "references": [{"slug": "entities/xxx", "reason": "对照参照"}]\n'
            "派生来源: 关系分析里的 from/to 方向——文档引用已有页（→ 已有页 slug 进 references）、\n"
            "新建页面之间的互引（→ 本次 new 的 target 进 references）。\n"
            "示例:\n"
            '  reason: "从文档提取Python迭代器特征创建独立页"\n'
            '  references: [{"slug": "entities/cpp-iterator", "reason": "对照基准"}, {"slug": "concepts/iterable", "reason": "区分用法"}]\n'
            "\n## ⚠️ 约束\n"
            "references 中的 slug 只能引用: 已有页面（见下方 index）/ 本次 plan 中的 new 页面。\n"
            "不确定时宁可不写交叉引用。\n"
            "\n## 输出\n"
            '纯 JSON（不要用 ```json 包裹）: {"page_targets": [{"wiki_path": "wiki/...", '
            '"title": "...", "disposition": "...", "page_type": "concept", "reason": "具体操作指令",'
            ' "references": [{"slug": "entities/xxx", "reason": "对比参照"}]}'
        ),
    ]
    return "\n\n".join(p for p in parts if p)


def plan_user(
    extract: ExtractResult,
    analysis_text: str,
    *,
    index_content: str = "",
) -> str:
    """策展人决策的动态度——分析文本/文档摘要/index（逐文件变化）。

    Args:
        extract: 文档摘要结果。
        analysis_text: 关系分析文本。
        index_content: index.md 全文。

    Returns:
        user prompt 文本。
    """
    parts: list[str] = [
        f"## 关系分析文本\n{analysis_text[:_PLAN_ANALYSIS_CHARS]}",
        f"## 文档摘要 ({extract.source_identity})\n{extract.document_summary[:_PLAN_SUMMARY_CHARS]}",
        index_content
        and f"## 已有页面 Index（references 的 slug 必须从中选取或为本次 new 页面）\n{_truncate_index_by_entries(index_content, _SEARCH_INDEX_CHARS)}",
    ]
    return "\n\n".join(p for p in parts if p)


def new_page_system() -> str:
    """新页生成的固定段——编辑规则 + frontmatter 规范（跨页面共享）。

    Returns:
        system prompt 文本。
    """
    return "\n\n".join(
        [
            "你是知识库的编辑。**直接输出页面内容，不要任何解释或前言。**",
            "首字符必须是 `-`（frontmatter 开头），不在此之前输出任何文字。",
            "## Frontmatter 规范",
            "```yaml\n---\n"
            "type: concept          # concept / entity / topic / source\n"
            'title: "标题"           # 技术标准名\n'
            'summary: "≤50字概述"     # 用于索引\n'
            "tags: [tag1, tag2]\n"
            'goal: "本页使命"         # 见下方 goal 字段说明\n'
            'gaps: "一句话说明本页尚未覆盖的内容——后续文档可据此补缺"\n'
            "# created/updated/sources 由系统维护，不要写\n"
            "---\n```\n"
            "## goal 字段（本页使命——演化方向锚）\n"
            "goal 写明本页**要成为什么**：可检验的主题范围，如\n"
            "'系统覆盖 Python 闭包的机制、应用场景与内存管理，服务初学者的完整参考'。\n"
            "不是内容清单（那是 gaps 的活）。太泛（'讲好 Python'）或太窄\n"
            "（'覆盖 print 用法'）都不合格。\n"
            "## gaps 字段（重要）\n"
            "gaps 只能记录当前 source 明确指出但没有展开的缺口；\n"
            "不要使用训练记忆或常识主动发明相关主题。没有明确缺口时写空字符串。\n"
            "这些声明会在后续文档编译时被读取，用于判断新文档能否补充本页。\n"
            "## 正文: # 标题 → 概述 → ## 分节\n"
            "[[wikilink]] 必须带 | 和可读文字，格式: [[slug|显示文本]]。\n"
            "- ✓ [[entities/wraps|wraps]]\n"
            "- ✓ [[concepts/partial-application|偏函数应用]]\n"
            "- ✗ [[entities/wraps]]          ← 缺少 | 说明文字\n"
            "- ✗ [[wraps]]                    ← 缺少路径前缀\n"
            "只链接确实存在的页面。不确定时不用 [[]]。\n"
            "## 内容边界: 页面只写本次内容来源和页面原因明确支持的事实；"
            "不要补充常识、典型用法、性能结论、因果关系或未给出的数字。"
            "代码块必须成对闭合（``` 打开就要 ``` 关闭）",
        ]
    )


def new_page_user(target: PageTarget, extract: ExtractResult) -> str:
    """新页生成的动态度——页面/原因/references/内容源（逐页面变化）。

    Args:
        target: 页面目标（标题/路径/原因/references）。
        extract: 文档摘要结果。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        [
            f"## 页面: {target.title} (路径: {target.wiki_path})",
            "## 页面范围（必须遵守）\n"
            f"本页只能覆盖以下计划原因明确要求的范围：{target.reason}\n"
            "如果内容来源没有支持某个细节，就省略，不要用外部知识补全。",
            (
                "## 页面类型（计划已决策，必须原样写入 frontmatter）\n"
                f"type: {target.page_type}\n"
                "不要自行判断或更改——路径目录与 type 的一致性由系统校验。"
                if target.page_type
                else ""
            ),
            _format_references(target.references),
            f"## 唯一事实来源 ({extract.source_identity})\n"
            f"以下摘要是本次允许新增事实的唯一来源：\n{extract.document_summary[:_GEN_SUMMARY_CHARS]}",
            "## ⚠️ 重申: 不要输出任何解释、前言或包裹性的 ``` 代码块。直接从 `---` 开始。",
        ]
    )


def update_system() -> str:
    """页面更新的固定段——融入规则 + goal/gaps 保守更新（跨页面共享）。

    Returns:
        system prompt 文本。
    """
    return "\n\n".join(
        [
            "你是知识库的编辑。在已有页面基础上融入新信息。**直接输出页面，不要解释。**",
            "首字符必须是 `---`（frontmatter 开头的三个连字符），"
            "不是 YAML 列表的单个 `-`——不要输出 `- ---` 这样的列表包裹形式。",
            "## 两类输入的职责（必须区分）\n"
            "已有页面是保留基线，不是本次新增事实的来源；除非与新信息冲突，\n"
            "已有页面中的事实应保留，但不得把它们伪装成本次 source 的新增内容。\n"
            "本次新信息是唯一允许新增事实、数字、因果和结论的来源。\n"
            "最终仍输出完整页面，但新增内容只能来自本次新信息或对已有内容的直接修正。",
            "## 规则: 1.保留已有页面事实 2.同事实用新版表述 3.只把新信息支持的增量融入现有骨架 "
            "4.链接去重 5.tags去重 6.[[wikilink]]必须带|文字: [[slug|显示文本]] ✓, [[slug]] ✗ "
            "7.代码块必须成对闭合",
            "## 禁止扩展\n"
            "不要根据常识、训练记忆或候选页面自行补充新章节；不要把相关但未提供的主题写入正文。\n"
            "新信息没有说明的细节保持原样或省略，不要猜测。",
            "## goal 字段（保守更新）\n"
            "goal 是本页使命——**默认保持不动**。只有本次补充实质性改变了本页的"
            "主题范围（不只是填充内容）时才修订 goal；缺 goal 字段时补写一个"
            "（可检验的主题范围，不是内容清单）。",
            "## gaps 字段更新\n"
            "只移除本次新信息明确覆盖的已有缺口，保留其他缺口。"
            "不要根据外部知识新增 gaps；已有页面没有 gaps 时可写空字符串。",
            "## 冲突: 矛盾时标注:",
            "> **Status: Disputed**",
            "> - 版本A (已有): ...",
            "> - 版本B (新): ...",
            "## 输出: 完整页面（frontmatter + 正文），首字符 `---`（三个连字符，不是列表 `-`）",
        ]
    )


def update_user(target: PageTarget, existing: str, extract: ExtractResult) -> str:
    """页面更新的动态度——原因/references/已有页/新信息（逐页面变化）。

    Args:
        target: 页面目标。
        existing: 已有页面内容。
        extract: 文档摘要结果。

    Returns:
        user prompt 文本。
    """
    return "\n\n".join(
        [
            "## 本次更新范围（计划约束）\n"
            f"只执行以下计划操作：{target.reason}\n"
            "如果计划原因没有要求拆分或扩展主题，不要自行增加新页面主题。",
            _format_references(target.references),
            f"## 已有页面（保留基线，不是新增证据）\n{existing[:_UPDATE_EXISTING_CHARS]}",
            f"## 本次新信息（唯一新增事实来源，source={extract.source_identity}）\n"
            f"{extract.document_summary[:_UPDATE_SUMMARY_CHARS]}",
            "## 更新检查\n"
            "输出前确认：每一处新增事实都能在“本次新信息”中找到依据；"
            "无法找到依据的内容不要新增；冲突内容保留双方并标记 Disputed。",
            "## ⚠️ 重申: 不要写任何前言。直接从 `---` 开始。",
        ]
    )


# prompt 格式化辅助


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
            # 非条目行按 header 对待（保序无关紧要——index 结构是 header+条目）
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
            f"\n... （还有 {omitted} 个页面未显示——若文档可能涉及这些主题，请用 Grep 搜索确认）"
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
    lines = ["## 引用建议 (来自策划阶段——以下 slug 已确认存在，可安全使用 [[wikilink]])"]
    for r in refs:
        lines.append(f"- [[{r['slug']}]] — {r.get('reason', '')}")
    return "\n".join(lines)
