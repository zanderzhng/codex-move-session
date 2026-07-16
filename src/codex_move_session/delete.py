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


_TABLE_ACTIONS: tuple[
    tuple[str, DatabaseActionKind, str, tuple[str, ...]], ...
] = (
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
            (
                tuple(row)
                for row in db.execute(
                    f'SELECT * FROM "{table}" WHERE {where_clause}', params
                )
            ),
            key=repr,
        )
    )
    if not rows:
        return None
    return DatabaseAction(path, table, action, where_clause, params, columns, rows)


def _thread_rollout_references(
    db: sqlite3.Connection,
) -> tuple[tuple[str, str], ...]:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
    ).fetchone()
    if exists is None:
        return ()
    columns = {row[1] for row in db.execute('PRAGMA table_info("threads")')}
    if not {"id", "rollout_path"}.issubset(columns):
        return ()
    return tuple(
        (str(row[0]), str(row[1]))
        for row in db.execute('SELECT id, rollout_path FROM "threads"')
        if row[1]
    )


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


def _plan_rollout_deletions(
    home: Path,
    session_id: str,
    selected_paths: set[str],
    references: tuple[tuple[str, str], ...],
    warnings: list[str],
    errors: list[str],
) -> list[FileDeletion]:
    shared_paths = {
        Path(value).resolve()
        for thread_id, value in references
        if thread_id != session_id and Path(value).is_absolute()
    }
    deletions: list[FileDeletion] = []
    seen: set[Path] = set()
    for value in sorted(selected_paths):
        path = Path(value)
        if not path.is_absolute():
            errors.append(f"rollout path is not absolute: {path}")
            continue
        if path.is_symlink():
            errors.append(f"rollout path is a symlink: {path}")
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not _allowed_rollout(path, home):
            errors.append(f"rollout path is outside the Codex session directories: {path}")
            continue
        if resolved in shared_paths:
            errors.append(f"rollout file is referenced by another session: {path}")
            continue
        if not path.exists():
            warnings.append(f"rollout file not found: {path}")
            continue
        if not path.is_file():
            errors.append(f"rollout path is not a regular file: {path}")
            continue
        try:
            original = path.read_bytes()
        except OSError as error:
            errors.append(f"could not read rollout file {path}: {error}")
            continue
        deletions.append(
            FileDeletion(
                path=path,
                original=original,
                original_digest=hashlib.sha256(original).hexdigest(),
            )
        )
    return deletions


def _plan_global_state(
    home: Path, session_id: str, errors: list[str]
) -> FileChange | None:
    path = home / ".codex-global-state.json"
    if not path.exists():
        return None
    if not path.is_file():
        errors.append(f"{path}: expected a regular file")
        return None
    try:
        original = path.read_bytes()
        parsed: Any = json.loads(original)
    except OSError as error:
        errors.append(f"could not read global state {path}: {error}")
        return None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: invalid JSON: {error}")
        return None
    if not isinstance(parsed, dict):
        errors.append(f"{path}: expected a JSON object")
        return None
    hints = parsed.get("thread-workspace-root-hints")
    if hints is None:
        return None
    if not isinstance(hints, dict):
        errors.append(f"{path}: expected thread-workspace-root-hints to be a JSON object")
        return None
    if session_id not in hints:
        return None
    updated = dict(parsed)
    updated_hints = dict(hints)
    del updated_hints[session_id]
    updated["thread-workspace-root-hints"] = updated_hints
    output = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    return FileChange(
        path=path,
        area="global-state-delete",
        original=original,
        updated=output,
        original_digest=hashlib.sha256(original).hexdigest(),
        replacements=1,
    )


def build_deletion_plan(home: Path, session_id: str) -> DeletionPlan:
    home = home.expanduser().resolve()
    session = next(
        (item for item in discover_sessions(home) if item.id == session_id),
        None,
    )
    database_actions: list[DatabaseAction] = []
    warnings: list[str] = []
    errors: list[str] = []
    references: list[tuple[str, str]] = []

    for path in _database_paths(home):
        try:
            with open_sqlite_snapshot(path) as db:
                for table, action, where_clause, required_columns in _TABLE_ACTIONS:
                    planned = _read_action(
                        db,
                        path,
                        table,
                        action,
                        where_clause,
                        required_columns,
                        session_id,
                    )
                    if planned is not None:
                        database_actions.append(planned)
                references.extend(_thread_rollout_references(db))
        except (OSError, RuntimeError, sqlite3.Error) as error:
            errors.append(f"could not inspect database {path}: {error}")

    thread_actions = [action for action in database_actions if action.table == "threads"]
    if not thread_actions:
        errors.append(f"session not found: {session_id}")
        return DeletionPlan(
            home=home,
            session=None,
            database_actions=tuple(database_actions),
            file_deletions=(),
            file_updates=(),
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    rollout_paths: set[str] = set()
    for action in thread_actions:
        if "rollout_path" not in action.columns:
            continue
        index = action.columns.index("rollout_path")
        rollout_paths.update(str(row[index]) for row in action.original_rows if row[index])
    file_deletions = _plan_rollout_deletions(
        home,
        session_id,
        rollout_paths,
        tuple(references),
        warnings,
        errors,
    )
    global_state = _plan_global_state(home, session_id, errors)
    file_updates = () if global_state is None else (global_state,)
    return DeletionPlan(
        home=home,
        session=session,
        database_actions=tuple(database_actions),
        file_deletions=tuple(file_deletions),
        file_updates=file_updates,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )
