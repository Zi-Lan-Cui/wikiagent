"""Settlement 契约：每个结算类别都有产生方、都有明确的账本归宿。

新增/删除 Settlement 枚举值时本文件失败：每个值必须既有代码产生它、
又在 ISSUE_RULES 或显式不碰账本名单里。

直接运行:  .venv/bin/python test/test_settlement_contract.py
"""

from pathlib import Path

from wiki_agent.issues.outcomes import ISSUE_RULES
from wiki_agent.queue.models import Settlement

# 不碰 issue 账本的显式名单：成功但不产生 issue 动作的类别
NO_ISSUE_LEDGER: set[Settlement] = {
    Settlement.APPLIED,  # 批操作结果不是"源材料等人修"的待办
    Settlement.LINKED,
    Settlement.UNIT_MISSING,
    Settlement.PREVIEWED,  # 动作是写维护提议账本，见 maintenance/outcomes 的注册
}

# 产生方所在的执行体目录（handlers 在这里申报 detail["settlement"]）
_PRODUCER_DIRS = ("sync", "maintenance", "issues")


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
        assert member in ISSUE_RULES or member in NO_ISSUE_LEDGER, (
            f"{member.value} 既不在 ISSUE_RULES 也不在显式不碰账本名单"
        )
    # NO_ISSUE_LEDGER 里的成员必须真的没有规则
    assert not NO_ISSUE_LEDGER & set(ISSUE_RULES)


def test_production_assembly_registers_all_ledgers():
    """装配工厂的注册面必须盖住全部账本归宿——删掉任一 register 本测试即红。"""
    import tempfile

    from wiki_agent.application.outcome_registry import build_job_outcomes
    from wiki_agent.database import Database
    from wiki_agent.issues import IssueStore
    from wiki_agent.maintenance import ProposalStore
    from wiki_agent.sync.state import SyncState

    with tempfile.TemporaryDirectory() as tmp:
        handler = build_job_outcomes(
            issue_store=IssueStore(Database(tmp)),
            sync_state=SyncState(f"{tmp}/state.json"),
            source_records_dir=f"{tmp}/records",
            proposals=ProposalStore(Database(tmp)),
        )
    settlements, errors, any_success = handler.coverage()
    assert ISSUE_RULES.keys() | {Settlement.PREVIEWED} <= settlements
    assert any_success  # sync 的完成账按"任意成功"注册
    assert "ingest_error" in errors


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
