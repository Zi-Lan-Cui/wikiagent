"""编译流水线数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from openai.types.shared_params import ResponseFormatJSONObject


class Disposition(StrEnum):
    """页面操作类型，只含要执行的决策。

    无需操作时 page_targets 为空数组，不放"不操作"条目。
    """

    NEW = "new"
    UPDATE = "update"


# 编译调用关闭 thinking：reasoning 模型的思考段可能占满 max_tokens
# 预算导致 content 为空。integration 各阶段共用此常量。
NO_THINKING = {"thinking": {"type": "disabled"}}

# API 级 JSON 模式，输出必为合法 JSON 对象。check/parse 层仍接受顶层数组，
# 防兼容端点静默忽略该参数。各阶段共用此常量。
JSON_MODE: ResponseFormatJSONObject = {"type": "json_object"}


# 提取阶段输入——来自 Chunker 的结构化 chunk


@dataclass
class SourceChunk:
    """单个 chunk 及其在源文件中的位置元信息，Extract 阶段用于构造 prompt。"""

    content: str
    """chunk 的文本内容。"""

    index: int
    """chunk 在源文件中的序号（从 0 开始）。"""

    total: int
    """源文件的总 chunk 数。"""

    heading_path: str = ""
    """标题路径，如 ``# 神经网络 > ## 反向传播 > ### 梯度计算``。"""

    source_name: str = ""
    """源文件名，如 ``吴恩达深度学习笔记.md``——chunk 级摘要 prompt 的出处标注。"""

    # 不设 chunk_overlap/prev_head/next_head：编译全量消费每个 chunk，
    # 跨边界语义由 digest 与 synthesis 覆盖；检索场景需要时再加在消费端。


@dataclass
class SourceDocument:
    """一个源文件的完整结构化表示。

    包含其所有 chunk 及文件级元信息。
    """

    name: str
    """源文件名。"""

    ext: str
    """源文件扩展名。"""

    path: str
    """源文件路径（相对于 raw/ 或绝对路径）。"""

    chunks: list[SourceChunk] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        """chunk 总数。"""
        return len(self.chunks)


# 提取阶段输出


@dataclass
class ChunkSummary:
    chunk_index: int
    heading_path: str = ""
    text: str = ""


@dataclass
class SourcePage:
    """源文件的溯源档案页——slug + 完整页面文本。

    档案页只向用户展示来源信息，对模型不可见，引用一律指向原始文件，
    因此放在 wiki 之外。构造在 extraction，job 成功后才落盘。
    """

    slug: str
    content: str


@dataclass
class ExtractResult:
    """Extract 输出：文档级摘要。不做实体提取，不做格式化。"""

    source_identity: str
    document_summary: str = ""
    source_page: SourcePage | None = None
    """档案页构造结果（compile/sync 模式才有），写入责任在结算方。"""


# 集成阶段输出


@dataclass
class PageTarget:
    wiki_path: str
    title: str
    disposition: Disposition
    reason: str = ""
    references: list[dict[str, str]] = field(default_factory=list)
    """引用建议: [{"slug": "entities/xxx", "reason": "对比参照"}, ...]。从 reason 中剥离，方便后续过滤和 retry。"""

    page_type: str = ""
    """new 页面的 type（concept/entity/topic）——plan 决策的单一权威。

    路由目录与 type 由 plan 同一次决策产出，generate 阶段不再自行判断：
    否则两个独立 LLM 判断各走各的（实测: plan 路由 entities/、
    generate 写 type=concept，质量闸门 type/目录不一致拦截）。
    update 目标留空（沿用已有页面 type）。"""


@dataclass
class SearchResult:
    """Search 阶段输出——与新文档相关的已有 wiki 页面列表。"""

    rel_paths: list[str] = field(default_factory=list)
    """相关页面路径，如 ['concepts/backpropagation.md', ...]。"""

    raw: str = ""
    """LLM 原始输出，供阶段评测和失败排查使用。"""


@dataclass
class PageRelationship:
    from_page: str = ""  # 关系起点——已有页面路径（或当前文档标识）
    to_page: str = ""  # 关系终点——已有页面路径（或当前文档标识）
    relation: str = ""  # duplicate / extends / related / contradicts / unrelated
    detail: str = ""  # 具体说明（内容层面的共同点/差异/依据）


@dataclass
class AnalysisResult:
    """Analysis 输出：自由分析主体 + 结构化字段（供下游消费）。

    analyze 只做实体/概念提取和与候选页的关系判定；
    新建与交叉引用的决策由 plan 阶段产出。
    """

    source_identity: str
    """源文件名。"""

    analysis_text: str = ""
    """自由分析主体——LLM 的完整推理文本（plan 决策的主要依据）。"""

    raw_analysis: str = ""
    """LLM 原始输出（含 JSON 尾巴，保留用于调试）。"""

    entities: list[dict] = field(default_factory=list)
    """命名实体: [{"name": "functools.partial", "type": "函数", "description": "...", "importance": "核心"}]。"""

    concepts: list[dict] = field(default_factory=list)
    """抽象概念: [{"name": "偏函数", "description": "...", "importance": "核心"}]。"""

    relationships: list[PageRelationship] = field(default_factory=list)
    """对每个候选页面的关系判定。"""


@dataclass
class IntegrationPlan:
    page_targets: list[PageTarget] = field(default_factory=list)
    raw: str = ""
    """LLM 原始输出——解析前的现场，供审计/失败排查（替代 .debug_plan.json）。"""
