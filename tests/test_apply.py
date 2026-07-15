import json
import sqlite3
from pathlib import Path

import pytest
from test_planner import create_codex_fixture

import codex_move_session.storage as storage
from codex_move_session.planner import build_plan
from codex_move_session.storage import (
    ApplyError,
    ConcurrentChangeError,
    ProcessRunningError,
    apply_plan,
)


def read_thread_cwd(state: Path) -> str:
    with sqlite3.connect(state) as db:
        return db.execute("SELECT cwd FROM threads WHERE id='thread-1'").fetchone()[0]


def test_apply_updates_all_stores_creates_backup_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, rollout = create_codex_fixture(home, old)
    plan = build_plan(home, str(old), str(new))

    result = apply_plan(plan, process_checker=lambda: [])

    assert read_thread_cwd(state) == str(new)
    assert str(new) in rollout.read_text()
    with sqlite3.connect(home / "memories_1.sqlite") as db:
        memory = db.execute(
            "SELECT raw_memory FROM stage1_outputs WHERE thread_id='thread-1'"
        ).fetchone()[0]
    assert str(new) in memory
    assert (result.backup_dir / "manifest.json").is_file()
    manifest = json.loads((result.backup_dir / "manifest.json").read_text())
    assert len(manifest["databases"]) == 2
    assert len(manifest["files"]) == 2
    second_plan = build_plan(home, str(old), str(new))
    assert not second_plan.has_changes


def test_apply_refuses_file_changed_after_preview(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, rollout = create_codex_fixture(home, old)
    plan = build_plan(home, str(old), str(new))
    rollout.write_text(rollout.read_text() + "{}\n")

    with pytest.raises(ConcurrentChangeError):
        apply_plan(plan, process_checker=lambda: [])

    assert read_thread_cwd(state) == str(old)


def test_apply_refuses_when_codex_is_running(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    create_codex_fixture(home, old)
    plan = build_plan(home, str(old), str(new))

    with pytest.raises(ProcessRunningError, match="Codex"):
        apply_plan(plan, process_checker=lambda: ["Codex"])


def test_apply_restores_database_when_file_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, rollout = create_codex_fixture(home, old)
    original_rollout = rollout.read_bytes()
    plan = build_plan(home, str(old), str(new))

    def fail_write(path: Path, content: bytes) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(storage, "_atomic_write", fail_write)
    with pytest.raises(ApplyError, match="restored") as raised:
        apply_plan(plan, process_checker=lambda: [])

    assert read_thread_cwd(state) == str(old)
    assert rollout.read_bytes() == original_rollout
    assert raised.value.backup_dir.is_dir()
