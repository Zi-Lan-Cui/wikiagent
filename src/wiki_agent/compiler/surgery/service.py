"""兼容导入面——结构手术全流程已拆至 models/common/proposal/review/resolve/execute/rewrite/transaction。

真实模块（蓝图分层）:
    models       Proposal/SurgeryResult/Conflict/ArbitrationResult + 超参数
    common       页面读取 / 证据收集 / LLM 输出安全解析
    proposal     LLM 粗提
    review       LLM 复判 + 冲突复裁
    resolve      确定性冲突 / 依赖消解
    execute      原子执行
    rewrite      确定性链接改写
    transaction  备份 / 快照 / 回滚

LLM 只在 proposal/review，备份回滚只在 transaction。
本模块迁移期仅作 re-export，零新代码引用后即删除。
"""

from __future__ import annotations

from wiki_agent.compiler.surgery.common import (
    _filter_valid_pages,
    _incoming_links,
    _index_overview,
    _load_pages,
    _safe_parse_json,
)
from wiki_agent.compiler.surgery.execute import (
    _HEADING_RE,
    _append_index_entry,
    _create_page_content,
    _extract_sections,
    _page_type_for_dir,
    _remove_index_entry,
    _section_ranges,
    _trim_sections,
    execute,
    execute_create,
    execute_delete,
    execute_merge,
    execute_trim,
)
from wiki_agent.compiler.surgery.models import (
    _CONTENT_DIRS,
    _PROPOSE_MAX_COUNT,
    _PROPOSE_MAX_TOKENS,
    _RE_ARBITRATE_MAX_TOKENS,
    _RECHECK_BODY_CHARS,
    _RECHECK_MAX_TOKENS,
    ArbitrationResult,
    Conflict,
    Proposal,
    SurgeryResult,
)
from wiki_agent.compiler.surgery.proposal import _check_propose_list, propose_from_index
from wiki_agent.compiler.surgery.resolve import (
    _page_quality,
    _src_of,
    _validate_operation_sequence,
    resolve_conflicts,
)
from wiki_agent.compiler.surgery.review import (
    _check_re_arbitrate,
    _check_recheck,
    _recheck_prompt,
    re_arbitrate,
    recheck,
)
from wiki_agent.compiler.surgery.rewrite import (
    _LINK_RE,
    _plain_source_links,
    _rewrite_links,
    _rewrite_source_links,
)
from wiki_agent.compiler.surgery.transaction import (
    _backup,
    _restore_group,
    _snapshot_group,
)

__all__ = [
    "ArbitrationResult",
    "Conflict",
    "Proposal",
    "SurgeryResult",
    "_CONTENT_DIRS",
    "_PROPOSE_MAX_TOKENS",
    "_PROPOSE_MAX_COUNT",
    "_RECHECK_MAX_TOKENS",
    "_RE_ARBITRATE_MAX_TOKENS",
    "_RECHECK_BODY_CHARS",
    "_load_pages",
    "_filter_valid_pages",
    "_index_overview",
    "_incoming_links",
    "_safe_parse_json",
    "_check_propose_list",
    "propose_from_index",
    "_check_recheck",
    "_recheck_prompt",
    "recheck",
    "_check_re_arbitrate",
    "re_arbitrate",
    "_page_quality",
    "_src_of",
    "resolve_conflicts",
    "_validate_operation_sequence",
    "_rewrite_links",
    "_LINK_RE",
    "_rewrite_source_links",
    "_plain_source_links",
    "_remove_index_entry",
    "_HEADING_RE",
    "_section_ranges",
    "_extract_sections",
    "_trim_sections",
    "_page_type_for_dir",
    "_create_page_content",
    "_append_index_entry",
    "execute_merge",
    "execute_delete",
    "execute_create",
    "execute_trim",
    "_backup",
    "_snapshot_group",
    "_restore_group",
    "execute",
]

