from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import Session, SessionScope, candidate_session_databases, discover_sessions
from .paths import PathMapper
from .sqlite_read import open_sqlite_snapshot


class PathKeyCollisionError(ValueError):
    pass


def _serialized_text_contains_path(value: str, mapper: PathMapper) -> bool:
    if mapper.replace_text(value)[1]:
        return True
    encoded = json.dumps(mapper.old, ensure_ascii=False)[1:-1]
    if mapper.flavor == "windows":
        return encoded.casefold() in value.casefold()
    return encoded in value


@dataclass(frozen=True)
class DatabaseChange:
    path: Path
    table: str
    key_column: str
    key: str
    column: str
    original: str
    updated: str
    replacements: int


@dataclass(frozen=True)
class FileChange:
    path: Path
    area: str
    original: bytes
    updated: bytes
    original_digest: str
    replacements: int


@dataclass(frozen=True)
class MigrationPlan:
    home: Path
    old: str
    new: str
    sessions: tuple[Session, ...]
    database_changes: tuple[DatabaseChange, ...]
    file_changes: tuple[FileChange, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def replacement_count(self) -> int:
        return sum(change.replacements for change in self.database_changes) + sum(
            change.replacements for change in self.file_changes
        )

    @property
    def has_changes(self) -> bool:
        return bool(self.database_changes or self.file_changes)


def _replace_json(value: Any, mapper: PathMapper) -> tuple[Any, int]:
    if isinstance(value, str):
        return mapper.replace_text(value)
    if isinstance(value, list):
        result = []
        count = 0
        for item in value:
            updated, item_count = _replace_json(item, mapper)
            result.append(updated)
            count += item_count
        return result, count
    if isinstance(value, dict):
        result = {}
        source_keys = {}
        count = 0
        for key, item in value.items():
            updated_key, key_count = mapper.replace_text(key) if isinstance(key, str) else (key, 0)
            if updated_key in source_keys and source_keys[updated_key] != key:
                raise PathKeyCollisionError(
                    f"path-key collision: {source_keys[updated_key]!r} and {key!r} "
                    f"both map to {updated_key!r}"
                )
            updated_item, item_count = _replace_json(item, mapper)
            result[updated_key] = updated_item
            source_keys[updated_key] = key
            count += key_count + item_count
        return result, count
    return value, 0


def _json_text_change(
    value: str, mapper: PathMapper, *, location: str, errors: list[str]
) -> tuple[str, int]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        if _serialized_text_contains_path(value, mapper):
            errors.append(f"{location}: invalid JSON containing the old path: {error}")
        return value, 0
    try:
        updated, count = _replace_json(parsed, mapper)
    except PathKeyCollisionError as error:
        errors.append(f"{location}: {error}")
        return value, 0
    if count == 0:
        return value, 0
    return json.dumps(updated, ensure_ascii=False, separators=(",", ":")), count


def _plan_rollout(path: Path, mapper: PathMapper, errors: list[str]) -> FileChange | None:
    original = path.read_bytes()
    output = bytearray()
    replacements = 0
    for line_number, raw_line in enumerate(original.splitlines(keepends=True), start=1):
        if raw_line.endswith(b"\r\n"):
            body, ending = raw_line[:-2], b"\r\n"
        elif raw_line.endswith(b"\n"):
            body, ending = raw_line[:-1], b"\n"
        elif raw_line.endswith(b"\r"):
            body, ending = raw_line[:-1], b"\r"
        else:
            body, ending = raw_line, b""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            errors.append(f"{path}:{line_number}: rollout is not UTF-8: {error}")
            output.extend(raw_line)
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            if _serialized_text_contains_path(text, mapper):
                errors.append(
                    f"{path}:{line_number}: invalid JSON containing the old path: {error}"
                )
            output.extend(raw_line)
            continue
        try:
            updated, count = _replace_json(parsed, mapper)
        except PathKeyCollisionError as error:
            errors.append(f"{path}:{line_number}: {error}")
            output.extend(raw_line)
            continue
        if count:
            encoded = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode()
            output.extend(encoded + ending)
            replacements += count
        else:
            output.extend(raw_line)
    if replacements == 0:
        return None
    return FileChange(
        path=path,
        area="rollout",
        original=original,
        updated=bytes(output),
        original_digest=hashlib.sha256(original).hexdigest(),
        replacements=replacements,
    )


def _memory_databases(home: Path) -> list[Path]:
    paths = set(candidate_session_databases(home))
    paths.update(path for path in home.glob("memories_*.sqlite") if path.is_file())
    return sorted(paths)


def _plan_memories(
    home: Path, thread_ids: set[str], mapper: PathMapper, errors: list[str]
) -> list[DatabaseChange]:
    if not thread_ids:
        return []
    changes: list[DatabaseChange] = []
    for path in _memory_databases(home):
        try:
            with open_sqlite_snapshot(path) as db:
                table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage1_outputs'"
                ).fetchone()
                if table is None:
                    continue
                columns = {row[1] for row in db.execute("PRAGMA table_info(stage1_outputs)")}
                text_columns = [
                    name for name in ("raw_memory", "rollout_summary") if name in columns
                ]
                if "thread_id" not in columns or not text_columns:
                    continue
                placeholders = ",".join("?" for _ in thread_ids)
                query = (
                    f"SELECT thread_id, {', '.join(text_columns)} FROM stage1_outputs "
                    f"WHERE thread_id IN ({placeholders})"
                )
                for row in db.execute(query, tuple(sorted(thread_ids))):
                    for index, column in enumerate(text_columns, start=1):
                        original = row[index]
                        if not isinstance(original, str):
                            continue
                        updated, count = mapper.replace_text(original)
                        if count:
                            changes.append(
                                DatabaseChange(
                                    path=path,
                                    table="stage1_outputs",
                                    key_column="thread_id",
                                    key=row[0],
                                    column=column,
                                    original=original,
                                    updated=updated,
                                    replacements=count,
                                )
                            )
        except (OSError, RuntimeError, sqlite3.Error) as error:
            errors.append(f"could not inspect memory database {path}: {error}")
    return changes


