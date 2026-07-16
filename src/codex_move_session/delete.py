from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
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
    original_device: int
    original_inode: int
    original_mode: int


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
        return sum(action.row_count for action in self.database_actions if action.action == "clear")


_TABLE_ACTIONS: tuple[tuple[str, DatabaseActionKind, str, tuple[str, ...]], ...] = (
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


def _database_paths(home: Path, errors: list[str]) -> list[Path]:
    sqlite_dir = home / "sqlite"
    legacy = home / "state_5.sqlite"
    candidates: set[Path] = set()
    can_use_discovery_candidates = True

    try:
        sqlite_info = sqlite_dir.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        errors.append(f"could not inspect database directory {sqlite_dir}: {error}")
        can_use_discovery_candidates = False
    else:
        if stat.S_ISLNK(sqlite_info.st_mode):
            errors.append(f"database directory is a symlink: {sqlite_dir}")
            can_use_discovery_candidates = False
        elif not stat.S_ISDIR(sqlite_info.st_mode):
            errors.append(f"database path is not a directory: {sqlite_dir}")
            can_use_discovery_candidates = False
        else:
            try:
                candidates.update(
                    path
                    for path in sqlite_dir.iterdir()
                    if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}
                )
            except OSError as error:
                errors.append(f"could not inspect database directory {sqlite_dir}: {error}")
                can_use_discovery_candidates = False

    try:
        legacy.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        errors.append(f"could not inspect database path {legacy}: {error}")
        can_use_discovery_candidates = False
    else:
        candidates.add(legacy)
        if legacy.is_symlink():
            can_use_discovery_candidates = False

    if can_use_discovery_candidates:
        try:
            candidates.update(candidate_session_databases(home))
        except OSError as error:
            errors.append(f"could not enumerate session databases: {error}")

    try:
        candidates.update(home.glob("memories_*.sqlite"))
    except OSError as error:
        errors.append(f"could not enumerate memory databases in {home}: {error}")

    paths: list[Path] = []
    for path in sorted(candidates):
        try:
            info = path.lstat()
        except FileNotFoundError as error:
            errors.append(f"database path disappeared during planning {path}: {error}")
            continue
        except OSError as error:
            errors.append(f"could not inspect database path {path}: {error}")
            continue
        if stat.S_ISLNK(info.st_mode):
            errors.append(f"database path is a symlink: {path}")
            continue
        if not stat.S_ISREG(info.st_mode):
            errors.append(f"database path is not a regular file: {path}")
            continue
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            errors.append(f"could not resolve database path {path}: {error}")
            continue
        allowed = (
            (path == legacy and resolved == legacy)
            or (
                path.parent == sqlite_dir
                and resolved.parent == sqlite_dir
                and path.name == resolved.name
            )
            or (
                path.parent == home
                and path.name.startswith("memories_")
                and path.suffix == ".sqlite"
                and resolved.parent == home
                and path.name == resolved.name
            )
        )
        if not allowed:
            errors.append(f"database path is outside its permitted location: {path}")
            continue
        paths.append(path)
    return paths


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
                for row in db.execute(f'SELECT * FROM "{table}" WHERE {where_clause}', params)
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
    if not path.is_absolute():
        return False
    try:
        info = path.lstat()
        resolved = path.resolve()
        roots = (home / "sessions", home / "archived_sessions")
        return (
            stat.S_ISREG(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and any(_is_within(resolved, root.resolve()) for root in roots)
        )
    except (OSError, RuntimeError):
        return False


def _safe_session_roots(home: Path, errors: list[str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for root in (home / "sessions", home / "archived_sessions"):
        try:
            info = root.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(f"could not inspect session directory {root}: {error}")
            continue
        if stat.S_ISLNK(info.st_mode):
            errors.append(f"session directory is a symlink: {root}")
            continue
        if not stat.S_ISDIR(info.st_mode):
            errors.append(f"session directory is not a directory: {root}")
            continue
        try:
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            errors.append(f"could not resolve session directory {root}: {error}")
            continue
        if resolved.parent != home or resolved.name != root.name:
            errors.append(f"session directory is outside CODEX_HOME: {root}")
            continue
        roots.append(root)
    return tuple(roots)


def _has_symlinked_ancestry(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts[:-1]:
        if part in {"", ".", ".."}:
            return True
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except OSError:
            return True
    return False


def _plan_rollout_deletions(
    home: Path,
    session_id: str,
    selected_paths: set[str],
    references: tuple[tuple[str, str], ...],
    warnings: list[str],
    errors: list[str],
) -> list[FileDeletion]:
    allowed_roots = _safe_session_roots(home, errors)
    shared_paths: set[Path] = set()
    for thread_id, value in references:
        if thread_id == session_id:
            continue
        try:
            shared_paths.add(Path(value).resolve())
        except (OSError, RuntimeError) as error:
            errors.append(
                f"could not resolve rollout path referenced by session {thread_id}: "
                f"{value}: {error}"
            )
    deletions: list[FileDeletion] = []
    seen: set[Path] = set()
    for value in sorted(selected_paths):
        path = Path(value)
        if not path.is_absolute():
            errors.append(f"rollout path is not absolute: {path}")
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            try:
                resolved = path.resolve()
            except FileNotFoundError:
                warnings.append(f"rollout file not found: {path}")
                continue
            except (OSError, RuntimeError) as error:
                errors.append(f"could not resolve rollout path {path}: {error}")
                continue
            if not any(_is_within(resolved, root) for root in allowed_roots):
                errors.append(f"rollout path is outside the Codex session directories: {path}")
            else:
                warnings.append(f"rollout file not found: {path}")
            continue
        except OSError as error:
            errors.append(f"could not inspect rollout path {path}: {error}")
            continue
        if stat.S_ISLNK(info.st_mode):
            errors.append(f"rollout path is a symlink: {path}")
            continue
        if not stat.S_ISREG(info.st_mode):
            errors.append(f"rollout path is not a regular file: {path}")
            continue
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            warnings.append(f"rollout file not found: {path}")
            continue
        except (OSError, RuntimeError) as error:
            errors.append(f"could not resolve rollout path {path}: {error}")
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        lexical_root = next((root for root in allowed_roots if _is_within(path, root)), None)
        if lexical_root is not None and _has_symlinked_ancestry(path, lexical_root):
            errors.append(f"rollout path has symlinked ancestry: {path}")
            continue
        containing_root = next((root for root in allowed_roots if _is_within(resolved, root)), None)
        if containing_root is None:
            errors.append(f"rollout path is outside the Codex session directories: {path}")
            continue
        if resolved in shared_paths:
            errors.append(f"rollout file is referenced by another session: {path}")
            continue
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            warnings.append(f"rollout file not found: {path}")
            continue
        except OSError as error:
            errors.append(f"could not read rollout file {path}: {error}")
            continue
        deletions.append(
            FileDeletion(
                path=path,
                original=original,
                original_digest=hashlib.sha256(original).hexdigest(),
                original_device=info.st_dev,
                original_inode=info.st_ino,
                original_mode=stat.S_IMODE(info.st_mode),
            )
        )
    return deletions


def _plan_global_state(home: Path, session_id: str, errors: list[str]) -> FileChange | None:
    path = home / ".codex-global-state.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        errors.append(f"could not inspect global state {path}: {error}")
        return None
    if stat.S_ISLNK(info.st_mode):
        errors.append(f"global state is a symlink: {path}")
        return None
    if not stat.S_ISREG(info.st_mode):
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
    database_actions: list[DatabaseAction] = []
    warnings: list[str] = []
    errors: list[str] = []
    references: list[tuple[str, str]] = []
    database_paths = _database_paths(home, errors)
    session: Session | None = None
    if not errors:
        try:
            session = next(
                (item for item in discover_sessions(home) if item.id == session_id),
                None,
            )
        except (OSError, RuntimeError, sqlite3.Error) as error:
            errors.append(f"could not discover sessions: {error}")

    for path in database_paths:
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

    if session is None:
        errors.append(f"could not discover metadata for session: {session_id}")
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
