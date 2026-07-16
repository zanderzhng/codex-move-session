# Per-Session Move And Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dry-run-first per-session move and safe local session deletion, with backups,
verification, rollback, interactive selection, and `--delete SESSION_ID` automation.

**Architecture:** Keep migration and deletion as separate immutable plans. Add an optional session
filter to the existing migration planner, implement deletion discovery in a focused `delete.py`, and
execute deletion through the existing storage safety layer. The CLI selects one stale session and
dispatches either plan without changing existing path-wide non-interactive migration.

**Tech Stack:** Python 3.10+, stdlib `argparse`/`sqlite3`/`json`/`pathlib`, Rich, Questionary,
pytest, Ruff, uv, GitHub Actions on Windows/macOS/Linux.

## Global Constraints

- Dry-run is the default; only `--apply` or an explicit interactive confirmation writes data.
- Delete only Codex session data. Never delete or move the project source directory.
- Interactive move and delete target exactly one selected session.
- Existing `--old OLD --new NEW` continues to migrate all matching sessions.
- `--delete SESSION_ID` is mutually exclusive with `--old` and `--new`.
- Refuse writes while Codex processes are running.
- Back up every touched database and file before deletion; automatically roll back any partial
  failure and retain the backup after success.
- Do not add manual undo, multi-select, or whole-directory deletion.
- Rollout deletion is limited to regular files under `CODEX_HOME/sessions` or
  `CODEX_HOME/archived_sessions`, never a file shared by another session.
- Match existing project style and add no new runtime dependency.

---

### Task 1: Filter Migration To One Interactive Session

**Files:**
- Modify: `src/codex_move_session/planner.py`
- Modify: `tests/test_planner.py`

**Interfaces:**
- Consumes: existing `build_plan(home, old, new, include_archived=False, scope=None)` callers.
- Produces: `build_plan(..., session_id: str | None = None) -> MigrationPlan`; `None` preserves
  current path-wide behavior, while an ID limits session-specific stores and skips shared roots.

- [ ] **Step 1: Write failing tests for a filtered move**

Add a fixture helper that inserts a sibling thread, rollout, and memory row, then add:

```python
def test_plan_can_limit_move_to_one_session(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, _ = create_codex_fixture(home, old)
    sibling_rollout = insert_sibling_session(home, state, old, thread_id="thread-2")

    plan = build_plan(home, str(old), str(new), scope="all", session_id="thread-1")

    assert [session.id for session in plan.sessions] == ["thread-1"]
    assert {change.key for change in plan.database_changes} == {"thread-1"}
    assert all(change.path != sibling_rollout for change in plan.file_changes)
    global_change = next(
        change for change in plan.file_changes if change.area == "global-state"
    )
    updated = json.loads(global_change.updated)
    assert updated["thread-workspace-root-hints"]["thread-1"] == str(new)
    assert updated["thread-workspace-root-hints"]["thread-2"] == str(old)
    assert updated["project-order"] == [str(old)]


def test_unfiltered_plan_still_moves_all_matching_sessions(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, _ = create_codex_fixture(home, old)
    insert_sibling_session(home, state, old, thread_id="thread-2")

    plan = build_plan(home, str(old), str(new), scope="all")

    assert {session.id for session in plan.sessions} == {"thread-1", "thread-2"}
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest \
  tests/test_planner.py::test_plan_can_limit_move_to_one_session \
  tests/test_planner.py::test_unfiltered_plan_still_moves_all_matching_sessions -q
```

Expected: the first test fails because `build_plan` does not accept `session_id`.

- [ ] **Step 3: Implement the session filter and selected global hint update**

Change the signature and session loop:

```python
def build_plan(
    home: Path,
    old: str,
    new: str,
    *,
    include_archived: bool = False,
    scope: SessionScope | None = None,
    session_id: str | None = None,
) -> MigrationPlan:
    # existing setup
    for session in discover_sessions(home):
        if session_id is not None and session.id != session_id:
            continue
        # existing scope and path checks
```

Add a focused helper for filtered global state:

