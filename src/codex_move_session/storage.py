from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

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
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
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


def _backup_database(source_path: Path, backup_path: Path) -> None:
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as destination:
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


def _apply_database_changes(path: Path, changes: list[DatabaseChange]) -> None:
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for change in changes:
                table = _quote_identifier(change.table)
                column = _quote_identifier(change.column)
                key_column = _quote_identifier(change.key_column)
                cursor = db.execute(
                    f"UPDATE {table} SET {column} = ? "
                    f"WHERE {key_column} = ? AND {column} = ?",
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


def _restore_backup(backup_dir: Path, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for entry in manifest["databases"]:
        destination = Path(entry["original"])
        source = backup_dir / entry["backup"]
        try:
            _restore_copy(source, destination)
            Path(f"{destination}-wal").unlink(missing_ok=True)
            Path(f"{destination}-shm").unlink(missing_ok=True)
        except OSError as error:
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
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
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