def _plan_json_file(
    path: Path,
    mapper: PathMapper,
    *,
    area: str,
    keys: tuple[str, ...] | None,
    errors: list[str],
) -> FileChange | None:
    original = path.read_bytes()
    try:
        parsed = json.loads(original)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: invalid JSON: {error}")
        return None
    count = 0
    try:
        if keys is None:
            updated, count = _replace_json(parsed, mapper)
        else:
            if not isinstance(parsed, dict):
                errors.append(f"{path}: expected a JSON object")
                return None
            updated = dict(parsed)
            for key in keys:
                if key not in parsed:
                    continue
                updated_value, item_count = _replace_json(parsed[key], mapper)
                updated[key] = updated_value
                count += item_count
    except PathKeyCollisionError as error:
        errors.append(f"{path}: {error}")
        return None
    if count == 0:
        return None
    output = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    return FileChange(
        path=path,
        area=area,
        original=original,
        updated=output,
        original_digest=hashlib.sha256(original).hexdigest(),
        replacements=count,
    )


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
    try:
        updated_hint, count = _replace_json(hints[session_id], mapper)
    except PathKeyCollisionError as error:
        errors.append(f"{path}: {error}")
        return None
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


def _rollout_owners(
    sessions: list[Session], errors: list[str]
) -> tuple[dict[Path, set[str]], bool]:
    owners: dict[Path, set[str]] = {}
    complete = True
    for session in sessions:
        for record in session.records:
            if not record.rollout_path:
                continue
            path = Path(record.rollout_path)
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                complete = False
                errors.append(
                    f"could not resolve rollout path referenced by session "
                    f"{session.id}: {path}: {error}"
                )
                continue
            owners.setdefault(resolved, set()).add(session.id)
    return owners, complete


