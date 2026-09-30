"""维护单元的声明模型：in 页集合 → out 页清单。

单元是提议、确认、入队、执行、撤销的同一粒度；payload 只带声明
（章节归属在运行时计算，全文与分配表不进 payload）。合并/拆分/新建/
改写/删除都是它的形状特例：out 单页=合并吸收，in 单页多 out=拆分，
in==out=改写，out 空=删除。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Take:
    """确定性搬运：out 页只准用 in 页的指定章节，LLM 不得增删。"""

    from_slug: str
    sections: list[str]  # 章节标题（"## " 后的标题文本；""= 正文头部）


@dataclass(frozen=True, slots=True)
class OutPage:
    """输出页声明。intent 描述这一页将来讲什么（路由与重写的依据）。"""

    slug: str
    intent: str = ""
    take: list[Take] = field(default_factory=list)
    polish: bool = True  # False=按 take 装配即成品，不过 LLM 成文

    @staticmethod
    def from_dict(raw: dict) -> OutPage:
        take = [Take(from_slug=str(t.get("from_slug") or ""), sections=[str(s) for s in t.get("sections") or []]) for t in raw.get("take") or [] if isinstance(t, dict)]
        intent = str(raw.get("intent") or "")
        return OutPage(
            slug=str(raw.get("slug") or ""),
            intent=intent,
            take=take,
            # 有 take 才允许不过成文；无 take 的声明必须经 LLM 成文
            polish=bool(raw.get("polish", True)) if take else True,
        )


@dataclass(frozen=True, slots=True)
class Unit:
    """一个维护单元：in 页被整体消费，产出 out 页清单（空=删除）。"""

    in_pages: list[str]
    out: list[OutPage]
    reason: str = ""

    @property
    def out_slugs(self) -> list[str]:
        return [p.slug for p in self.out]

    def to_dict(self) -> dict:
        return {
            "in_pages": list(self.in_pages),
            "out": [
                {
                    "slug": p.slug,
                    "intent": p.intent,
                    "take": [
                        {"from_slug": t.from_slug, "sections": list(t.sections)}
                        for t in p.take
                    ],
                    "polish": p.polish,
                }
                for p in self.out
            ],
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(raw: object) -> Unit:
        """从 payload 对象还原声明；非法形状抛 TypeError/ValueError。"""
        if not isinstance(raw, dict):
            raise TypeError(f"单元声明必须是对象，收到 {type(raw).__name__}")
        return Unit(
            in_pages=[str(s) for s in raw.get("in_pages") or []],
            out=[OutPage.from_dict(p) for p in raw.get("out") or [] if isinstance(p, dict)],
            reason=str(raw.get("reason") or ""),
        )

    # 供核对与执行的派生集合
    @property
    def vanished(self) -> list[str]:
        """被消费且不再产出的页——链接与 index 的机械收尾对象。"""
        out = set(self.out_slugs)
        return [s for s in self.in_pages if s not in out]


class UnitError(ValueError):
    """单元声明不合法（提交侧消解与执行侧核对共用）。"""


class UnitMismatchError(UnitError):
    """声明落不上当前盘面：in 页缺失或 out 冲突——排队期间世界变了。"""

    def __init__(self, missing: list[str] | None = None, detail: str = "") -> None:
        super().__init__(detail or f"单元落不上当前盘面: {missing}")
        self.missing = list(missing or [])


class RewriteError(UnitError):
    """逐页成文阶段失败（含重试耗尽后校验仍不过）。"""


class RouteError(UnitError):
    """运行时章节分配不守恒（漏配/重配/空 out/非法目标）——整单元失败。"""
