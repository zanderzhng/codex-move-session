import sqlite3
from pathlib import Path

from codex_move_session.discovery import discover_sessions, stale_groups


def create_threads_db(path: Path, rows: list[tuple[str, str, str, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                title TEXT,
                cwd TEXT,
                archived INTEGER,
                updated_at_ms INTEGER,
                rollout_path TEXT,
                sandbox_policy TEXT
            )"""
        )
        db.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, '', '{}')",
            rows,
        )


def test_discovery_reads_legacy_and_sqlite_databases_and_deduplicates(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    stale = tmp_path / "moved"
    create_threads_db(
        home / "state_5.sqlite",
        [("same", "Legacy title", str(stale), 0, 100), ("legacy", "Legacy", str(stale), 1, 50)],
    )
    create_threads_db(
        home / "sqlite" / "codex-dev.db",
        [("same", "Current title", str(stale), 0, 200), ("new", "New", str(stale), 0, 300)],
    )

    sessions = discover_sessions(home)

    assert [session.id for session in sessions] == ["new", "same", "legacy"]
    duplicate = next(session for session in sessions if session.id == "same")
    assert duplicate.title == "Current title"
    assert len(duplicate.records) == 2


def test_stale_groups_filter_active_archived_or_all(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    missing = tmp_path / "missing"
    existing = tmp_path / "existing"
    existing.mkdir()
    create_threads_db(
        home / "state_5.sqlite",
        [
            ("active", "Active", str(missing), 0, 300),
            ("archived", "Archived", str(missing), 1, 200),
            ("present", "Present", str(existing), 0, 100),
        ],
    )
    sessions = discover_sessions(home)

    assert [group.count for group in stale_groups(sessions, scope="active")] == [1]
    assert [group.count for group in stale_groups(sessions, scope="archived")] == [1]
    assert [group.count for group in stale_groups(sessions, scope="all")] == [2]


def test_discovery_includes_rollout_without_database_and_recovers_title(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    rollout = home / "sessions" / "2026" / "rollout-thread-orphan.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"thread-orphan","cwd":"/moved"}}\n'
        '{"type":"response_item","payload":{"text":"Recovered title"}}\n'
    )

    session = discover_sessions(home)[0]

    assert session.id == "thread-orphan"
    assert session.title == "Recovered title"
    assert session.records == ()
    assert session.rollouts[0].path == rollout
    assert session.issues == ("rollout_without_database",)
