import hashlib
import json
import sqlite3
import stat
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import codex_move_session.storage as storage
from codex_move_session.delete import build_deletion_plan
from codex_move_session.storage import (
    ApplyError,
    ConcurrentChangeError,
    DeletionResult,
    PlanValidationError,
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


def assigned_thread_count(path: Path, job_id: str, thread_id: str | None) -> int:
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT COUNT(*) FROM agent_job_items WHERE id = ? AND assigned_thread_id IS ?",
            (job_id, thread_id),
        ).fetchone()[0]


def memory_count(path: Path, thread_id: str) -> int:
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT COUNT(*) FROM stage1_outputs WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]


def assert_delete_fixture_restored(fixture: DeleteFixture) -> None:
    assert thread_count(fixture.state, "thread-1") == 1
    for table in (
        "thread_dynamic_tools",
        "thread_goals",
        "automation_runs",
        "inbox_items",
    ):
        assert related_count(fixture.state, table, "thread-1") == 1
    with sqlite3.connect(fixture.state) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM thread_spawn_edges "
                "WHERE parent_thread_id='thread-1' OR child_thread_id='thread-1'"
            ).fetchone()[0]
            == 1
        )
    assert assigned_thread(fixture.state, "job-1") == "thread-1"
    assert memory_count(fixture.memories, "thread-1") == 1


