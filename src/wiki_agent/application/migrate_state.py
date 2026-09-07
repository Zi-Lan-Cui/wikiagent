"""One-time migration of legacy operational state into ``workspace/state.db``."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from wiki_agent.config import load_config
from wiki_agent.issues.migration import migrate_legacy_issues
from wiki_agent.issues.store import IssueStore


def _merge_issues_database(old_database: Path, state_database: Path) -> bool:
    """Merge the former issues database once without replacing newer state."""
    if not old_database.is_file() or not state_database.is_file():
        return False
    with sqlite3.connect(state_database) as connection:
        imported = connection.execute(
            "SELECT value FROM issue_meta WHERE key = 'legacy_issues_db_imported'"
        ).fetchone()
        if imported is not None:
            return False
        connection.execute("ATTACH DATABASE ? AS legacydb", (str(old_database),))
        connection.execute("INSERT OR IGNORE INTO issues SELECT * FROM legacydb.issues")
        connection.execute(
            """
            INSERT INTO issue_events(issue_id, event, payload_json, created_at)
            SELECT issue_id, event, payload_json, created_at FROM legacydb.issue_events
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO issue_actions SELECT * FROM legacydb.issue_actions"
        )
        connection.execute(
            "INSERT OR IGNORE INTO legacy_issue_imports SELECT * FROM legacydb.legacy_issue_imports"
        )
        connection.execute(
            "INSERT OR REPLACE INTO issue_meta(key, value) VALUES('legacy_issues_db_imported', '1')"
        )
    return True


def main() -> None:
    config = load_config(project_root=Path.cwd())
    workspace = config.paths.resolved_workspace_dir()
    old_database = workspace / "issues.db"
    state_database = workspace / "state.db"
    copied = old_database.is_file() and not state_database.exists()
    if copied:
        shutil.copy2(old_database, state_database)
        print(f"已复制旧问题数据库: {old_database.name} -> {state_database.name}")

    store = IssueStore(workspace)
    if not copied and _merge_issues_database(old_database, state_database):
        print(f"已合并旧问题数据库: {old_database.name}")
    counts = migrate_legacy_issues(workspace, store)
    print(
        "旧文件导入完成: "
        f"queue={counts['queue']} corrections={counts['corrections']} skipped={counts['skipped']}"
    )


if __name__ == "__main__":
    main()