```python
def _plan_selected_thread_hint(
    path: Path, session_id: str, mapper: PathMapper, errors: list[str]
) -> FileChange | None:
    original = path.read_bytes()
    try:
        parsed = json.loads(original)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: invalid JSON: {error}")
        return None
    if not isinstance(parsed, dict):
        errors.append(f"{path}: expected a JSON object")
        return None
    hints = parsed.get("thread-workspace-root-hints")
    if not isinstance(hints, dict) or session_id not in hints:
        return None
    updated_hint, count = _replace_json(hints[session_id], mapper)
    if count == 0:
        return None
    updated = dict(parsed)
    updated_hints = dict(hints)
    updated_hints[session_id] = updated_hint
    updated["thread-workspace-root-hints"] = updated_hints
    output = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    return FileChange(
        path=path,
        area="global-state",
        original=original,
        updated=output,
        original_digest=hashlib.sha256(original).hexdigest(),
        replacements=count,
    )
```

When `session_id` is set, call this helper and skip project-wide global keys and `cap_sid`. Preserve
the existing code path when it is `None`.

- [ ] **Step 4: Run planner and full tests and verify GREEN**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest tests/test_planner.py -q
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```console
rtk git add src/codex_move_session/planner.py tests/test_planner.py
rtk git commit -m "feat: filter migration by session"
```

---

### Task 2: Build A Read-Only Deletion Plan

**Files:**
- Create: `src/codex_move_session/delete.py`
- Create: `tests/test_delete.py`

**Interfaces:**
- Consumes: `discover_sessions`, `candidate_session_databases`, and `open_sqlite_snapshot`.
- Produces:
  - `DatabaseAction(path, table, action, where_clause, params, columns, original_rows)`
  - `FileDeletion(path, original, original_digest)`
  - `DeletionPlan(home, session, database_actions, file_deletions, file_updates, warnings, errors)`
  - `build_deletion_plan(home: Path, session_id: str) -> DeletionPlan`

- [ ] **Step 1: Write failing deletion-planner tests**

Create temporary databases with `threads`, all optional related tables, `memories_1.sqlite`, global
state, and rollout files. Add concrete tests:

```python
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
    assert len(plan.file_updates) == 1
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
```

- [ ] **Step 2: Run the new module tests and verify RED**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest tests/test_delete.py -q
```

Expected: collection fails with `ModuleNotFoundError: codex_move_session.delete`.

- [ ] **Step 3: Define immutable deletion plan types**

Implement these exact public types in `delete.py`:

```python
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .discovery import Session, candidate_session_databases, discover_sessions
from .planner import FileChange
from .sqlite_read import open_sqlite_snapshot

DatabaseActionKind = Literal["delete", "clear"]
SQLiteValue = str | int | float | bytes | None


@dataclass(frozen=True)
class DatabaseAction:
    path: Path
    table: str
    action: DatabaseActionKind
    where_clause: str
    params: tuple[SQLiteValue, ...]
    columns: tuple[str, ...]
    original_rows: tuple[tuple[SQLiteValue, ...], ...]

    @property
    def row_count(self) -> int:
        return len(self.original_rows)


@dataclass(frozen=True)
class FileDeletion:
    path: Path
    original: bytes
    original_digest: str


@dataclass(frozen=True)
class DeletionPlan:
    home: Path
    session: Session | None
    database_actions: tuple[DatabaseAction, ...]
    file_deletions: tuple[FileDeletion, ...]
    file_updates: tuple[FileChange, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.database_actions or self.file_deletions or self.file_updates)

    @property
    def deleted_rows(self) -> int:
        return sum(
            action.row_count for action in self.database_actions if action.action == "delete"
        )

    @property
    def cleared_assignments(self) -> int:
        return sum(
            action.row_count for action in self.database_actions if action.action == "clear"
        )
```

- [ ] **Step 4: Implement database action discovery**

Use a fixed internal table specification, introspect columns before querying, and snapshot exact
rows for concurrency checks:

```python
_TABLE_ACTIONS = (
    ("thread_dynamic_tools", "delete", "thread_id = ?", ("thread_id",)),
    ("thread_goals", "delete", "thread_id = ?", ("thread_id",)),
    (
        "thread_spawn_edges",
        "delete",
        "parent_thread_id = ? OR child_thread_id = ?",
        ("parent_thread_id", "child_thread_id"),
    ),
    ("stage1_outputs", "delete", "thread_id = ?", ("thread_id",)),
    ("agent_job_items", "clear", "assigned_thread_id = ?", ("assigned_thread_id",)),
    ("automation_runs", "delete", "thread_id = ?", ("thread_id",)),
    ("inbox_items", "delete", "thread_id = ?", ("thread_id",)),
    ("threads", "delete", "id = ?", ("id",)),
)


