import ctypes
import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import codex_move_session.storage as storage
import codex_move_session.windows_file as windows_file
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


def test_windows_writable_identity_access_includes_read_attributes() -> None:
    assert (
        windows_file._WRITABLE_IDENTITY_ACCESS & windows_file._FILE_READ_ATTRIBUTES
        == windows_file._FILE_READ_ATTRIBUTES
    )


@pytest.mark.parametrize(
    ("pointer_size", "expected_header_size", "expected_name_offset"),
    [(4, 16, 12), (8, 24, 20)],
)
def test_windows_rename_buffer_includes_native_layout(
    pointer_size: int, expected_header_size: int, expected_name_offset: int
) -> None:
    encoded_name = "target.json".encode("utf-16-le")
    header_size, name_offset = windows_file._rename_information_layout(pointer_size)

    buffer = windows_file._build_rename_buffer(header_size, name_offset, encoded_name)

    assert header_size == expected_header_size
    assert name_offset == expected_name_offset
    assert len(buffer) == expected_header_size + len(encoded_name)
    assert buffer[name_offset : name_offset + len(encoded_name)] == encoded_name


def test_windows_rename_dispatch_uses_native_class_flags_and_converts_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object, bytes, int, int]] = []

    def nt_set_information(
        handle: object,
        io_status: object,
        information: object,
        length: int,
        information_class: int,
    ) -> int:
        calls.append((handle, io_status, bytes(information[:length]), length, information_class))
        return -1073741811

    class FakeWinTypes:
        @staticmethod
        def HANDLE(value: object) -> object:
            return value

    monkeypatch.setattr(windows_file, "wintypes", FakeWinTypes, raising=False)
    monkeypatch.setattr(windows_file, "_IoStatusBlock", ctypes.c_byte, raising=False)
    monkeypatch.setattr(windows_file, "_nt_set_information_file", nt_set_information, raising=False)
    monkeypatch.setattr(
        windows_file,
        "_rtl_nt_status_to_dos_error",
        lambda status: 5 if status == -1073741811 else 0,
        raising=False,
    )
    monkeypatch.setattr(
        windows_file.ctypes,
        "WinError",
        lambda code: OSError(f"converted {code}"),
        raising=False,
    )
    monkeypatch.setattr(
        windows_file,
        "_FileRenameInfoHeader",
        windows_file._FILE_RENAME_INFORMATION_TYPES[ctypes.sizeof(ctypes.c_void_p)],
        raising=False,
    )

    with pytest.raises(OSError, match="converted 5"):
        windows_file._rename_handle(11, 22, "target.json")

    assert len(calls) == 1
    handle, _io_status, encoded_information, length, information_class = calls[0]
    information = windows_file._FileRenameInfoHeader.from_buffer_copy(encoded_information)
    assert handle == 11
    assert length == len(encoded_information)
    assert information_class == 65
    assert information.flags == 3
    assert information.root_directory == 22
    assert information.file_name_length == len("target.json".encode("utf-16-le"))


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


def test_apply_deletion_reports_database_backup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    failed_source: Path | None = None

    def fail_database_backup(source: Path, _destination: Path) -> None:
        nonlocal failed_source
        failed_source = source
        raise sqlite3.OperationalError("simulated database backup failure")

    monkeypatch.setattr(storage, "_backup_database", fail_database_backup)

    with pytest.raises(ApplyError) as raised:
        apply_deletion(plan, process_checker=lambda: [])

    assert failed_source is not None
    assert str(failed_source) in str(raised.value)
    assert "database backup" in str(raised.value)
    assert raised.value.backup_dir.is_dir()
    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()


def test_apply_deletion_reports_file_backup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_write_bytes = Path.write_bytes

    def fail_rollout_backup(path: Path, data: bytes) -> int:
        if path.parent.name == "files" and path.name.endswith(f"{fixture.rollout.name}.bin"):
            raise OSError("simulated file backup failure")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_rollout_backup)

    with pytest.raises(ApplyError) as raised:
        apply_deletion(plan, process_checker=lambda: [])

    assert str(fixture.rollout) in str(raised.value)
    assert "file backup" in str(raised.value)
    assert raised.value.backup_dir.is_dir()
    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()


