from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections import Counter
from collections.abc import Callable
from contextlib import closing, nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .delete import (
    DatabaseAction,
    DeletionPlan,
    FileDeletion,
    _database_paths,
    _thread_rollout_references,
)
from .planner import DatabaseChange, MigrationPlan


class PlanValidationError(RuntimeError):
    pass


class ConcurrentChangeError(RuntimeError):
    pass


class ProcessRunningError(RuntimeError):
    pass


class ApplyError(RuntimeError):
    def __init__(self, message: str, backup_dir: Path) -> None:
        super().__init__(message)
        self.backup_dir = backup_dir


@dataclass(frozen=True)
class ApplyResult:
    backup_dir: Path | None
    database_changes: int
    file_changes: int


@dataclass(frozen=True)
class DeletionResult:
    backup_dir: Path
    deleted_rows: int
    cleared_assignments: int
    deleted_files: int


@dataclass
class _OpenPlannedFile:
    parent_fd: int
    file_fd: int
    path: Path
    name: str
    content: bytes
    mode: int
    device: int
    inode: int

    def close(self) -> None:
        if self.file_fd >= 0:
            os.close(self.file_fd)
            self.file_fd = -1
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


def _supports_secure_dir_fd() -> bool:
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    return (
        os.name != "nt"
        and os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.unlink in supports_dir_fd
    )


def _planned_relative_parts(home: Path, path: Path, *, rollout: bool) -> tuple[str, ...]:
    try:
        relative = path.relative_to(home)
    except ValueError as error:
        raise ConcurrentChangeError(f"file moved outside CODEX_HOME: {path}") from error
    parts = relative.parts
    if rollout:
        if len(parts) < 2 or parts[0] not in {"sessions", "archived_sessions"}:
            raise ConcurrentChangeError(f"rollout moved outside session directories: {path}")
    elif parts != (".codex-global-state.json",):
        raise ConcurrentChangeError(f"unexpected deletion file update path: {path}")
    if any(part in {"", ".", ".."} for part in parts):
        raise ConcurrentChangeError(f"unsafe file path: {path}")
    return parts


def _validate_fallback_parent(home: Path, path: Path, *, rollout: bool) -> None:
    parts = _planned_relative_parts(home, path, rollout=rollout)
    try:
        home_info = home.lstat()
        if stat.S_ISLNK(home_info.st_mode) or not stat.S_ISDIR(home_info.st_mode):
            raise ConcurrentChangeError(f"CODEX_HOME is not a real directory: {home}")
        resolved_home = home.resolve(strict=True)
        current = home
        for part in parts[:-1]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ConcurrentChangeError(f"file has unsafe ancestry: {path}")
        resolved_parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        if isinstance(error, ConcurrentChangeError):
            raise
        raise ConcurrentChangeError(f"file path changed after preview: {path}: {error}") from error
    try:
        resolved_parent.relative_to(resolved_home)
    except ValueError as error:
        raise ConcurrentChangeError(f"file moved outside CODEX_HOME: {path}") from error


def _open_safe_parent(home: Path, path: Path, *, rollout: bool) -> tuple[int, str]:
    parts = _planned_relative_parts(home, path, rollout=rollout)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = -1
    try:
        current_fd = os.open(home, flags)
        for part in parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1]
    except OSError as error:
        if current_fd >= 0:
            os.close(current_fd)
        raise ConcurrentChangeError(
            f"file symlink or path change after preview: {path}: {error}"
        ) from error


def running_codex_processes() -> list[str]:
    found: set[str] = set()
    current_pid = os.getpid()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if process.info["pid"] == current_pid:
                continue
            name = (process.info.get("name") or "").strip()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        folded = name.casefold()
        if folded in {"codex", "codex.exe", "codex-app-server", "codex-app-server.exe"} or (
            folded.startswith("codex ") and "move-session" not in folded
        ):
            found.add(name)
    return sorted(found)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _group_database_changes(
    changes: tuple[DatabaseChange, ...],
) -> dict[Path, list[DatabaseChange]]:
    grouped: dict[Path, list[DatabaseChange]] = {}
    for change in changes:
        grouped.setdefault(change.path, []).append(change)
    return grouped


