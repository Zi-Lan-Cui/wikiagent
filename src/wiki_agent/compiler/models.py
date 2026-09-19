"""WikiCompiler 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Disposition(StrEnum):
    """页面操作类型——只含要执行的决策。

    plan 的产出是"要做什么"，不是"我考虑过什么"。
    "不操作"不进 page_targets——由空数组 + plan_noop 事件表达。
    """

    NEW = "new"
    UPDATE = "update"


# 编译流水线关 thinking——deepseek-v4-flash 是 reasoning 模型，
# 思考段会静默吃掉整个 max_tokens 预算、content 留空（审计 C1 根因）。
# 编译输出是"写页面"不是"解难题"，直接写更可靠也更便宜。
# 单一来源：integration 四阶段与 restructure 复用此常量（勿再各存副本）。
_NO_THINKING = {"thinking": {"type": "disabled"}}


# Phase 1 输入 —— 来自 Chunker 的结构化 chunk


@dataclass
class SourceChunk:
    """单个 chunk 的完整上下文。

    不只是裸文本——携带其在源文件中的位置信息，
    Extract 阶段用这些元信息构造更精确的 prompt。
    """

    content: str
    """chunk 的文本内容。"""

    index: int
    """chunk 在源文件中的序号（从 0 开始）。"""

    total: int
    """源文件的总 chunk 数。"""

    heading_path: str = ""
    """标题路径，如 ``# 神经网络 > ## 反向传播 > ### 梯度计算``。"""

    source_name: str = ""
    """源文件名，如 ``吴恩达深度学习笔记.md``。"""

    source_ext: str = ""
    """源文件扩展名，如 ``md``、``pdf``（经过 MinerU 转换后变为文本）。"""

    # 未实现字段（预留——需要时再加，理由见下）
    # heading_path 已由 chunker metadata 产出（2026-08-14），见 text_chunker。
    # chunk_overlap：检索场景（RAG 按相似度选 chunk）才需要——防切分点切断
    #   语义导致漏命中。编译是全量消费（每 chunk 都喂 LLM），边界语义由
    #   滚动压缩的 digest（全局摘要）与均匀分配的 synthesis（全量合成）覆盖，
    #   比 overlap 更强。RAG 路径复活时再加，且不应在 chunker 层做——
    #   重叠是"检索单元的构造策略"，该在 embedding 消费端做。
    # prev_head/next_head：同理——编译消费端用 heading_path 已够。


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
        """返回 chunk 总数。

        Returns:
            chunks 列表长度。
        """
        return len(self.chunks)


# Phase 1 输出 —— ExtractResult


@dataclass
class ChunkSummary:
    chunk_index: int
    heading_path: str = ""
    text: str = ""


@dataclass
class ExtractResult:
    """Extract 输出——纯文档级摘要。不做实体提取，不做格式化。"""

    source_identity: str
    document_summary: str = ""


# Phase 2 输出


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
    """Analysis 输出——自由分析（主体）+ 结构化尾巴（供下游消费）。

    analyze 只做分析: 实体/概念提取 + 与候选页的关系判定。
    新建建议和交叉引用是决策的活，由 plan 阶段产出。
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
