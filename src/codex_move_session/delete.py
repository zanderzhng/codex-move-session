from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .discovery import Session, SessionScope, candidate_session_databases, discover_sessions
from .planner import DatabaseChange, FileChange
from .session_files import RolloutRecord
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
    database_changes: tuple[DatabaseChange, ...]
    database_actions: tuple[DatabaseAction, ...]
    file_deletions: tuple[FileDeletion, ...]
    file_updates: tuple[FileChange, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        return bool(
            self.database_changes
            or self.database_actions
            or self.file_deletions
            or self.file_updates
        )

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


def _plan_global_state(
    home: Path,
    session_id: str,
    errors: list[str],
    *,
    cwd: str = "",
    remove_workspace: bool = False,
) -> FileChange | None:
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
    updated = dict(parsed)
    replacements = 0
    hints = updated.get("thread-workspace-root-hints")
    if hints is not None and not isinstance(hints, dict):
        errors.append(f"{path}: expected thread-workspace-root-hints to be a JSON object")
        return None
    if isinstance(hints, dict) and session_id in hints:
        updated_hints = dict(hints)
        del updated_hints[session_id]
        updated["thread-workspace-root-hints"] = updated_hints
        replacements += 1
    pinned = updated.get("pinned-thread-ids")
    if isinstance(pinned, list):
        filtered = [item for item in pinned if item != session_id]
        replacements += len(pinned) - len(filtered)
        updated["pinned-thread-ids"] = filtered
    orders = updated.get("sidebar-project-thread-orders")
    if isinstance(orders, dict):
        next_orders = dict(orders)
        for workspace, raw_order in orders.items():
            if not isinstance(raw_order, dict) or not isinstance(raw_order.get("threadIds"), list):
                continue
            thread_ids = raw_order["threadIds"]
            filtered = [item for item in thread_ids if item != session_id]
            if filtered != thread_ids:
                next_order = dict(raw_order)
                next_order["threadIds"] = filtered
                next_orders[workspace] = next_order
                replacements += len(thread_ids) - len(filtered)
                if remove_workspace and workspace == cwd and not filtered:
                    del next_orders[workspace]
        updated["sidebar-project-thread-orders"] = next_orders
    if remove_workspace and cwd:
        for key in (
            "electron-saved-workspace-roots",
            "active-workspace-roots",
            "project-order",
        ):
            values = updated.get(key)
            if isinstance(values, list):
                filtered = [item for item in values if item != cwd]
                replacements += len(values) - len(filtered)
                updated[key] = filtered
        labels = updated.get("electron-workspace-root-labels")
        if isinstance(labels, dict) and cwd in labels:
            next_labels = dict(labels)
            del next_labels[cwd]
            updated["electron-workspace-root-labels"] = next_labels
            replacements += 1
        persisted = updated.get("electron-persisted-atom-state")
        if isinstance(persisted, dict):
            groups = persisted.get("sidebar-collapsed-groups")
            if isinstance(groups, dict) and cwd in groups:
                next_persisted = dict(persisted)
                next_groups = dict(groups)
                del next_groups[cwd]
                next_persisted["sidebar-collapsed-groups"] = next_groups
                updated["electron-persisted-atom-state"] = next_persisted
                replacements += 1
    if not replacements:
        return None
    output = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    return FileChange(
        path=path,
        area="global-state-delete",
        original=original,
        updated=output,
        original_digest=hashlib.sha256(original).hexdigest(),
        replacements=replacements,
    )


def _plan_session_index(home: Path, session_id: str, errors: list[str]) -> FileChange | None:
    path = home / "session_index.jsonl"
    if not path.is_file():
        return None
    try:
        original = path.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        errors.append(f"could not read session index {path}: {error}")
        return None
    kept: list[str] = []
    removed = 0
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        try:
            item = json.loads(body)
        except json.JSONDecodeError:
            kept.append(raw)
            continue
        if not isinstance(item, dict):
            kept.append(raw)
            continue
        if item.get("id") == session_id or item.get("session_id") == session_id:
            removed += 1
        else:
            kept.append(raw)
    if not removed:
        return None
    return FileChange(
        path=path,
        area="session-index-delete",
        original=original,
        updated="".join(kept).encode(),
        original_digest=hashlib.sha256(original).hexdigest(),
        replacements=removed,
    )


def _selected_rollouts(
    session: Session, scope: SessionScope
) -> tuple[set[str], tuple[RolloutRecord, ...]]:
    selected = tuple(
        rollout
        for rollout in session.rollouts
        if scope == "all"
        or (scope == "active" and not rollout.archived)
        or (scope == "archived" and rollout.archived)
    )
    paths = {str(rollout.path) for rollout in selected}
    if not paths and scope == "all":
        paths.update(record.rollout_path for record in session.records if record.rollout_path)
    return paths, selected


def _plan_survivor_updates(
    session: Session, surviving_rollout: RolloutRecord, errors: list[str]
) -> list[DatabaseChange]:
    changes: list[DatabaseChange] = []
    desired = {
        "rollout_path": str(surviving_rollout.path),
        "archived": int(surviving_rollout.archived),
        "archived_at": (
            None
            if not surviving_rollout.archived
            else surviving_rollout.updated_at_ms // 1000
        ),
    }
    for record in session.records:
        try:
            with open_sqlite_snapshot(record.db_path) as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info("threads")')}
                row = db.execute('SELECT * FROM "threads" WHERE id = ?', (session.id,)).fetchone()
                if row is None:
                    continue
                names = [item[1] for item in db.execute('PRAGMA table_info("threads")')]
                current = dict(zip(names, row, strict=True))
                for column, updated in desired.items():
                    if column in columns and current[column] != updated:
                        changes.append(
                            DatabaseChange(
                                path=record.db_path,
                                table="threads",
                                key_column="id",
                                key=session.id,
                                column=column,
                                original=current[column],
                                updated=updated,
                                replacements=1,
                            )
                        )
        except (OSError, RuntimeError, sqlite3.Error) as error:
            errors.append(f"could not plan surviving rollout redirect in {record.db_path}: {error}")
    return changes


def build_deletion_plan(
    home: Path, session_id: str, *, scope: SessionScope = "all"
) -> DeletionPlan:
    if scope not in {"active", "archived", "all"}:
        raise ValueError(f"unknown session scope: {scope}")
    home = home.expanduser().resolve()
    database_changes: list[DatabaseChange] = []
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

    selected_paths: set[str] = set()
    surviving_rollouts: tuple[RolloutRecord, ...] = ()
    if session is not None:
        selected_paths, selected_rollouts = _selected_rollouts(session, scope)
        surviving_rollouts = tuple(
            item for item in session.rollouts if item not in selected_rollouts
        )

    logical_delete = not surviving_rollouts
    for path in database_paths:
        try:
            with open_sqlite_snapshot(path) as db:
                if logical_delete:
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

    if session is None:
        errors.append(f"session not found; could not discover metadata: {session_id}")
        return DeletionPlan(
            home=home,
            session=None,
            database_changes=(),
            database_actions=tuple(database_actions),
            file_deletions=(),
            file_updates=(),
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    if not selected_paths:
        errors.append(f"no {scope} rollout found for session: {session_id}")
        return DeletionPlan(
            home=home,
            session=session,
            database_changes=(),
            database_actions=tuple(database_actions),
            file_deletions=(),
            file_updates=(),
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    if surviving_rollouts:
        surviving = max(
            surviving_rollouts,
            key=lambda item: (not item.archived, item.updated_at_ms, str(item.path)),
        )
        database_changes.extend(_plan_survivor_updates(session, surviving, errors))
    file_deletions = _plan_rollout_deletions(
        home,
        session_id,
        selected_paths,
        tuple(references),
        warnings,
        errors,
    )
    file_updates: list[FileChange] = []
    if logical_delete:
        other_cwds = {
            item.cwd for item in discover_sessions(home) if item.id != session_id and item.cwd
        }
        for change in (
            _plan_global_state(
                home,
                session_id,
                errors,
                cwd=session.cwd,
                remove_workspace=session.cwd not in other_cwds,
            ),
            _plan_session_index(home, session_id, errors),
        ):
            if change is not None:
                file_updates.append(change)
    return DeletionPlan(
        home=home,
        session=session,
        database_changes=tuple(database_changes),
        database_actions=tuple(database_actions),
        file_deletions=tuple(file_deletions),
        file_updates=tuple(file_updates),
        warnings=tuple(warnings),
        errors=tuple(errors),
    )