def _group_deletion_actions(
    actions: tuple[DatabaseAction, ...],
) -> dict[Path, list[DatabaseAction]]:
    grouped: dict[Path, list[DatabaseAction]] = {}
    for action in actions:
        grouped.setdefault(action.path, []).append(action)
    return grouped


def _quote_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise PlanValidationError(f"unsafe SQLite identifier: {value}")
    return f'"{value}"'


def _read_database_value(db: sqlite3.Connection, change: DatabaseChange) -> Any:
    table = _quote_identifier(change.table)
    column = _quote_identifier(change.column)
    key_column = _quote_identifier(change.key_column)
    row = db.execute(
        f"SELECT {column} FROM {table} WHERE {key_column} = ?", (change.key,)
    ).fetchone()
    if row is None:
        raise ConcurrentChangeError(
            f"database row disappeared after preview: {change.path}:{change.table}[{change.key}]"
        )
    return row[0]


def _validate_unchanged(plan: MigrationPlan) -> None:
    for path, changes in _group_database_changes(plan.database_changes).items():
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        with closing(connection) as db:
            for change in changes:
                current = _read_database_value(db, change)
                if current != change.original:
                    raise ConcurrentChangeError(
                        f"database changed after preview: {path}:{change.table}."
                        f"{change.column}[{change.key}]"
                    )
    for change in plan.file_changes:
        try:
            current = change.path.read_bytes()
        except OSError as error:
            message = f"file changed after preview: {change.path}: {error}"
            raise ConcurrentChangeError(message) from error
        if hashlib.sha256(current).hexdigest() != change.original_digest:
            raise ConcurrentChangeError(f"file changed after preview: {change.path}")


def _read_action_rows(
    db: sqlite3.Connection, action: DatabaseAction
) -> tuple[tuple[Any, ...], ...]:
    table = _quote_identifier(action.table)
    return tuple(
        sorted(
            db.execute(f"SELECT * FROM {table} WHERE {action.where_clause}", action.params),
            key=repr,
        )
    )


def _validate_database_action(action: DatabaseAction) -> None:
    _quote_identifier(action.table)
    for column in action.columns:
        _quote_identifier(column)
    if action.action not in {"delete", "clear"}:
        raise PlanValidationError(f"unsupported database action: {action.action}")
    if action.action == "clear" and "assigned_thread_id" not in action.columns:
        raise PlanValidationError(f"clear action lacks assigned_thread_id column: {action.table}")
    if not action.where_clause or any(
        token in action.where_clause for token in (";", "--", "/*", "*/", '"', "'")
    ):
        raise PlanValidationError(f"unsafe SQLite predicate: {action.where_clause}")


def _open_planned_file(
    home: Path,
    path: Path,
    expected: bytes,
    *,
    deletion: FileDeletion | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> _OpenPlannedFile:
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    current_fd = -1
    file_fd = -1
    try:
        if _supports_secure_dir_fd():
            current_fd, name = _open_safe_parent(home, path, rollout=deletion is not None)
            file_fd = os.open(name, file_flags, dir_fd=current_fd)
            path_info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
        else:
            _validate_fallback_parent(home, path, rollout=deletion is not None)
            name = path.name
            file_fd = os.open(path, file_flags)
            path_info = path.lstat()
            if stat.S_ISLNK(path_info.st_mode):
                raise ConcurrentChangeError(f"file is a symlink: {path}")
        file_info = os.fstat(file_fd)
        if not stat.S_ISREG(file_info.st_mode):
            raise ConcurrentChangeError(f"file is no longer regular: {path}")
        if (path_info.st_dev, path_info.st_ino) != (file_info.st_dev, file_info.st_ino):
            raise ConcurrentChangeError(f"file identity changed after preview: {path}")
        if (
            expected_identity is not None
            and (
                file_info.st_dev,
                file_info.st_ino,
            )
            != expected_identity
        ):
            raise ConcurrentChangeError(f"file identity changed after preview: {path}")
        if deletion is not None and (
            file_info.st_dev != deletion.original_device
            or file_info.st_ino != deletion.original_inode
        ):
            raise ConcurrentChangeError(f"file identity changed after preview: {path}")
        with os.fdopen(os.dup(file_fd), "rb") as handle:
            content = handle.read()
        if content != expected:
            raise ConcurrentChangeError(f"file changed after preview: {path}")
        return _OpenPlannedFile(
            current_fd,
            file_fd,
            path,
            name,
            content,
            file_info.st_mode,
            file_info.st_dev,
            file_info.st_ino,
        )
    except (ConcurrentChangeError, OSError) as error:
        if file_fd >= 0:
            os.close(file_fd)
        if current_fd >= 0:
            os.close(current_fd)
        if isinstance(error, ConcurrentChangeError):
            raise
        detail = (
            "symlink or path change"
            if error.errno in {getattr(os, "ELOOP", 62), 20}
            else str(error)
        )
        raise ConcurrentChangeError(
            f"file symlink or path change after preview: {path}: {detail}"
        ) from error


def _validate_no_shared_rollouts(plan: DeletionPlan, databases: list[sqlite3.Connection]) -> None:
    if plan.session is None:
        return
    planned = {item.path.resolve(strict=False) for item in plan.file_deletions}
    for db in databases:
        for thread_id, value in _thread_rollout_references(db):
            if thread_id == plan.session.id:
                continue
            try:
                referenced = Path(value).resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise ConcurrentChangeError(
                    f"could not revalidate rollout reference for {thread_id}: {value}: {error}"
                ) from error
            if referenced in planned:
                raise ConcurrentChangeError(f"rollout is referenced by another session: {value}")


def _validate_deletion_unchanged(plan: DeletionPlan) -> dict[Path, tuple[int, int]]:
    identities: dict[Path, tuple[int, int]] = {}
    for action in plan.database_actions:
        _validate_database_action(action)
    inspected: list[sqlite3.Connection] = []
    errors: list[str] = []
    known_paths = _database_paths(plan.home, errors)
    if errors:
        raise ConcurrentChangeError("could not revalidate databases: " + "; ".join(errors))
    try:
        for path in known_paths:
            inspected.append(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))
        _validate_no_shared_rollouts(plan, inspected)
    finally:
        for db in inspected:
            db.close()
    for path, actions in _group_deletion_actions(plan.database_actions).items():
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        with closing(connection) as db:
            for action in actions:
                if _read_action_rows(db, action) != action.original_rows:
                    raise ConcurrentChangeError(
                        f"database changed after delete preview: {path}:{action.table}"
                    )
    for item in plan.file_deletions:
        opened = _open_planned_file(plan.home, item.path, item.original, deletion=item)
        identities[item.path] = (opened.device, opened.inode)
        opened.close()
    for item in plan.file_updates:
        opened = _open_planned_file(plan.home, item.path, item.original)
        identities[item.path] = (opened.device, opened.inode)
        opened.close()
    return identities


def _backup_database(source_path: Path, backup_path: Path) -> None:
    with (
        closing(sqlite3.connect(source_path)) as source,
        closing(sqlite3.connect(backup_path)) as destination,
    ):
        source.backup(destination)


def _create_backup(plan: MigrationPlan) -> tuple[Path, dict[str, Any]]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = plan.home / "backups" / f"codex-move-session-{timestamp}"
    database_dir = backup_dir / "databases"
    file_dir = backup_dir / "files"
    database_dir.mkdir(parents=True)
    file_dir.mkdir()
    manifest: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "old": plan.old,
        "new": plan.new,
        "databases": [],
        "files": [],
    }
    for index, path in enumerate(_group_database_changes(plan.database_changes)):
        backup_path = database_dir / f"{index:03d}-{path.name}"
        _backup_database(path, backup_path)
        manifest["databases"].append(
            {"original": str(path), "backup": str(backup_path.relative_to(backup_dir))}
        )
    for index, change in enumerate(plan.file_changes):
        backup_path = file_dir / f"{index:03d}-{change.path.name}.bin"
        backup_path.write_bytes(change.original)
        manifest["files"].append(
            {"original": str(change.path), "backup": str(backup_path.relative_to(backup_dir))}
        )
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return backup_dir, manifest


def _create_deletion_backup(plan: DeletionPlan) -> tuple[Path, dict[str, Any]]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = plan.home / "backups" / f"codex-move-session-{timestamp}"
    database_dir = backup_dir / "databases"
    file_dir = backup_dir / "files"
    database_dir.mkdir(parents=True)
    file_dir.mkdir()
    database_paths = sorted({action.path for action in plan.database_actions})
    file_paths = sorted(
        {item.path for item in plan.file_deletions} | {item.path for item in plan.file_updates}
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
        connection = sqlite3.connect(backup_path.resolve().as_uri() + "?mode=ro", uri=True)
        with closing(connection) as db:
            for action in (item for item in plan.database_actions if item.path == path):
                if _read_action_rows(db, action) != action.original_rows:
                    raise ConcurrentChangeError(
                        f"database changed while creating delete backup: {path}:{action.table}"
                    )
        manifest["databases"].append(
            {"original": str(path), "backup": str(backup_path.relative_to(backup_dir))}
        )
    originals = {item.path: item.original for item in plan.file_deletions}
    originals.update({item.path: item.original for item in plan.file_updates})
    for index, path in enumerate(file_paths):
        backup_path = file_dir / f"{index:03d}-{path.name}.bin"
        backup_path.write_bytes(originals[path])
        manifest["files"].append(
            {"original": str(path), "backup": str(backup_path.relative_to(backup_dir))}
        )
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return backup_dir, manifest


def _apply_database_changes(path: Path, changes: list[DatabaseChange]) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for change in changes:
                table = _quote_identifier(change.table)
                column = _quote_identifier(change.column)
                key_column = _quote_identifier(change.key_column)
                cursor = db.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {key_column} = ? AND {column} = ?",
                    (change.updated, change.key, change.original),
                )
                if cursor.rowcount != 1:
                    raise ConcurrentChangeError(
                        f"database changed during apply: {path}:{change.table}."
                        f"{change.column}[{change.key}]"
                    )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _apply_deletion_actions(
    db: sqlite3.Connection, path: Path, actions: list[DatabaseAction]
) -> None:
    for action in actions:
        if _read_action_rows(db, action) != action.original_rows:
            raise ConcurrentChangeError(f"database changed during delete: {path}:{action.table}")
        table = _quote_identifier(action.table)
        if action.action == "clear":
            cursor = db.execute(
                f"UPDATE {table} SET assigned_thread_id = NULL WHERE {action.where_clause}",
                action.params,
            )
        else:
            cursor = db.execute(f"DELETE FROM {table} WHERE {action.where_clause}", action.params)
        if cursor.rowcount != action.row_count:
            raise ConcurrentChangeError(f"database changed during delete: {path}:{action.table}")


def _remove_file(path: Path, *, dir_fd: int | None = None) -> None:
    os.unlink(path, dir_fd=dir_fd)


def _atomic_write_at(parent_fd: int, name: str, content: bytes, mode: int) -> tuple[int, int]:
    temporary_name = f".{name}.{os.getpid()}.{os.urandom(8).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary_name, flags, stat.S_IMODE(mode), dir_fd=parent_fd)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)


def _atomic_write_opened(
    home: Path, opened: _OpenPlannedFile, content: bytes, *, rollout: bool = False
) -> tuple[int, int]:
    if opened.parent_fd >= 0:
        return _atomic_write_at(opened.parent_fd, opened.name, content, opened.mode)
    _validate_fallback_parent(home, opened.path, rollout=rollout)
    current = opened.path.lstat()
    if (current.st_dev, current.st_ino) != (opened.device, opened.inode):
        raise ConcurrentChangeError(f"file identity changed before update: {opened.path}")
    if os.name == "nt":
        opened.close()
        _validate_fallback_parent(home, opened.path, rollout=rollout)
        current = opened.path.lstat()
        if (current.st_dev, current.st_ino) != (opened.device, opened.inode):
            raise ConcurrentChangeError(f"file identity changed before update: {opened.path}")
    _atomic_write(opened.path, content)
    _validate_fallback_parent(home, opened.path, rollout=rollout)
    updated = opened.path.lstat()
    if stat.S_ISLNK(updated.st_mode) or not stat.S_ISREG(updated.st_mode):
        raise ConcurrentChangeError(f"updated file is no longer regular: {opened.path}")
    return updated.st_dev, updated.st_ino