def _database_paths(home: Path) -> list[Path]:
    paths = set(candidate_session_databases(home))
    paths.update(path for path in home.glob("memories_*.sqlite") if path.is_file())
    return sorted(paths)


def _read_action(
    db: sqlite3.Connection,
    path: Path,
    table: str,
    action: DatabaseActionKind,
    where_clause: str,
    required_columns: tuple[str, ...],
    session_id: str,
) -> DatabaseAction | None:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return None
    columns = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
    if not set(required_columns).issubset(columns):
        return None
    params = (session_id, session_id) if " OR " in where_clause else (session_id,)
    rows = tuple(
        sorted(
            db.execute(f'SELECT * FROM "{table}" WHERE {where_clause}', params),
            key=repr,
        )
    )
    if not rows:
        return None
    return DatabaseAction(path, table, action, where_clause, params, columns, rows)
```

`build_deletion_plan` must collect actions from every database, catch read errors into `errors`, and
require at least one `threads` action.

- [ ] **Step 5: Implement rollout and global-state planning**

Require an absolute path, resolve allowed roots, and reject shared/outside/symlinked targets before
reading bytes:

```python
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _allowed_rollout(path: Path, home: Path) -> bool:
    if not path.is_absolute() or path.is_symlink():
        return False
    resolved = path.resolve()
    return any(
        _is_within(resolved, root.resolve())
        for root in (home / "sessions", home / "archived_sessions")
    )
```

Read rollout paths from the selected `threads` rows. Search every `threads` table for a different ID
with the same resolved path. Missing files become warnings; unsafe, shared, unreadable, or non-file
paths become errors. Eligible files become `FileDeletion` with SHA-256.

For `.codex-global-state.json`, parse an object and remove only
`thread-workspace-root-hints[session_id]`. When changed, create a `FileChange` preserving original
bytes and digest, with area `global-state-delete` and one replacement.

- [ ] **Step 6: Run focused and full tests and verify GREEN**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest tests/test_delete.py -q
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest -q
```

Expected: all tests pass and planning performs no writes.

- [ ] **Step 7: Commit**

```console
rtk git add src/codex_move_session/delete.py tests/test_delete.py
rtk git commit -m "feat: plan safe session deletion"
```

---

### Task 3: Apply Deletion With Backup, Verification, And Rollback

**Files:**
- Modify: `src/codex_move_session/storage.py`
- Modify: `tests/test_delete.py`

**Interfaces:**
- Consumes: `DeletionPlan`, `DatabaseAction`, `FileDeletion` from Task 2 and existing process,
  backup, restore, and atomic-write helpers.
- Produces: `DeletionResult` and
  `apply_deletion(plan, process_checker=running_codex_processes) -> DeletionResult`.

- [ ] **Step 1: Write failing apply and rollback tests**

Add:

```python
def test_apply_deletion_removes_rows_and_file_and_keeps_backup(tmp_path: Path) -> None:
    fixture = create_delete_fixture(tmp_path)
    plan = build_deletion_plan(fixture.home, "thread-1")

    result = apply_deletion(plan, process_checker=lambda: [])

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
```

- [ ] **Step 2: Run apply tests and verify RED**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest \
  tests/test_delete.py -k "apply_deletion" -q
```

Expected: import fails because `apply_deletion` and `DeletionResult` do not exist.

- [ ] **Step 3: Add deletion result and exact concurrency validation**

Add to `storage.py`:

```python
from .delete import DatabaseAction, DeletionPlan


@dataclass(frozen=True)
class DeletionResult:
    backup_dir: Path
    deleted_rows: int
    cleared_assignments: int
    deleted_files: int


def _read_action_rows(db: sqlite3.Connection, action: DatabaseAction) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        sorted(
            db.execute(
                f'SELECT * FROM "{action.table}" WHERE {action.where_clause}', action.params
            ),
            key=repr,
        )
    )


def _validate_deletion_unchanged(plan: DeletionPlan) -> None:
    for path, actions in _group_deletion_actions(plan.database_actions).items():
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        with closing(connection) as db:
            for action in actions:
                if _read_action_rows(db, action) != action.original_rows:
                    raise ConcurrentChangeError(
                        f"database changed after delete preview: {path}:{action.table}"
                    )
    for item in plan.file_deletions:
        if not item.path.is_file() or hashlib.sha256(item.path.read_bytes()).hexdigest() != item.original_digest:
            raise ConcurrentChangeError(f"file changed after delete preview: {item.path}")
    for item in plan.file_updates:
        if hashlib.sha256(item.path.read_bytes()).hexdigest() != item.original_digest:
            raise ConcurrentChangeError(f"file changed after delete preview: {item.path}")