def build_plan(
    home: Path,
    old: str,
    new: str,
    *,
    include_archived: bool = False,
    scope: SessionScope | None = None,
    session_id: str | None = None,
) -> MigrationPlan:
    home = home.expanduser().resolve()
    mapper = PathMapper(old, new)
    errors: list[str] = []
    warnings: list[str] = []
    database_changes: list[DatabaseChange] = []
    file_changes: list[FileChange] = []
    affected_sessions: list[Session] = []
    rollout_paths: set[Path] = set()

    effective_scope: SessionScope = scope or ("all" if include_archived else "active")
    discovered_sessions = discover_sessions(home)
    if session_id is not None:
        rollout_owners, rollout_ownership_complete = _rollout_owners(discovered_sessions, errors)
    else:
        rollout_owners, rollout_ownership_complete = {}, True
    for session in discovered_sessions:
        if session_id is not None and session.id != session_id:
            continue
        if effective_scope == "active" and session.archived:
            continue
        if effective_scope == "archived" and not session.archived:
            continue
        affected_records = [record for record in session.records if mapper.map_path(record.cwd)]
        if not affected_records:
            continue
        affected_sessions.append(session)
        for record in affected_records:
            mapped_cwd = mapper.map_path(record.cwd)
            if mapped_cwd is not None and mapped_cwd != record.cwd:
                database_changes.append(
                    DatabaseChange(
                        path=record.db_path,
                        table="threads",
                        key_column="id",
                        key=record.id,
                        column="cwd",
                        original=record.cwd,
                        updated=mapped_cwd,
                        replacements=1,
                    )
                )
            if record.sandbox_policy:
                updated_policy, count = _json_text_change(
                    record.sandbox_policy,
                    mapper,
                    location=f"{record.db_path}:threads[{record.id}].sandbox_policy",
                    errors=errors,
                )
                if count:
                    database_changes.append(
                        DatabaseChange(
                            path=record.db_path,
                            table="threads",
                            key_column="id",
                            key=record.id,
                            column="sandbox_policy",
                            original=record.sandbox_policy,
                            updated=updated_policy,
                            replacements=count,
                        )
                    )
            if record.rollout_path:
                rollout_paths.add(Path(record.rollout_path))

    for path in sorted(rollout_paths):
        if session_id is not None and not rollout_ownership_complete:
            continue
        if not path.is_file():
            errors.append(f"rollout file not found: {path}")
            continue
        if session_id is not None:
            try:
                owners = rollout_owners.get(path.resolve(strict=True), set())
            except (OSError, RuntimeError) as error:
                errors.append(f"could not resolve rollout path {path}: {error}")
                continue
            other_sessions = owners - {session_id}
            if other_sessions:
                errors.append(
                    f"rollout file is referenced by another session "
                    f"({', '.join(sorted(other_sessions))}): {path}"
                )
                continue
        change = _plan_rollout(path, mapper, errors)
        if change:
            file_changes.append(change)

    thread_ids = {session.id for session in affected_sessions}
    database_changes.extend(_plan_memories(home, thread_ids, mapper, errors))

    global_path = home / ".codex-global-state.json"
    if global_path.is_file():
        if session_id is not None:
            change = _plan_selected_thread_hint(global_path, session_id, mapper, errors)
        else:
            change = _plan_json_file(
                global_path,
                mapper,
                area="global-state",
                keys=(
                    "electron-saved-workspace-roots",
                    "active-workspace-roots",
                    "project-order",
                    "electron-workspace-root-labels",
                    "thread-workspace-root-hints",
                ),
                errors=errors,
            )
        if change:
            file_changes.append(change)
    cap_sid = home / "cap_sid"
    if session_id is None and cap_sid.is_file():
        change = _plan_json_file(cap_sid, mapper, area="cap_sid", keys=None, errors=errors)
        if change:
            file_changes.append(change)

    if not Path(new).is_dir():
        warnings.append(f"destination directory does not exist: {new}")
    return MigrationPlan(
        home=home,
        old=mapper.old,
        new=mapper.new,
        sessions=tuple(affected_sessions),
        database_changes=tuple(database_changes),
        file_changes=tuple(file_changes),
        warnings=tuple(warnings),
        errors=tuple(errors),
    )