def _remove_opened_file(home: Path, opened: _OpenPlannedFile) -> None:
    if opened.parent_fd >= 0:
        _remove_file(Path(opened.name), dir_fd=opened.parent_fd)
        return
    _validate_fallback_parent(home, opened.path, rollout=True)
    current = opened.path.lstat()
    if (current.st_dev, current.st_ino) != (opened.device, opened.inode):
        raise ConcurrentChangeError(f"file identity changed before delete: {opened.path}")
    if os.name == "nt":
        opened.close()
        _validate_fallback_parent(home, opened.path, rollout=True)
        current = opened.path.lstat()
        if (current.st_dev, current.st_ino) != (opened.device, opened.inode):
            raise ConcurrentChangeError(f"file identity changed before delete: {opened.path}")
    _remove_file(opened.path)


def _open_deletion_transactions(
    plan: DeletionPlan,
) -> dict[Path, sqlite3.Connection]:
    errors: list[str] = []
    paths = set(_database_paths(plan.home, errors))
    paths.update(action.path for action in plan.database_actions)
    if errors:
        raise ConcurrentChangeError("could not lock deletion databases: " + "; ".join(errors))
    databases: dict[Path, sqlite3.Connection] = {}
    try:
        for path in sorted(paths):
            db = sqlite3.connect(path)
            databases[path] = db
            db.execute("BEGIN IMMEDIATE")
        _validate_no_shared_rollouts(plan, list(databases.values()))
        return databases
    except BaseException:
        for db in databases.values():
            db.rollback()
            db.close()
        raise


def _validate_locked_database_set(
    plan: DeletionPlan, databases: dict[Path, sqlite3.Connection]
) -> None:
    errors: list[str] = []
    current = set(_database_paths(plan.home, errors))
    if errors:
        raise ConcurrentChangeError("could not recheck database set: " + "; ".join(errors))
    if current != set(databases):
        raise ConcurrentChangeError("database set changed during delete apply")
    _validate_no_shared_rollouts(plan, list(databases.values()))


def _commit_deletion_database(db: sqlite3.Connection, _path: Path) -> None:
    db.commit()