def test_apply_deletion_removes_rows_and_file_and_keeps_backup(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    original_rollout = fixture.rollout.read_bytes()
    original_global_state = fixture.global_state.read_bytes()
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
    assert {item["original"] for item in manifest["databases"]} == {
        str(fixture.state),
        str(fixture.memories),
    }
    assert {item["original"] for item in manifest["files"]} == {
        str(fixture.rollout),
        str(fixture.global_state),
    }
    backup_files = {
        item["original"]: result.backup_dir / item["backup"] for item in manifest["files"]
    }
    assert backup_files[str(fixture.rollout)].read_bytes() == original_rollout
    assert backup_files[str(fixture.global_state)].read_bytes() == original_global_state
    backup_databases = {
        item["original"]: result.backup_dir / item["backup"] for item in manifest["databases"]
    }
    assert thread_count(backup_databases[str(fixture.state)], "thread-1") == 1
    assert memory_count(backup_databases[str(fixture.memories)], "thread-1") == 1


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
    original_global_state = fixture.global_state.read_bytes()
    plan = build_deletion_plan(fixture.home, "thread-1")

    def fail_remove(path: Path, **_kwargs: object) -> None:
        raise OSError("simulated remove failure")

    monkeypatch.setattr(storage, "_remove_file", fail_remove)
    with pytest.raises(ApplyError, match="restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.read_bytes() == original
    assert fixture.global_state.read_bytes() == original_global_state
    backups = list((fixture.home / "backups").iterdir())
    assert len(backups) == 1
    assert backups[0].joinpath("manifest.json").is_file()


def test_apply_deletion_refuses_running_codex(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")

    with pytest.raises(ProcessRunningError):
        apply_deletion(plan, process_checker=lambda: ["Codex"])

    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()
    assert fixture.global_state.exists()
    assert not fixture.home.joinpath("backups").exists()


def test_apply_deletion_rechecks_process_before_mutation(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    checks = 0

    def process_checker() -> list[str]:
        nonlocal checks
        checks += 1
        return [] if checks == 1 else ["Codex"]

    with pytest.raises(ProcessRunningError):
        apply_deletion(plan, process_checker=process_checker)

    assert checks == 2
    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()


def test_apply_deletion_uses_portable_file_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    checks = 0

    def force_fallback() -> bool:
        nonlocal checks
        checks += 1
        return False

    monkeypatch.setattr(storage, "_supports_secure_dir_fd", force_fallback, raising=False)

    apply_deletion(plan, process_checker=lambda: [])

    assert checks > 0
    assert not fixture.rollout.exists()
    assert thread_count(fixture.state, "thread-1") == 0


def test_apply_deletion_refuses_rollout_shared_after_preview(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    with sqlite3.connect(fixture.state) as db:
        db.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("thread-2", "Keep", str(tmp_path), 0, 200, str(fixture.rollout), "{}"),
        )

    with pytest.raises(ConcurrentChangeError, match="referenced by another session"):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.rollout.exists()
    assert thread_count(fixture.state, "thread-1") == 1


def test_apply_deletion_refuses_new_database_after_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_open = storage._open_deletion_transactions

    def add_database_after_locking(plan: object):
        databases = original_open(plan)
        late = fixture.home / "sqlite" / "late.sqlite"
        with sqlite3.connect(late) as db:
            _create_thread_tables(db)
            db.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("thread-2", "Keep", str(tmp_path), 0, 200, str(fixture.rollout), "{}"),
            )
        return databases

    monkeypatch.setattr(storage, "_open_deletion_transactions", add_database_after_locking)

    with pytest.raises(ApplyError, match="database set changed"):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.rollout.exists()
    assert_delete_fixture_restored(fixture)


def test_apply_deletion_refuses_new_database_immediately_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_open = storage._open_planned_file
    deletion_opens = 0

    def add_database_before_unlink(*args: object, **kwargs: object):
        nonlocal deletion_opens
        opened = original_open(*args, **kwargs)
        if kwargs.get("deletion") is not None:
            deletion_opens += 1
            if deletion_opens == 2:
                late = fixture.home / "sqlite" / "late.sqlite"
                with sqlite3.connect(late) as db:
                    _create_thread_tables(db)
                    db.execute(
                        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            "thread-2",
                            "Keep",
                            str(tmp_path),
                            0,
                            200,
                            str(fixture.rollout),
                            "{}",
                        ),
                    )
        return opened

    monkeypatch.setattr(storage, "_open_planned_file", add_database_before_unlink)

    with pytest.raises(ApplyError, match="database set changed"):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.rollout.exists()
    assert_delete_fixture_restored(fixture)


def test_apply_deletion_refuses_parent_redirect_after_preview(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    external_sessions = tmp_path / "external-sessions"
    external_rollout = external_sessions / "2026" / "07" / "thread-1.jsonl"
    external_rollout.parent.mkdir(parents=True)
    external_rollout.write_bytes(fixture.rollout.read_bytes())
    fixture.home.joinpath("sessions").rename(fixture.home / "original-sessions")
    fixture.home.joinpath("sessions").symlink_to(external_sessions, target_is_directory=True)

    with pytest.raises(ConcurrentChangeError, match="symlink"):
        apply_deletion(plan, process_checker=lambda: [])

    assert external_rollout.exists()
    assert thread_count(fixture.state, "thread-1") == 1


def test_apply_deletion_refuses_database_change_after_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_backup = storage._create_deletion_backup

    def change_after_backup(plan: object):
        result = original_backup(plan)
        with sqlite3.connect(fixture.state) as db:
            db.execute("UPDATE threads SET title='changed' WHERE id='thread-1'")
        return result

    monkeypatch.setattr(storage, "_create_deletion_backup", change_after_backup)

    with pytest.raises(ApplyError, match="restored"):
        apply_deletion(plan, process_checker=lambda: [])

    with sqlite3.connect(fixture.state) as db:
        title = db.execute("SELECT title FROM threads WHERE id='thread-1'").fetchone()[0]
        assert title == "changed"
    assert fixture.rollout.exists()


def test_apply_deletion_refuses_file_change_after_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_backup = storage._create_deletion_backup

    def change_after_backup(plan: object):
        result = original_backup(plan)
        fixture.rollout.write_text("changed by another process\n")
        return result

    monkeypatch.setattr(storage, "_create_deletion_backup", change_after_backup)

    with pytest.raises(ApplyError, match="rollback was incomplete"):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.rollout.read_text() == "changed by another process\n"
    assert_delete_fixture_restored(fixture)


def test_apply_deletion_refuses_file_identity_change_after_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_backup = storage._create_deletion_backup
    replacement_inode = 0

    def replace_after_backup(plan: object):
        nonlocal replacement_inode
        result = original_backup(plan)
        replacement = fixture.global_state.with_suffix(".replacement")
        replacement.write_bytes(fixture.global_state.read_bytes())
        replacement.replace(fixture.global_state)
        replacement_inode = fixture.global_state.stat().st_ino
        return result

    monkeypatch.setattr(storage, "_create_deletion_backup", replace_after_backup)

    with pytest.raises(ApplyError, match="restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.global_state.stat().st_ino == replacement_inode
    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()


def test_apply_deletion_refuses_update_replacement_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    replacement_inode = 0
    replacement_content = b""

    def replace_update_and_fail(*_args: object, **_kwargs: object) -> None:
        nonlocal replacement_inode, replacement_content
        replacement_content = fixture.global_state.read_bytes()
        replacement = fixture.global_state.with_suffix(".replacement")
        replacement.write_bytes(replacement_content)
        replacement.replace(fixture.global_state)
        replacement_inode = fixture.global_state.stat().st_ino
        raise RuntimeError("simulated verification failure")

    monkeypatch.setattr(storage, "_verify_deletion", replace_update_and_fail)

    with pytest.raises(ApplyError, match="rollback was incomplete"):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.global_state.stat().st_ino == replacement_inode
    assert fixture.global_state.read_bytes() == replacement_content
    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()


def test_apply_deletion_checks_exact_rows_inside_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_read = storage._read_action_rows
    changed = False

    def change_inside_transaction(db: sqlite3.Connection, action: object):
        nonlocal changed
        if db.in_transaction and not changed and action.table == "threads":
            db.execute("UPDATE threads SET title='changed' WHERE id='thread-1'")
            changed = True
        return original_read(db, action)

    monkeypatch.setattr(storage, "_read_action_rows", change_inside_transaction)

    with pytest.raises(ApplyError, match="restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()


def test_apply_deletion_rolls_back_when_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    original_rollout = fixture.rollout.read_bytes()
    original_rollout_mode = stat.S_IMODE(fixture.rollout.stat().st_mode)
    original_global_state = fixture.global_state.read_bytes()
    plan = build_deletion_plan(fixture.home, "thread-1")

    def fail_verification(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated verification failure")

    monkeypatch.setattr(storage, "_verify_deletion", fail_verification)

    with pytest.raises(ApplyError, match="restored") as raised:
        apply_deletion(plan, process_checker=lambda: [])

    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.read_bytes() == original_rollout
    assert stat.S_IMODE(fixture.rollout.stat().st_mode) == original_rollout_mode
    assert fixture.global_state.read_bytes() == original_global_state
    assert raised.value.backup_dir.joinpath("manifest.json").is_file()


def test_apply_deletion_rolls_back_when_database_apply_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    original_rollout = fixture.rollout.read_bytes()
    original_global_state = fixture.global_state.read_bytes()
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_apply = storage._apply_deletion_actions
    calls = 0

    def fail_database_apply(*args: object, **kwargs: object) -> None:
        nonlocal calls
        original_apply(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("simulated database failure")

    monkeypatch.setattr(storage, "_apply_deletion_actions", fail_database_apply)

    with pytest.raises(ApplyError, match="restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.read_bytes() == original_rollout
    assert fixture.global_state.read_bytes() == original_global_state


def test_apply_deletion_partial_commit_restores_only_touched_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_commit = storage._commit_deletion_database

    def fail_after_first_commit(db: sqlite3.Connection, path: Path) -> None:
        if path == fixture.memories:
            original_commit(db, path)
            with sqlite3.connect(fixture.memories) as other:
                other.execute("INSERT INTO stage1_outputs VALUES ('thread-2', 'unrelated')")
            return
        raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(storage, "_commit_deletion_database", fail_after_first_commit)

    with pytest.raises(ApplyError, match="restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert_delete_fixture_restored(fixture)
    assert memory_count(fixture.memories, "thread-2") == 1
    assert fixture.rollout.exists()


def test_apply_deletion_partial_commit_restores_duplicate_clear_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    with sqlite3.connect(fixture.state) as db:
        db.execute("INSERT INTO agent_job_items VALUES ('job-1', 'thread-1', 'keep')")
    legacy = fixture.home / "state_5.sqlite"
    with sqlite3.connect(legacy) as db:
        _create_thread_tables(db)
        db.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-1",
                "Legacy",
                str(tmp_path),
                0,
                50,
                str(fixture.rollout),
                "{}",
            ),
        )
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_commit = storage._commit_deletion_database

    def fail_last_commit(db: sqlite3.Connection, path: Path) -> None:
        if path == legacy:
            raise sqlite3.OperationalError("simulated final commit failure")
        original_commit(db, path)

    monkeypatch.setattr(storage, "_commit_deletion_database", fail_last_commit)

    with pytest.raises(ApplyError, match="touched data restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert assigned_thread_count(fixture.state, "job-1", "thread-1") == 2
    assert thread_count(fixture.state, "thread-1") == 1
    assert fixture.rollout.exists()


def test_apply_deletion_rejects_invalid_database_action(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    actions = list(plan.database_actions)
    actions[0] = replace(actions[0], table='threads"; DROP TABLE threads; --')
    invalid_plan = replace(plan, database_actions=tuple(actions))

    with pytest.raises(PlanValidationError, match="unsafe SQLite identifier"):
        apply_deletion(invalid_plan, process_checker=lambda: [])

    assert not fixture.home.joinpath("backups").exists()


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


def test_deletion_plan_rejects_symlinked_session_root(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    external_sessions = tmp_path / "external-sessions"
    fixture.home.joinpath("sessions").rename(external_sessions)
    fixture.home.joinpath("sessions").symlink_to(external_sessions, target_is_directory=True)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("session directory is a symlink" in error for error in plan.errors)
    assert not plan.file_deletions


def test_deletion_plan_rejects_symlinked_rollout_ancestry(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    external_year = tmp_path / "external-year"
    fixture.home.joinpath("sessions", "2026").rename(external_year)
    fixture.home.joinpath("sessions", "2026").symlink_to(external_year, target_is_directory=True)

    plan = build_deletion_plan(fixture.home, "thread-1")

    assert any("symlinked ancestry" in error for error in plan.errors)
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