```

- [ ] **Step 4: Add deletion-specific backup creation**

Reuse `_backup_database` and the existing manifest layout:

```python
def _create_deletion_backup(plan: DeletionPlan) -> tuple[Path, dict[str, Any]]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = plan.home / "backups" / f"codex-move-session-{timestamp}"
    database_dir = backup_dir / "databases"
    file_dir = backup_dir / "files"
    database_dir.mkdir(parents=True)
    file_dir.mkdir()
    database_paths = sorted({action.path for action in plan.database_actions})
    file_paths = sorted(
        {item.path for item in plan.file_deletions}
        | {item.path for item in plan.file_updates}
    )
    manifest: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "action": "delete",
        "session_id": plan.session.id if plan.session else "",
        "databases": [],
        "files": [],
    }
    for index, path in enumerate(database_paths):
        backup_path = database_dir / f"{index:03d}-{path.name}"
        _backup_database(path, backup_path)
        manifest["databases"].append(
            {"original": str(path), "backup": str(backup_path.relative_to(backup_dir))}
        )
    for index, path in enumerate(file_paths):
        backup_path = file_dir / f"{index:03d}-{path.name}.bin"
        backup_path.write_bytes(path.read_bytes())
        manifest["files"].append(
            {"original": str(path), "backup": str(backup_path.relative_to(backup_dir))}
        )
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return backup_dir, manifest
```

- [ ] **Step 5: Implement transactional actions, file removal, and verification**

Add grouped apply and verify helpers:

```python
def _apply_deletion_actions(path: Path, actions: list[DatabaseAction]) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for action in actions:
                if action.action == "clear":
                    cursor = db.execute(
                        f'UPDATE "{action.table}" SET assigned_thread_id = NULL '
                        f"WHERE {action.where_clause}",
                        action.params,
                    )
                else:
                    cursor = db.execute(
                        f'DELETE FROM "{action.table}" WHERE {action.where_clause}',
                        action.params,
                    )
                if cursor.rowcount != action.row_count:
                    raise ConcurrentChangeError(
                        f"database changed during delete: {path}:{action.table}"
                    )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _remove_file(path: Path) -> None:
    path.unlink()


def _verify_deletion(plan: DeletionPlan) -> None:
    for path, actions in _group_deletion_actions(plan.database_actions).items():
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        with closing(connection) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError(f"SQLite quick_check failed: {path}")
            for action in actions:
                if _read_action_rows(db, action):
                    raise RuntimeError(f"database delete verification failed: {path}:{action.table}")
    for item in plan.file_deletions:
        if item.path.exists():
            raise RuntimeError(f"file delete verification failed: {item.path}")
    for item in plan.file_updates:
        if item.path.read_bytes() != item.updated:
            raise RuntimeError(f"file update verification failed: {item.path}")
```

- [ ] **Step 6: Implement `apply_deletion` with automatic rollback**

```python
def apply_deletion(
    plan: DeletionPlan,
    *,
    process_checker: Callable[[], list[str]] = running_codex_processes,
) -> DeletionResult:
    if plan.errors or plan.session is None:
        raise PlanValidationError("deletion plan contains errors: " + "; ".join(plan.errors))
    running = process_checker()
    if running:
        raise ProcessRunningError("Close Codex before applying changes: " + ", ".join(running))
    if not plan.has_changes:
        raise PlanValidationError("deletion plan contains no changes")
    _validate_deletion_unchanged(plan)
    backup_dir, manifest = _create_deletion_backup(plan)
    try:
        for path, actions in _group_deletion_actions(plan.database_actions).items():
            _apply_deletion_actions(path, actions)
        for item in plan.file_updates:
            _atomic_write(item.path, item.updated)
        for item in plan.file_deletions:
            _remove_file(item.path)
        _verify_deletion(plan)
    except BaseException as error:
        restore_errors = _restore_backup(backup_dir, manifest)
        if restore_errors:
            raise ApplyError(
                f"delete failed and rollback was incomplete: {error}; {'; '.join(restore_errors)}",
                backup_dir,
            ) from error
        raise ApplyError(f"delete failed; touched data restored: {error}", backup_dir) from error
    return DeletionResult(
        backup_dir=backup_dir,
        deleted_rows=plan.deleted_rows,
        cleared_assignments=plan.cleared_assignments,
        deleted_files=len(plan.file_deletions),
    )
