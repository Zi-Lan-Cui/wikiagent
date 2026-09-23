"""Settlement 契约：每个结算类别都有产生方、都有明确的账本归宿。

新增/删除 Settlement 枚举值时本文件失败——防止出现"申报了没人产"或
"产生了没人管账"的悬空类别（方案 A 收口时 restructure 粒度失配的教训：
约定要靠测试钉，不靠人肉数）。

直接运行:  .venv/bin/python test/test_settlement_contract.py
"""

from pathlib import Path

from wiki_agent.jobs.models import Settlement
from wiki_agent.jobs.outcomes import ISSUE_RULES

# 显式"不碰账本"名单：成功但语义上不产生账本动作的类别
NO_LEDGER = {Settlement.REFINED, Settlement.UNIT_MISSING, Settlement.APPLIED}

# 产生方所在的执行体目录（handlers 在这里申报 detail["settlement"]）
_PRODUCER_DIRS = ("sync", "application")


def _package_root() -> Path:
    import wiki_agent

    return Path(wiki_agent.__file__).parent


def test_every_settlement_has_a_producer():
    text = "\n".join(
        f.read_text(encoding="utf-8")
        for d in _PRODUCER_DIRS
        for f in (_package_root() / d).rglob("*.py")
    )
    for member in Settlement:
        assert f"Settlement.{member.name}" in text, f"{member.value} 没有任何产生方"


def test_every_settlement_has_a_ledger_placement():
    for member in Settlement:
        assert member in ISSUE_RULES or member in NO_LEDGER, (
            f"{member.value} 既不在 ISSUE_RULES 也不在显式不碰账本名单"
        )
    # NO_LEDGER 里的成员必须真的没有规则
    assert not NO_LEDGER & set(ISSUE_RULES)


if __name__ == "__main__":
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except Exception:
                failed += 1
                print(f"  ✗ {name}")
                traceback.print_exc()
    raise SystemExit(1 if failed else 0)
