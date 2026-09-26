"""维护单元：声明模型 + 提议/复核/消解 + 执行侧核对装配与落盘。

提交侧与执行侧共用的词汇只有一个：Unit（in 页被整体消费，out 页清单
带 intent/take/polish 声明）。章节归属在运行时计算、随 job detail 审计，
不进 payload。
"""

from .apply import apply_unit, pages_linking_to
from .models import (
    OutPage,
    RouteError,
    Take,
    Unit,
    UnitError,
    UnitMismatchError,
)
from .plan import UnitPlan, prepare_unit
from .propose import propose_units
from .resolve import assert_units_valid, resolve_unit_conflicts, validate_unit
from .review import recheck_units
from .rewrite import rewrite_unit_page
from .sections import Section, page_sections

__all__ = [
    "OutPage",
    "RouteError",
    "Section",
    "Take",
    "Unit",
    "UnitError",
    "UnitMismatchError",
    "UnitPlan",
    "apply_unit",
    "assert_units_valid",
    "pages_linking_to",
    "page_sections",
    "prepare_unit",
    "propose_units",
    "recheck_units",
    "resolve_unit_conflicts",
    "rewrite_unit_page",
    "validate_unit",
]
