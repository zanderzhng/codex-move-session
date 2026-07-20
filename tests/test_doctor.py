import sqlite3
from pathlib import Path

from test_planner import create_codex_fixture

from codex_move_session.doctor import build_doctor_plan
from codex_move_session.storage import apply_plan


def test_doctor_repairs_rollout_path_and_missing_index_entry(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    project = tmp_path / "project"
    state, rollout = create_codex_fixture(home, project)
    with sqlite3.connect(state) as db:
        db.execute("UPDATE threads SET rollout_path = '/missing/rollout.jsonl'")
    index = home / "session_index.jsonl"
    index.write_text("")

    plan = build_doctor_plan(home)

    assert any(issue.code == "rollout_path_mismatch" for issue in plan.issues)
    assert any(issue.code == "missing_session_index_entry" for issue in plan.issues)
    apply_plan(plan.migration_plan(), process_checker=lambda: [])

    with sqlite3.connect(state) as db:
        assert db.execute("SELECT rollout_path FROM threads").fetchone()[0] == str(rollout)
    assert '"id":"thread-1"' in index.read_text()


def test_doctor_creates_missing_session_index(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    project = tmp_path / "project"
    create_codex_fixture(home, project)

    plan = build_doctor_plan(home)

    assert plan.file_changes[0].created
    apply_plan(plan.migration_plan(), process_checker=lambda: [])
    assert '"id":"thread-1"' in (home / "session_index.jsonl").read_text()


def test_doctor_recovers_blank_database_and_index_titles(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    project = tmp_path / "project"
    state, rollout = create_codex_fixture(home, project)
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"thread-1","cwd":"/project"}}\n'
        '{"type":"response_item","payload":{"text":"Recovered title"}}\n'
    )
    with sqlite3.connect(state) as db:
        db.execute("UPDATE threads SET title = ''")
    index = home / "session_index.jsonl"
    index.write_text('{"id":"thread-1","thread_name":"thread-1"}\n')

    plan = build_doctor_plan(home)
    apply_plan(plan.migration_plan(), process_checker=lambda: [])

    with sqlite3.connect(state) as db:
        assert db.execute("SELECT title FROM threads").fetchone()[0] == "Recovered title"
    assert '"thread_name":"Recovered title"' in index.read_text()