```

- [ ] **Step 7: Run deletion, storage, and full tests and verify GREEN**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest tests/test_delete.py tests/test_apply.py -q
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest -q
```

Expected: all tests pass, including rollback and process refusal.

- [ ] **Step 8: Commit**

```console
rtk git add src/codex_move_session/storage.py tests/test_delete.py
rtk git commit -m "feat: apply safe session deletion"
```

---

### Task 4: Add Interactive Action Selection And `--delete`

**Files:**
- Modify: `src/codex_move_session/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: filtered `build_plan`, `build_deletion_plan`, and `apply_deletion`.
- Produces prompt methods `choose_session`, `choose_action`, `confirm_delete`; CLI argument
  `--delete SESSION_ID`; `render_deletion_plan(plan, console, applying)`.

- [ ] **Step 1: Write failing CLI tests**

Extend `FakePrompts` with selected session and action, then add:

```python
def test_noninteractive_delete_is_dry_run(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    state, rollout = create_codex_fixture(home, old)
    console, recorder = recording_console()

    exit_code = run(
        ["--delete", "thread-1", "--codex-home", str(home)], console=console
    )

    assert exit_code == 0
    assert "Delete dry run" in recorder.export_text()
    assert read_thread_cwd(state) == str(old)
    assert rollout.exists()


def test_noninteractive_delete_apply_removes_session(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    state, rollout = create_codex_fixture(home, old)

    exit_code = run(
        ["--delete", "thread-1", "--codex-home", str(home), "--apply"],
        console=recording_console()[0],
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert thread_count(state, "thread-1") == 0
    assert not rollout.exists()


def test_parser_rejects_delete_with_move_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        run(["--delete", "thread-1", "--old", "/old", "--new", "/new"])


def test_interactive_can_delete_one_selected_session(tmp_path: Path) -> None:
    fixture = create_two_session_cli_fixture(tmp_path)
    prompts = FakePrompts(
        fixture.old,
        fixture.new,
        confirm=True,
        session_id="thread-1",
        action="delete",
    )

    exit_code = run(
        ["--codex-home", str(fixture.home)],
        console=recording_console()[0],
        prompts=prompts,
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert thread_count(fixture.state, "thread-1") == 0
    assert thread_count(fixture.state, "thread-2") == 1


def test_interactive_move_changes_only_selected_session(tmp_path: Path) -> None:
    fixture = create_two_session_cli_fixture(tmp_path)
    prompts = FakePrompts(
        fixture.old,
        fixture.new,
        confirm=True,
        session_id="thread-1",
        action="move",
    )

    exit_code = run(
        ["--codex-home", str(fixture.home)],
        console=recording_console()[0],
        prompts=prompts,
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert thread_cwd(fixture.state, "thread-1") == str(fixture.new)
    assert thread_cwd(fixture.state, "thread-2") == str(fixture.old)
```

- [ ] **Step 2: Run CLI tests and verify RED**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest tests/test_cli.py -q
```

Expected: `--delete` is unrecognized and prompt methods are missing.

- [ ] **Step 3: Add prompt methods and parser validation**

```python
def choose_session(self, group: StaleGroup) -> str | None:
    choices = [
        Choice(
            f"{session.title or '(untitled)'}  {session.id[:8]}  "
            f"({'archived' if session.archived else 'active'})",
            session.id,
        )
        for session in group.sessions
    ]
    return questionary.select("Session", choices=choices).ask()


def choose_action(self) -> str | None:
    return questionary.select(
        "Action",
        choices=[Choice("Move session", "move"), Choice("Delete session", "delete")],
    ).ask()


def confirm_delete(self, session: Session) -> bool:
    return bool(
        questionary.confirm(
            f"Delete session '{session.title or session.id}'? Project files will not be deleted.",
            default=False,
        ).ask()
    )
```

Add `--delete` and validate modes after parsing:

```python
parser.add_argument("--delete", metavar="SESSION_ID", help="Delete one local Codex session")

if args.delete and (args.old or args.new):
    parser.error("--delete cannot be combined with --old or --new")
if bool(args.old) != bool(args.new):
    parser.error("--old and --new must be supplied together")
```

- [ ] **Step 4: Render deletion plans and dispatch non-interactive deletion**

Import the new APIs and add a Rich table renderer that lists session metadata, each database action
with row count, each file deletion, each file update, totals, warnings, and errors. The concrete
heading and summary are:

```python
console.rule("Delete plan" if applying else "Delete dry run")
console.print(
    f"[bold]{plan.deleted_rows}[/bold] row(s) deleted, "
    f"[bold]{plan.cleared_assignments}[/bold] assignment(s) cleared, and "
    f"[bold]{len(plan.file_deletions)}[/bold] rollout file(s) deleted."
)
```

In `run`, handle `args.delete` before the interactive branch: build and render the deletion plan,
return on errors or dry-run, then call `apply_deletion` and print the backup path.

- [ ] **Step 5: Dispatch interactive per-session move or delete**

After `choose_old`, retrieve its exact `StaleGroup`, call `choose_session`, resolve the selected
`Session`, then call `choose_action`. For move, prompt for destination and call:

```python
plan = build_plan(
    args.codex_home,
    selected_group.path,
    new,
    scope=scope,
    session_id=selected_session.id,
)
```

For delete, call `build_deletion_plan`, render it, and use `confirm_delete`. Both cancellation paths
print `Cancelled.` and return zero.

- [ ] **Step 6: Run CLI and full tests and verify GREEN**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest tests/test_cli.py -q
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest -q
```

Expected: all tests pass and dry-run tests leave databases and rollout files unchanged.

- [ ] **Step 7: Commit**

```console
rtk git add src/codex_move_session/cli.py tests/test_cli.py
rtk git commit -m "feat(cli): choose move or delete"
```

---

### Task 5: Document The Feature And Bump The Minor Version

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `src/codex_move_session/__init__.py` only if it contains a literal version
- Modify: `uv.lock`

**Interfaces:**
- Consumes: completed CLI behavior from Task 4.
- Produces: user documentation and package version `0.2.0`.

- [ ] **Step 1: Update README usage and safety text**

Document the interactive session/action selector and add:

```console
codex-move-session --delete SESSION_ID
codex-move-session --delete SESSION_ID --apply
```

State that deletion removes local session rows, related memory, and the rollout file after backup;
it never deletes project files. Add deletion backup, concurrency, verification, and rollback to the
Safety section.

- [ ] **Step 2: Bump package version and refresh the lock**

Set:

```toml
version = "0.2.0"
```

Then run:

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv lock
```

Expected: `uv.lock` records `codex-move-session==0.2.0` without unrelated dependency upgrades.

- [ ] **Step 3: Verify documented commands and version**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run codex-move-session --help
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run codex-move-session --version
```

Expected: help includes `--delete SESSION_ID`; version prints `codex-move-session 0.2.0`.

- [ ] **Step 4: Commit**

```console
rtk git add README.md pyproject.toml src/codex_move_session/__init__.py uv.lock
rtk git commit -m "docs: document session deletion"
```

If `__init__.py` is not changed, omit it from `git add`.

---

### Task 6: Final Cross-Platform Verification

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: fresh evidence that tests, lint, build, CLI, package, and Git hygiene pass.

- [ ] **Step 1: Run the full local verification gate**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run pytest
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv run ruff check .
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-uv-cache uv build --clear
rtk proxy git diff --check
rtk git status --short --branch
```

Expected: all tests pass, Ruff reports no errors, `0.2.0` wheel and sdist build, diff check is empty,
and the worktree contains only intended changes or is clean after commits.

- [ ] **Step 2: Smoke-test the built wheel through uvx**

```console
rtk env UV_CACHE_DIR=/private/tmp/codex-move-session-0.2.0-wheel \
  uvx --from dist/codex_move_session-0.2.0-py3-none-any.whl \
  codex-move-session --version
```

Expected: `codex-move-session 0.2.0`.

- [ ] **Step 3: Push and monitor CI only after local verification**

```console
rtk git push origin main
rtk gh run list --branch main --limit 1
rtk gh run watch --exit-status
```

Expected: all Windows, macOS, and Ubuntu jobs pass on Python 3.10, 3.12, and 3.14. Do not create a
`v0.2.0` tag unless the user separately authorizes a release after CI passes.
