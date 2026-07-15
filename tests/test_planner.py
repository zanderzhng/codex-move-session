import json
import sqlite3
from pathlib import Path

from codex_move_session.planner import build_plan


def create_codex_fixture(home: Path, old: Path, *, archived: bool = False) -> tuple[Path, Path]:
    rollout = home / "sessions" / "2026" / "01" / "rollout-thread-1.jsonl"
    rollout.parent.mkdir(parents=True)
    lines = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(old)}},
        {
            "type": "response_item",
            "payload": {"text": f"run --cwd {old}/src but keep {old}-copy"},
        },
    ]
    rollout.write_bytes(b"\r\n".join(json.dumps(line).encode() for line in lines) + b"\r\n")

    state = home / "state_5.sqlite"
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state) as db:
        db.execute(
            """CREATE TABLE threads (
                id TEXT PRIMARY KEY, title TEXT, cwd TEXT, archived INTEGER,
                updated_at_ms INTEGER, rollout_path TEXT, sandbox_policy TEXT
            )"""
        )
        db.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-1",
                "Moved project",
                str(old),
                int(archived),
                100,
                str(rollout),
                json.dumps({"writable_roots": [str(old / "tmp")]}),
            ),
        )

    memories = home / "memories_1.sqlite"
    with sqlite3.connect(memories) as db:
        db.execute(
            """CREATE TABLE stage1_outputs (
                thread_id TEXT PRIMARY KEY, raw_memory TEXT, rollout_summary TEXT
            )"""
        )
        db.execute(
            "INSERT INTO stage1_outputs VALUES (?, ?, ?)",
            ("thread-1", f"Files live in {old}/src", f"Worked from {old}"),
        )

    global_state = {
        "electron-saved-workspace-roots": [str(old)],
        "project-order": [str(old)],
        "prompt-history": [f"do not rewrite {old}"],
        "thread-workspace-root-hints": {"thread-1": str(old)},
    }
    (home / ".codex-global-state.json").write_text(json.dumps(global_state))
    return state, rollout


def test_plan_repairs_database_rollout_memory_and_global_state(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    _, rollout = create_codex_fixture(home, old)

    plan = build_plan(home, str(old), str(new))

    assert not plan.errors
    assert [session.id for session in plan.sessions] == ["thread-1"]
    assert {(change.table, change.column) for change in plan.database_changes} == {
        ("threads", "cwd"),
        ("threads", "sandbox_policy"),
        ("stage1_outputs", "raw_memory"),
        ("stage1_outputs", "rollout_summary"),
    }
    rollout_change = next(change for change in plan.file_changes if change.path == rollout)
    assert b"new-project/src" in rollout_change.updated
    assert b"old-project-copy" in rollout_change.updated
    assert rollout_change.updated.endswith(b"\r\n")
    global_change = next(
        change for change in plan.file_changes if change.path.name == ".codex-global-state.json"
    )
    updated_state = json.loads(global_change.updated)
    assert updated_state["electron-saved-workspace-roots"] == [str(new)]
    assert updated_state["prompt-history"] == [f"do not rewrite {old}"]
    assert plan.replacement_count >= 8


def test_plan_excludes_archived_sessions_by_default(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    create_codex_fixture(home, old, archived=True)

    default_plan = build_plan(home, str(old), str(new))
    archived_plan = build_plan(home, str(old), str(new), include_archived=True)

    assert not default_plan.sessions
    assert archived_plan.sessions[0].id == "thread-1"


def test_plan_reports_malformed_affected_rollout(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    _, rollout = create_codex_fixture(home, old)
    rollout.write_text(f'{{"broken":"{old}\n')

    plan = build_plan(home, str(old), str(new))

    assert any("invalid JSON" in error for error in plan.errors)


def test_plan_reports_json_key_collision_in_rollout(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    _, rollout = create_codex_fixture(home, old)
    collision = {"type": "event", "payload": {str(old): "old", str(new): "new"}}
    rollout.write_text(json.dumps(collision) + "\n")

    plan = build_plan(home, str(old), str(new))

    assert any("path-key collision" in error for error in plan.errors)


def test_plan_reports_unreadable_memory_database(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    create_codex_fixture(home, old)
    (home / "memories_2.sqlite").write_text("not a database")

    plan = build_plan(home, str(old), str(new))

    assert any("could not inspect memory database" in error for error in plan.errors)