def test_apply_deletion_reports_manifest_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    original_write_text = Path.write_text

    def fail_manifest(path: Path, *args: object, **kwargs: object) -> int:
        if path.name == "manifest.json" and path.parent.parent.name == "backups":
            raise OSError("simulated manifest write failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_manifest)

    with pytest.raises(ApplyError) as raised:
        apply_deletion(plan, process_checker=lambda: [])

    assert str(raised.value.backup_dir / "manifest.json") in str(raised.value)
    assert "manifest" in str(raised.value)
    assert raised.value.backup_dir.is_dir()
    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()


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

    def fail_remove(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated remove failure")

    monkeypatch.setattr(storage, "_remove_opened_file", fail_remove)
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


@pytest.mark.skipif(
    os.name == "nt", reason="portable pathname replace cannot replace an open Windows target"
)
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
    monkeypatch.setattr(storage, "_uses_windows_handle_delete", lambda: False)

    apply_deletion(plan, process_checker=lambda: [])

    assert checks > 0
    assert not fixture.rollout.exists()
    assert thread_count(fixture.state, "thread-1") == 0


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX substitution simulation; native Win32 coverage follows"
)
def test_windows_fallback_deletes_through_open_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    deletion = plan.file_deletions[0]
    original_inode = fixture.rollout.stat().st_ino
    external_sessions = tmp_path / "external-sessions"
    external_rollout = external_sessions / "2026" / "07" / "thread-1.jsonl"
    external_rollout.parent.mkdir(parents=True)
    external_rollout.write_bytes(fixture.rollout.read_bytes())
    native_delete_calls = 0

    def native_open(path: Path) -> int:
        return os.open(path, os.O_RDONLY)

    def native_delete(fd: int) -> None:
        nonlocal native_delete_calls
        native_delete_calls += 1
        assert os.fstat(fd).st_ino == original_inode
        fixture.home.joinpath("sessions").rename(fixture.home / "original-sessions")
        fixture.home.joinpath("sessions").symlink_to(external_sessions, target_is_directory=True)
        fixture.home.joinpath("original-sessions", "2026", "07", "thread-1.jsonl").unlink()

    monkeypatch.setattr(storage, "_supports_secure_dir_fd", lambda: False)
    monkeypatch.setattr(storage, "_uses_windows_handle_delete", lambda: True, raising=False)
    monkeypatch.setattr(storage, "_open_windows_delete_fd", native_open, raising=False)
    monkeypatch.setattr(storage, "_delete_windows_file", native_delete, raising=False)

    opened = storage._open_planned_file(
        plan.home, deletion.path, deletion.original, deletion=deletion
    )
    try:
        storage._remove_opened_file(plan.home, opened)
    finally:
        opened.close()

    assert native_delete_calls == 1
    assert external_rollout.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 file handles")
def test_windows_native_delete_branch(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")

    apply_deletion(plan, process_checker=lambda: [])

    assert not fixture.rollout.exists()
    assert thread_count(fixture.state, "thread-1") == 0


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX substitution simulation; native Win32 coverage follows"
)
def test_windows_update_uses_handle_relative_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    update = plan.file_updates[0]
    native_replace_calls = 0

    def native_open(path: Path) -> int:
        return os.open(path, os.O_RDWR)

    def native_identity(_fd: int) -> tuple[int, int]:
        return 101, 202

    def native_parent_identity(_path: Path) -> tuple[int, int]:
        return 303, 404

    def native_replace(
        fd: int,
        parent: Path,
        name: str,
        parent_identity: tuple[int, int],
        target_identity: tuple[int, int],
        content: bytes,
        recorder: object,
    ) -> tuple[int, int]:
        nonlocal native_replace_calls
        native_replace_calls += 1
        assert os.fstat(fd)
        assert parent == fixture.global_state.parent
        assert name == fixture.global_state.name
        assert parent_identity == (303, 404)
        assert target_identity == (101, 202)
        temporary = parent / ".native-update.tmp"
        temporary.write_bytes(content)
        intended = (temporary.stat().st_dev, temporary.stat().st_ino)
        recorder(intended, False)
        os.replace(temporary, fixture.global_state)
        recorder(intended, True)
        return intended

    monkeypatch.setattr(storage, "_supports_secure_dir_fd", lambda: False)
    monkeypatch.setattr(storage, "_uses_windows_handle_delete", lambda: True)
    monkeypatch.setattr(storage, "_open_windows_update_fd", native_open, raising=False)
    monkeypatch.setattr(storage, "_get_windows_handle_identity", native_identity, raising=False)
    monkeypatch.setattr(
        storage, "_get_windows_file_identity", native_parent_identity, raising=False
    )
    monkeypatch.setattr(storage, "_atomic_replace_windows_file", native_replace, raising=False)

    opened = storage._open_planned_file(plan.home, update.path, update.original)
    try:
        storage._atomic_write_opened(
            plan.home, opened, update.updated, mutation_recorder=lambda *_args: None
        )
    finally:
        opened.close()

    assert native_replace_calls == 1
    assert fixture.global_state.read_bytes() == update.updated


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX substitution simulation; native Win32 coverage follows"
)
def test_windows_atomic_update_refuses_substituted_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    update = plan.file_updates[0]
    competitor = b"competitor\n"

    monkeypatch.setattr(storage, "_supports_secure_dir_fd", lambda: False)
    monkeypatch.setattr(storage, "_uses_windows_handle_delete", lambda: True)
    monkeypatch.setattr(storage, "_open_windows_update_fd", lambda path: os.open(path, os.O_RDWR))
    monkeypatch.setattr(storage, "_get_windows_handle_identity", lambda _fd: (1, 2), raising=False)
    monkeypatch.setattr(storage, "_get_windows_file_identity", lambda _path: (3, 4), raising=False)

    def refuse_substitution(
        _fd: int,
        _parent: Path,
        _name: str,
        _parent_identity: tuple[int, int],
        _target_identity: tuple[int, int],
        _content: bytes,
        _recorder: object,
    ) -> tuple[int, int]:
        replacement = fixture.global_state.with_suffix(".replacement")
        replacement.write_bytes(competitor)
        replacement.replace(fixture.global_state)
        raise ConcurrentChangeError("target identity changed before atomic replace")

    monkeypatch.setattr(storage, "_atomic_replace_windows_file", refuse_substitution, raising=False)

    opened = storage._open_planned_file(plan.home, update.path, update.original)
    try:
        with pytest.raises(ConcurrentChangeError, match="target identity changed"):
            storage._atomic_write_opened(plan.home, opened, update.updated)
    finally:
        opened.close()

    assert fixture.global_state.read_bytes() == competitor


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 file handles")
def test_windows_native_update_branch(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")

    apply_deletion(plan, process_checker=lambda: [])

    updated = json.loads(fixture.global_state.read_bytes())
    assert "thread-1" not in updated["thread-workspace-root-hints"]


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 file handles")
def test_windows_native_restore_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = create_delete_fixture(tmp_path)
    original_rollout = fixture.rollout.read_bytes()
    plan = build_deletion_plan(fixture.home, "thread-1")

    def fail_verification(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated verification failure")

    monkeypatch.setattr(storage, "_verify_deletion", fail_verification)

    with pytest.raises(ApplyError, match="touched data restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.rollout.read_bytes() == original_rollout
    assert_delete_fixture_restored(fixture)


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

    with pytest.raises(ConcurrentChangeError, match="symlink|unsafe ancestry"):
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


def test_apply_deletion_rolls_back_when_post_replace_identity_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    original_global_state = fixture.global_state.read_bytes()
    plan = build_deletion_plan(fixture.home, "thread-1")
    checks = 0

    original_windows_replace = storage._atomic_replace_windows_file

    def fail_first_identity_check(*_args: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise ConcurrentChangeError("simulated post-replace identity failure")

    def fail_after_first_windows_replace(*args: object, **kwargs: object) -> tuple[int, int]:
        nonlocal checks
        identity = original_windows_replace(*args, **kwargs)
        checks += 1
        if checks == 1:
            raise ConcurrentChangeError("simulated post-replace identity failure")
        return identity

    if storage._uses_windows_handle_delete():
        monkeypatch.setattr(
            storage, "_atomic_replace_windows_file", fail_after_first_windows_replace
        )
    else:
        monkeypatch.setattr(
            storage, "_verify_mutation_identity", fail_first_identity_check, raising=False
        )

    with pytest.raises(ApplyError, match="touched data restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert checks >= 1
    assert fixture.global_state.read_bytes() == original_global_state
    assert_delete_fixture_restored(fixture)
    assert fixture.rollout.exists()


def test_apply_deletion_clears_prepared_journal_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    original_global_state = fixture.global_state.read_bytes()
    plan = build_deletion_plan(fixture.home, "thread-1")
    replace_calls = 0

    def fail_first_replace(*_args: object, **_kwargs: object) -> None:
        nonlocal replace_calls
        replace_calls += 1
        raise OSError("simulated replace failure")

    monkeypatch.setattr(storage, "_replace_file", fail_first_replace, raising=False)
    monkeypatch.setattr(storage, "_uses_windows_handle_delete", lambda: False)

    with pytest.raises(ApplyError, match="touched data restored"):
        apply_deletion(plan, process_checker=lambda: [])

    assert replace_calls == 1
    assert fixture.global_state.read_bytes() == original_global_state
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


def test_apply_deletion_does_not_overwrite_reused_rollout_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    competitor = b"created concurrently\n"
    original_restore = getattr(storage, "_restore_deleted_file_exclusive", None)

    def create_competitor_before_restore(home: Path, deletion: object) -> None:
        deletion.path.write_bytes(competitor)
        assert original_restore is not None
        original_restore(home, deletion)

    def fail_verification(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated verification failure")

    monkeypatch.setattr(storage, "_verify_deletion", fail_verification)
    monkeypatch.setattr(
        storage,
        "_restore_deleted_file_exclusive",
        create_competitor_before_restore,
        raising=False,
    )

    with pytest.raises(ApplyError, match="rollback was incomplete"):
        apply_deletion(plan, process_checker=lambda: [])

    assert fixture.rollout.read_bytes() == competitor
    assert_delete_fixture_restored(fixture)


def test_windows_restore_creates_relative_to_validated_parent_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    deletion = plan.file_deletions[0]
    deletion.path.unlink()
    original_parent = deletion.path.parent
    expected_parent = (0xAABBCCDD, 0x1122334455667788)
    external_sessions = tmp_path / "external-sessions"
    external_parent = external_sessions / "2026" / "07"
    external_parent.mkdir(parents=True)
    native_create_calls = 0

    def native_create(parent: Path, name: str, identity: tuple[int, int]) -> int:
        nonlocal native_create_calls
        native_create_calls += 1
        assert parent == original_parent
        assert identity == expected_parent
        fixture.home.joinpath("sessions").rename(fixture.home / "original-sessions")
        fixture.home.joinpath("sessions").symlink_to(external_sessions, target_is_directory=True)
        stable_parent = fixture.home / "original-sessions" / "2026" / "07"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        return os.open(stable_parent / name, flags, 0o600)

    monkeypatch.setattr(storage, "_supports_secure_dir_fd", lambda: False)
    monkeypatch.setattr(storage, "_uses_windows_handle_delete", lambda: True)
    monkeypatch.setattr(
        storage, "_get_windows_file_identity", lambda _path: expected_parent, raising=False
    )
    monkeypatch.setattr(storage, "_create_windows_file_exclusive", native_create, raising=False)

    storage._restore_deleted_file_exclusive(plan.home, deletion)

    assert native_create_calls == 1
    assert not external_parent.joinpath(deletion.path.name).exists()
    restored = fixture.home / "original-sessions" / "2026" / "07" / deletion.path.name
    assert restored.read_bytes() == deletion.original


def test_windows_handle_adoption_deletes_child_when_crt_conversion_fails() -> None:
    calls: list[str] = []

    def fail_open(_handle: int, _flags: int) -> int:
        calls.append("open")
        raise OSError("simulated CRT conversion failure")

    def mark_delete(_handle: int) -> None:
        calls.append("delete")

    def close(_handle: int) -> None:
        calls.append("close")

    with pytest.raises(OSError, match="CRT conversion failure"):
        windows_file.adopt_created_handle(123, 0, fail_open, mark_delete, close)

    assert calls == ["open", "delete", "close"]


def test_windows_atomic_cleanup_closes_handles_when_delete_pending_fails() -> None:
    calls: list[str] = []
    original_error = OSError("simulated temporary write failure")

    def get_handle(fd: int) -> int:
        calls.append(f"get:{fd}")
        return 123

    def fail_delete(handle: int) -> None:
        calls.append(f"delete:{handle}")
        raise OSError("simulated delete-pending failure")

    def close_fd(fd: int) -> None:
        calls.append(f"close-fd:{fd}")

    def close_handle(handle: int) -> None:
        calls.append(f"close-handle:{handle}")

    with pytest.raises(OSError, match="delete-pending failure") as raised:
        windows_file.cleanup_atomic_handles(
            10,
            20,
            False,
            original_error,
            get_handle,
            fail_delete,
            close_fd,
            close_handle,
        )

    assert raised.value.__cause__ is original_error
    assert calls == ["get:10", "delete:123", "close-fd:10", "close-handle:20"]


def test_windows_restore_write_failure_removes_owned_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    deletion = plan.file_deletions[0]
    deletion.path.unlink()
    delete_calls = 0
    delete_pending: set[int] = set()
    original_close = os.close

    def native_create(parent: Path, name: str, _identity: tuple[int, int]) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        return os.open(parent / name, flags, 0o600)

    def fail_write(fd: int, _content: bytes) -> None:
        os.write(fd, b"partial")
        raise OSError("simulated write failure")

    def native_delete(fd: int) -> None:
        nonlocal delete_calls
        delete_calls += 1
        delete_pending.add(fd)

    def close_delete_pending(fd: int) -> None:
        original_close(fd)
        if fd in delete_pending:
            deletion.path.unlink()
            delete_pending.remove(fd)

    monkeypatch.setattr(storage, "_supports_secure_dir_fd", lambda: False)
    monkeypatch.setattr(storage, "_uses_windows_handle_delete", lambda: True)
    monkeypatch.setattr(storage, "_get_windows_file_identity", lambda _path: (1, 2), raising=False)
    monkeypatch.setattr(storage, "_create_windows_file_exclusive", native_create, raising=False)
    monkeypatch.setattr(storage, "_write_all", fail_write, raising=False)
    monkeypatch.setattr(storage, "_delete_windows_file", native_delete)
    monkeypatch.setattr(storage.os, "close", close_delete_pending)

    with pytest.raises(OSError, match="simulated write failure"):
        storage._restore_deleted_file_exclusive(plan.home, deletion)

    assert delete_calls == 1
    assert not deletion.path.exists()


def test_windows_restore_skips_fchmod_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")
    deletion = plan.file_deletions[0]
    deletion.path.unlink()
    created_fd = -1
    fsync_calls: list[int] = []
    original_fsync = os.fsync

    def native_create(parent: Path, name: str, _identity: tuple[int, int]) -> int:
        nonlocal created_fd
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        created_fd = os.open(parent / name, flags, 0o600)
        return created_fd

    def record_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        original_fsync(fd)

    def fail_fchmod(_fd: int, _mode: int) -> None:
        raise AssertionError("native Windows restore must not call fchmod")

    monkeypatch.setattr(storage, "_supports_secure_dir_fd", lambda: False)
    monkeypatch.setattr(storage, "_uses_windows_handle_delete", lambda: True)
    monkeypatch.setattr(storage, "_get_windows_file_identity", lambda _path: (1, 2), raising=False)
    monkeypatch.setattr(storage, "_create_windows_file_exclusive", native_create, raising=False)
    monkeypatch.setattr(storage, "_delete_windows_file", lambda _fd: deletion.path.unlink())
    monkeypatch.setattr(storage.os, "fsync", record_fsync)
    monkeypatch.setattr(storage.os, "fchmod", fail_fchmod, raising=False)

    storage._restore_deleted_file_exclusive(plan.home, deletion)

    assert deletion.path.read_bytes() == deletion.original
    assert fsync_calls == [created_fd]
    with pytest.raises(OSError):
        os.fstat(created_fd)


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


def test_deletion_plan_requires_discoverable_session_metadata(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    state = home / "sqlite" / "codex.db"
    state.parent.mkdir(parents=True)
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO threads VALUES ('thread-1')")

    plan = build_deletion_plan(home, "thread-1")

    assert plan.session is None
    assert any("metadata" in error for error in plan.errors)
    assert not plan.file_deletions
    assert not plan.file_updates


def test_delete_cleans_index_sidebar_and_last_workspace(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    project = tmp_path / "missing"
    index = fixture.home / "session_index.jsonl"
    index.write_text(
        '{"id":"thread-1","thread_name":"Delete me"}\n'
        '{"id":"thread-2","thread_name":"Keep me"}\n'
    )
    state = json.loads(fixture.global_state.read_text())
    state.update(
        {
            "pinned-thread-ids": ["thread-1", "thread-2"],
            "sidebar-project-thread-orders": {
                str(project): {"threadIds": ["thread-1"]}
            },
            "electron-saved-workspace-roots": [str(project)],
            "active-workspace-roots": [str(project)],
            "project-order": [str(project)],
            "electron-workspace-root-labels": {str(project): "Project"},
            "electron-persisted-atom-state": {
                "sidebar-collapsed-groups": {str(project): True}
            },
        }
    )
    fixture.global_state.write_text(json.dumps(state))

    plan = build_deletion_plan(fixture.home, "thread-1")
    apply_deletion(plan, process_checker=lambda: [])

    assert "thread-1" not in index.read_text()
    assert "thread-2" in index.read_text()
    updated = json.loads(fixture.global_state.read_text())
    assert updated["pinned-thread-ids"] == ["thread-2"]
    assert str(project) not in updated["sidebar-project-thread-orders"]
    assert str(project) not in updated["electron-saved-workspace-roots"]
    assert str(project) not in updated["electron-workspace-root-labels"]


def test_delete_archived_copy_redirects_thread_to_active_copy(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    project = tmp_path / "project"
    active = home / "sessions" / "rollout-thread-1.jsonl"
    archived = home / "archived_sessions" / "rollout-thread-1.jsonl"
    active.parent.mkdir(parents=True)
    archived.parent.mkdir(parents=True)
    line = json.dumps(
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(project)}}
    )
    active.write_text(line + "\n")
    archived.write_text(line + "\n")
    state = home / "state_5.sqlite"
    with sqlite3.connect(state) as db:
        db.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, "
            "archived INTEGER, archived_at INTEGER, updated_at_ms INTEGER, rollout_path TEXT)"
        )
        db.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("thread-1", "Title", str(project), 1, 123, 100, str(archived)),
        )
    index = home / "session_index.jsonl"
    index.write_text('{"id":"thread-1","thread_name":"Title"}\n')

    plan = build_deletion_plan(home, "thread-1", scope="archived")
    assert not plan.database_actions
    assert {change.column for change in plan.database_changes} == {
        "rollout_path",
        "archived",
        "archived_at",
    }
    apply_deletion(plan, process_checker=lambda: [])

    assert active.exists()
    assert not archived.exists()
    assert "thread-1" in index.read_text()
    with sqlite3.connect(state) as db:
        row = db.execute(
            "SELECT rollout_path, archived, archived_at FROM threads WHERE id='thread-1'"
        ).fetchone()
    assert row == (str(active), 0, None)


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