def _restore_committed_actions(path: Path, actions: list[DatabaseAction]) -> str | None:
    try:
        with closing(sqlite3.connect(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for action in reversed(actions):
                    table = _quote_identifier(action.table)
                    columns = tuple(_quote_identifier(column) for column in action.columns)
                    if action.action == "delete":
                        if _read_action_rows(db, action):
                            raise ConcurrentChangeError(
                                f"deleted rows were recreated concurrently: {path}:{action.table}"
                            )
                        placeholders = ", ".join("?" for _ in columns)
                        column_list = ", ".join(columns)
                        for row in action.original_rows:
                            db.execute(
                                f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
                                row,
                            )
                    else:
                        assigned_index = action.columns.index("assigned_thread_id")
                        for row, expected_count in Counter(action.original_rows).items():
                            post_row = list(row)
                            post_row[assigned_index] = None
                            predicate = " AND ".join(f"{column} IS ?" for column in columns)
                            cursor = db.execute(
                                f"UPDATE {table} SET assigned_thread_id = ? WHERE {predicate}",
                                (row[assigned_index], *post_row),
                            )
                            if cursor.rowcount != expected_count:
                                raise ConcurrentChangeError(
                                    f"cleared row changed concurrently: {path}:{action.table}"
                                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
    except (OSError, sqlite3.Error, RuntimeError) as error:
        return f"{path}: {error}"
    return None


def _restore_copy(source: Path, destination: Path) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore.", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_database(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as backup,
        closing(sqlite3.connect(destination)) as database,
    ):
        backup.backup(database)


def _restore_backup(backup_dir: Path, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for entry in manifest["databases"]:
        destination = Path(entry["original"])
        source = backup_dir / entry["backup"]
        try:
            _restore_database(source, destination)
            Path(f"{destination}-wal").unlink(missing_ok=True)
            Path(f"{destination}-shm").unlink(missing_ok=True)
        except (OSError, sqlite3.Error) as error:
            errors.append(f"{destination}: {error}")
    for entry in manifest["files"]:
        destination = Path(entry["original"])
        source = backup_dir / entry["backup"]
        try:
            _restore_copy(source, destination)
        except OSError as error:
            errors.append(f"{destination}: {error}")
    return errors


def _verify_applied(plan: MigrationPlan) -> None:
    for path, changes in _group_database_changes(plan.database_changes).items():
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        with closing(connection) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError(f"SQLite quick_check failed: {path}")
            for change in changes:
                if _read_database_value(db, change) != change.updated:
                    raise RuntimeError(
                        f"database verification failed: {path}:{change.table}."
                        f"{change.column}[{change.key}]"
                    )
    for change in plan.file_changes:
        if change.path.read_bytes() != change.updated:
            raise RuntimeError(f"file verification failed: {change.path}")


def _verify_deletion(
    plan: DeletionPlan,
    databases: dict[Path, sqlite3.Connection] | None = None,
    mutated_identities: dict[Path, tuple[int, int]] | None = None,
) -> None:
    for path, actions in _group_deletion_actions(plan.database_actions).items():
        if databases is None:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            manager = closing(connection)
        else:
            manager = nullcontext(databases[path])
        with manager as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError(f"SQLite quick_check failed: {path}")
            for action in actions:
                if _read_action_rows(db, action):
                    raise RuntimeError(
                        f"database delete verification failed: {path}:{action.table}"
                    )
    for item in plan.file_deletions:
        if _supports_secure_dir_fd():
            parent_fd, name = _open_safe_parent(plan.home, item.path, rollout=True)
            try:
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError(f"file delete verification failed: {item.path}")
            finally:
                os.close(parent_fd)
        else:
            _validate_fallback_parent(plan.home, item.path, rollout=True)
            try:
                item.path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError(f"file delete verification failed: {item.path}")
    for item in plan.file_updates:
        expected_identity = None
        if mutated_identities is not None:
            expected_identity = mutated_identities.get(item.path)
        opened = _open_planned_file(
            plan.home,
            item.path,
            item.updated,
            expected_identity=expected_identity,
        )
        opened.close()


def apply_plan(
    plan: MigrationPlan,
    *,
    process_checker: Callable[[], list[str]] = running_codex_processes,
) -> ApplyResult:
    if plan.errors:
        raise PlanValidationError("migration plan contains errors: " + "; ".join(plan.errors))
    if not Path(plan.new).is_dir():
        raise PlanValidationError(f"destination directory does not exist: {plan.new}")
    running = process_checker()
    if running:
        raise ProcessRunningError("Close Codex before applying changes: " + ", ".join(running))
    if not plan.has_changes:
        return ApplyResult(backup_dir=None, database_changes=0, file_changes=0)
    _validate_unchanged(plan)
    backup_dir, manifest = _create_backup(plan)
    try:
        for path, changes in _group_database_changes(plan.database_changes).items():
            _apply_database_changes(path, changes)
        for change in plan.file_changes:
            _atomic_write(change.path, change.updated)
        _verify_applied(plan)
    except BaseException as error:
        restore_errors = _restore_backup(backup_dir, manifest)
        if restore_errors:
            detail = "; ".join(restore_errors)
            raise ApplyError(
                f"apply failed and rollback was incomplete: {error}; {detail}", backup_dir
            ) from error
        raise ApplyError(f"apply failed; touched data restored: {error}", backup_dir) from error
    return ApplyResult(
        backup_dir=backup_dir,
        database_changes=len(plan.database_changes),
        file_changes=len(plan.file_changes),
    )


def _restore_deletion_files(
    plan: DeletionPlan, mutated_identities: dict[Path, tuple[int, int]]
) -> list[str]:
    errors: list[str] = []
    for item in plan.file_updates:
        try:
            expected_identity = mutated_identities.get(item.path)
            if expected_identity is None:
                unchanged = _open_planned_file(plan.home, item.path, item.original)
                unchanged.close()
                continue
            opened = _open_planned_file(
                plan.home,
                item.path,
                item.updated,
                expected_identity=expected_identity,
            )
            try:
                _atomic_write_opened(plan.home, opened, item.original)
            finally:
                opened.close()
        except (ConcurrentChangeError, OSError) as error:
            errors.append(f"{item.path}: unsafe file rollback refused: {error}")
    for item in plan.file_deletions:
        try:
            try:
                unchanged = _open_planned_file(plan.home, item.path, item.original, deletion=item)
            except ConcurrentChangeError:
                if _supports_secure_dir_fd():
                    parent_fd, name = _open_safe_parent(plan.home, item.path, rollout=True)
                    try:
                        try:
                            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            _atomic_write_at(parent_fd, name, item.original, item.original_mode)
                        else:
                            raise ConcurrentChangeError(
                                f"deleted rollout path was reused: {item.path}"
                            )
                    finally:
                        os.close(parent_fd)
                else:
                    _validate_fallback_parent(plan.home, item.path, rollout=True)
                    try:
                        item.path.lstat()
                    except FileNotFoundError:
                        _atomic_write(item.path, item.original)
                        os.chmod(item.path, item.original_mode)
                    else:
                        raise ConcurrentChangeError(f"deleted rollout path was reused: {item.path}")
            else:
                unchanged.close()
        except (ConcurrentChangeError, OSError) as error:
            errors.append(f"{item.path}: unsafe file rollback refused: {error}")
    return errors


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
    file_identities = _validate_deletion_unchanged(plan)
    backup_dir, _manifest = _create_deletion_backup(plan)
    databases: dict[Path, sqlite3.Connection] = {}
    committed: list[Path] = []
    mutated_identities: dict[Path, tuple[int, int]] = {}
    try:
        databases = _open_deletion_transactions(plan)
        _validate_locked_database_set(plan, databases)
        running = process_checker()
        if running:
            raise ProcessRunningError("Close Codex before applying changes: " + ", ".join(running))
        for path, actions in _group_deletion_actions(plan.database_actions).items():
            _apply_deletion_actions(databases[path], path, actions)
        for item in plan.file_updates:
            opened = _open_planned_file(
                plan.home,
                item.path,
                item.original,
                expected_identity=file_identities[item.path],
            )
            try:
                mutated_identities[item.path] = _atomic_write_opened(
                    plan.home, opened, item.updated
                )
            finally:
                opened.close()
        for item in plan.file_deletions:
            opened = _open_planned_file(
                plan.home,
                item.path,
                item.original,
                deletion=item,
                expected_identity=file_identities[item.path],
            )
            try:
                if opened.parent_fd >= 0:
                    current = os.stat(opened.name, dir_fd=opened.parent_fd, follow_symlinks=False)
                else:
                    _validate_fallback_parent(plan.home, item.path, rollout=True)
                    current = item.path.lstat()
                if (current.st_dev, current.st_ino) != (
                    item.original_device,
                    item.original_inode,
                ):
                    raise ConcurrentChangeError(
                        f"file identity changed immediately before delete: {item.path}"
                    )
                _validate_locked_database_set(plan, databases)
                _remove_opened_file(plan.home, opened)
            finally:
                opened.close()
        _verify_deletion(plan, databases, mutated_identities)
        for path, db in databases.items():
            _commit_deletion_database(db, path)
            committed.append(path)
    except ProcessRunningError:
        for db in databases.values():
            with suppress(sqlite3.Error):
                db.rollback()
        raise
    except BaseException as error:
        for path, db in databases.items():
            if path not in committed:
                with suppress(sqlite3.Error):
                    db.rollback()
        restore_errors = _restore_deletion_files(plan, mutated_identities)
        grouped = _group_deletion_actions(plan.database_actions)
        for path in reversed(committed):
            restore_error = _restore_committed_actions(path, grouped.get(path, []))
            if restore_error is not None:
                restore_errors.append(restore_error)
        if restore_errors:
            raise ApplyError(
                f"delete failed and rollback was incomplete: {error}; {'; '.join(restore_errors)}",
                backup_dir,
            ) from error
        raise ApplyError(f"delete failed; touched data restored: {error}", backup_dir) from error
    finally:
        for db in databases.values():
            db.close()
    return DeletionResult(
        backup_dir=backup_dir,
        deleted_rows=plan.deleted_rows,
        cleared_assignments=plan.cleared_assignments,
        deleted_files=len(plan.file_deletions),
    )
