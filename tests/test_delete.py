import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

import codex_move_session.storage as storage
from codex_move_session.delete import build_deletion_plan
from codex_move_session.storage import (
    ApplyError,
    ConcurrentChangeError,
    DeletionResult,
    ProcessRunningError,
    apply_deletion,
)


@dataclass(frozen=True)
class DeleteFixture:
    home: Path
    state: Path
    rollout: Path
    global_state: Path
    memories: Path


def _create_thread_tables(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, title TEXT, cwd TEXT, archived INTEGER,
            updated_at_ms INTEGER, rollout_path TEXT, sandbox_policy TEXT
        );
        CREATE TABLE thread_dynamic_tools (thread_id TEXT, tool TEXT);
        CREATE TABLE thread_goals (thread_id TEXT, goal TEXT);
        CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT);
        CREATE TABLE agent_job_items (id TEXT, assigned_thread_id TEXT, payload TEXT);
        CREATE TABLE automation_runs (id TEXT, thread_id TEXT);
        CREATE TABLE inbox_items (id TEXT, thread_id TEXT);
        """
    )


def create_delete_fixture(
    tmp_path: Path,
    *,
    rollout_path: Path | None = None,
    shared_rollout: bool = False,
) -> DeleteFixture:
    home = tmp_path / ".codex"
    rollout = rollout_path or home / "sessions" / "2026" / "07" / "thread-1.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text('{"type":"session_meta","payload":{"id":"thread-1"}}\n')

    state = home / "sqlite" / "codex.db"
    state.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state) as db:
        _create_thread_tables(db)
        db.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("thread-1", "Delete me", str(tmp_path / "missing"), 0, 100, str(rollout), "{}"),
        )
        db.execute("INSERT INTO thread_dynamic_tools VALUES ('thread-1', 'shell')")
        db.execute("INSERT INTO thread_goals VALUES ('thread-1', 'goal')")
        db.execute("INSERT INTO thread_spawn_edges VALUES ('thread-1', 'child')")
        db.execute("INSERT INTO agent_job_items VALUES ('job-1', 'thread-1', 'keep')")
        db.execute("INSERT INTO automation_runs VALUES ('run-1', 'thread-1')")
        db.execute("INSERT INTO inbox_items VALUES ('inbox-1', 'thread-1')")
        if shared_rollout:
            db.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("thread-2", "Keep me", str(tmp_path), 0, 200, str(rollout), "{}"),
            )

    memories = home / "memories_1.sqlite"
    with sqlite3.connect(memories) as db:
        db.execute("CREATE TABLE stage1_outputs (thread_id TEXT, raw_memory TEXT)")
        db.execute("INSERT INTO stage1_outputs VALUES ('thread-1', 'memory')")

    global_state = home / ".codex-global-state.json"
    global_state.write_text(
        json.dumps(
            {
                "electron-saved-workspace-roots": [str(tmp_path / "missing")],
                "thread-workspace-root-hints": {
                    "thread-1": str(tmp_path / "missing"),
                    "thread-2": str(tmp_path / "keep"),
                },
                "prompt-history": ["keep this"],
            }
        )
    )
    return DeleteFixture(home, state, rollout, global_state, memories)


def thread_count(path: Path, thread_id: str) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM threads WHERE id = ?", (thread_id,)).fetchone()[0]


def related_count(path: Path, table: str, thread_id: str) -> int:
    with sqlite3.connect(path) as db:
        return db.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE thread_id = ?', (thread_id,)
        ).fetchone()[0]


def assigned_thread(path: Path, job_id: str) -> str | None:
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT assigned_thread_id FROM agent_job_items WHERE id = ?", (job_id,)
        ).fetchone()[0]


def memory_count(path: Path, thread_id: str) -> int:
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT COUNT(*) FROM stage1_outputs WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]


def test_apply_deletion_removes_rows_and_file_and_keeps_backup(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")

    result: DeletionResult = apply_deletion(plan, process_checker=lambda: [])

    assert thread_count(fixture.state, "thread-1") == 0
    assert related_count(fixture.state, "thread_goals", "thread-1") == 0
    assert assigned_thread(fixture.state, "job-1") is None
    assert memory_count(fixture.memories, "thread-1") == 0
    assert not fixture.rollout.exists()
    assert result.backup_dir.joinpath("manifest.json").is_file()
    manifest = json.loads(result.backup_dir.joinpath("manifest.json").read_text())
    assert manifest["action"] == "delete"
    assert manifest["session_id"] == "thread-1"


def test_apply_deletion_refuses_concurrent_database_change(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    with sqlite3.connect(fixture.state) as db:
        db.execute("UPDATE threads SET title='changed' WHERE id='thread-1'")

    with pytest.raises(ConcurrentChangeError):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.rollout.exists()


def test_apply_deletion_rolls_back_when_file_remove_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    original = fixture.rollout.read_bytes()
    plan = build_deletion_plan(fixture.home, "thread-1")

    def fail_remove(path: Path) -> None:
        raise OSError("simulated remove failure")

    monkeypatch.setattr(storage, "_remove_file", fail_remove)
    with pytest.raises(ApplyError, match="restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert thread_count(fixture.state, "thread-1") == 1
    assert fixture.rollout.read_bytes() == original


def test_apply_deletion_refuses_running_codex(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")

    with pytest.raises(ProcessRunningError):
        apply_deletion(plan, process_checker=lambda: ["Codex"])


def test_build_deletion_plan_finds_all_related_data(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert plan.session is not None
    assert plan.session.id == "thread-1"
    assert {(item.table, item.action) for item in plan.database_actions} == {
        ("threads", "delete"),
        ("thread_dynamic_tools", "delete"),
        ("thread_goals", "delete"),
        ("thread_spawn_edges", "delete"),
        ("stage1_outputs", "delete"),
        ("agent_job_items", "clear"),
        ("automation_runs", "delete"),
        ("inbox_items", "delete"),
    }
    assert [item.path for item in plan.file_deletions] == [fixture.rollout]
    deletion = plan.file_deletions[0]
    assert deletion.original_digest == hashlib.sha256(deletion.original).hexdigest()
    assert len(plan.file_updates) == 1
    update = plan.file_updates[0]
    assert update.area == "global-state-delete"
    updated = json.loads(update.updated)
    assert "thread-1" not in updated["thread-workspace-root-hints"]
    assert updated["thread-workspace-root-hints"]["thread-2"] == str(tmp_path / "keep")
    assert updated["prompt-history"] == ["keep this"]
    assert plan.deleted_rows == 7
    assert plan.cleared_assignments == 1
    assert plan.has_changes
    assert not plan.errors


def test_deletion_plan_collects_duplicate_threads_from_every_database(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    legacy = fixture.home / "state_5.sqlite"
    with sqlite3.connect(legacy) as db:
        _create_thread_tables(db)
        db.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-1",
                "Legacy copy",
                str(tmp_path / "older"),
                0,
                50,
                str(fixture.rollout),
                "{}",
            ),
        )
        db.execute("INSERT INTO thread_goals VALUES ('thread-1', 'legacy goal')")

    plan = build_deletion_plan(fixture.home, "thread-1")

    thread_actions = [item for item in plan.database_actions if item.table == "threads"]
    assert {item.path for item in thread_actions} == {fixture.state, legacy}
    assert sum(item.row_count for item in thread_actions) == 2
    assert not plan.errors


def test_deletion_plan_rejects_shared_rollout(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path, shared_rollout=True)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("referenced by another session" in error for error in plan.errors)
    assert not plan.file_deletions


def test_deletion_plan_rejects_rollout_outside_codex_home(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    fixture = create_delete_fixture(tmp_path, rollout_path=outside)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("outside the Codex session directories" in error for error in plan.errors)
    assert not plan.file_deletions


def test_deletion_plan_rejects_relative_rollout(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    with sqlite3.connect(fixture.state) as db:
        db.execute("UPDATE threads SET rollout_path = 'relative.jsonl' WHERE id = 'thread-1'")

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("not absolute" in error for error in plan.errors)
    assert not plan.file_deletions


def test_deletion_plan_rejects_symlinked_rollout(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    target = fixture.rollout.with_name("target.jsonl")
    fixture.rollout.rename(target)
    fixture.rollout.symlink_to(target)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("symlink" in error for error in plan.errors)
    assert not plan.file_deletions


def test_deletion_plan_warns_when_rollout_is_missing(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    fixture.rollout.unlink()

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("not found" in warning for warning in plan.warnings)
    assert not plan.errors


def test_deletion_plan_requires_existing_thread(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)

    plan = build_deletion_plan(fixture.home, "missing")

    assert plan.session is None
    assert any("not found" in error for error in plan.errors)
    assert not plan.database_actions
    assert not plan.file_deletions
    assert not plan.file_updates


def test_deletion_plan_reports_database_and_global_state_read_errors(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    (fixture.home / "memories_2.sqlite").write_text("not sqlite")
    fixture.global_state.write_text("[]")

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("could not inspect database" in error for error in plan.errors)
    assert any("expected a JSON object" in error for error in plan.errors)


def test_deletion_plan_does_not_create_sqlite_sidecars(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    before = {path.relative_to(fixture.home) for path in fixture.home.rglob("*")}

    build_deletion_plan(fixture.home, "thread-1")

    after = {path.relative_to(fixture.home) for path in fixture.home.rglob("*")}
    assert after == before


def test_deletion_plan_rejects_external_database_symlink(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    external = tmp_path / "external.sqlite"
    with sqlite3.connect(external) as db:
        _create_thread_tables(db)
        db.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-1",
                "External copy",
                str(tmp_path / "external-project"),
                0,
                50,
                str(fixture.rollout),
                "{}",
            ),
        )
        db.execute("INSERT INTO thread_goals VALUES ('thread-1', 'external')")
    linked = fixture.home / "state_5.sqlite"
    linked.symlink_to(external)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any(str(linked) in error and "symlink" in error for error in plan.errors)
    assert all(action.path != linked for action in plan.database_actions)
    assert all("external" not in repr(action.original_rows) for action in plan.database_actions)


def test_deletion_plan_rejects_external_global_state_symlink(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    external = tmp_path / "external-global.json"
    external.write_text(json.dumps({"thread-workspace-root-hints": {"thread-1": "external"}}))
    fixture.global_state.unlink()
    fixture.global_state.symlink_to(external)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any(str(fixture.global_state) in error and "symlink" in error for error in plan.errors)
    assert not plan.file_updates


def test_deletion_plan_reports_discovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)

    def fail_discovery(_home: Path) -> list[object]:
        raise RuntimeError("snapshot changed")

    monkeypatch.setattr("codex_move_session.delete.discover_sessions", fail_discovery)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert plan.session is None
    assert any("could not discover sessions" in error for error in plan.errors)


def test_deletion_plan_reports_rollout_stat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    original_lstat = Path.lstat

    def fail_rollout_lstat(path: Path, *args: object, **kwargs: object):
        if path == fixture.rollout:
            raise PermissionError("denied")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fail_rollout_lstat)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("could not inspect rollout path" in error for error in plan.errors)
    assert not plan.warnings
    assert not plan.file_deletions


def test_deletion_plan_reports_rollout_resolve_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    original_resolve = Path.resolve

    def fail_rollout_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == fixture.rollout:
            raise RuntimeError("symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_rollout_resolve)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("could not resolve rollout path" in error for error in plan.errors)
    assert not plan.file_deletions
