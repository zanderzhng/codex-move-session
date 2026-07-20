from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .discovery import Session, discover_sessions
from .planner import DatabaseChange, FileChange, MigrationPlan
from .session_files import load_session_index
from .sqlite_read import open_sqlite_snapshot


@dataclass(frozen=True)
class DoctorIssue:
    session_id: str
    code: str
    detail: str
    repairable: bool


@dataclass(frozen=True)
class DoctorPlan:
    home: Path
    sessions: tuple[Session, ...]
    issues: tuple[DoctorIssue, ...]
    database_changes: tuple[DatabaseChange, ...]
    file_changes: tuple[FileChange, ...]
    errors: tuple[str, ...]

    @property
    def has_repairs(self) -> bool:
        return bool(self.database_changes or self.file_changes)

    def migration_plan(self) -> MigrationPlan:
        return MigrationPlan(
            home=self.home,
            old=str(self.home),
            new=str(self.home),
            sessions=self.sessions,
            database_changes=self.database_changes,
            file_changes=self.file_changes,
            warnings=(),
            errors=self.errors,
        )


def _index_repair(
    home: Path, sessions: list[Session], issues: list[DoctorIssue], errors: list[str]
) -> FileChange | None:
    path = home / "session_index.jsonl"
    created = not path.exists()
    if created:
        original = b""
    else:
        if not path.is_file():
            errors.append(f"session index is not a regular file: {path}")
            return None
        try:
            original = path.read_bytes()
            original.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f"could not read session index {path}: {error}")
            return None
    existing = load_session_index(path)
    sessions_by_id = {session.id: session for session in sessions}
    output_lines: list[bytes] = []
    replacements = 0
    if original:
        for raw in original.splitlines(keepends=True):
            ending = (
                b"\r\n"
                if raw.endswith(b"\r\n")
                else b"\n"
                if raw.endswith(b"\n")
                else b""
            )
            body = raw.rstrip(b"\r\n")
            try:
                item = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                output_lines.append(raw)
                continue
            session_id = (
                item.get("id") or item.get("session_id") if isinstance(item, dict) else None
            )
            session = sessions_by_id.get(session_id) if isinstance(session_id, str) else None
            current_title = item.get("thread_name") if isinstance(item, dict) else None
            if (
                session is not None
                and session.title
                and session.title != session.id
                and (
                    not isinstance(current_title, str)
                    or not current_title
                    or current_title == session.id
                )
            ):
                updated = dict(item)
                updated["thread_name"] = session.title
                output_lines.append(
                    json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode()
                    + ending
                )
                issues.append(
                    DoctorIssue(session.id, "weak_session_index_title", str(path), True)
                )
                replacements += 1
            else:
                output_lines.append(raw)
    additions: list[bytes] = []
    for session in sessions:
        if session.id in existing:
            continue
        issues.append(DoctorIssue(session.id, "missing_session_index_entry", str(path), True))
        updated_at = datetime.fromtimestamp(
            session.updated_at_ms / 1000, tz=timezone.utc
        ).isoformat()
        entry = {
            "id": session.id,
            "thread_name": session.title or session.id,
            "updated_at": updated_at,
        }
        additions.append(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        )
    if not additions and not replacements:
        return None
    output = b"".join(output_lines)
    separator = b"" if not output or output.endswith((b"\n", b"\r")) else b"\n"
    return FileChange(
        path=path,
        area="session-index-repair",
        original=original,
        updated=output + separator + b"".join(additions),
        original_digest=hashlib.sha256(original).hexdigest(),
        replacements=replacements + len(additions),
        created=created,
    )


def build_doctor_plan(home: Path) -> DoctorPlan:
    home = home.expanduser().resolve()
    sessions = discover_sessions(home)
    issues: list[DoctorIssue] = []
    changes: list[DatabaseChange] = []
    errors: list[str] = []
    for session in sessions:
        for code in session.issues:
            issues.append(
                DoctorIssue(
                    session.id,
                    code,
                    ", ".join(str(item.path) for item in session.rollouts)
                    or session.rollout_path,
                    code == "rollout_path_mismatch",
                )
            )
        if not session.rollouts:
            continue
        preferred = max(
            session.rollouts,
            key=lambda item: (not item.archived, item.updated_at_ms, str(item.path)),
        )
        for record in session.records:
            try:
                with open_sqlite_snapshot(record.db_path) as db:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            except (OSError, RuntimeError, sqlite3.Error) as error:
                errors.append(f"could not inspect {record.db_path}: {error}")
                continue
            if "rollout_path" in columns and record.rollout_path != str(preferred.path):
                changes.append(
                    DatabaseChange(
                        record.db_path,
                        "threads",
                        "id",
                        session.id,
                        "rollout_path",
                        record.rollout_path,
                        str(preferred.path),
                        1,
                    )
                )
            if "archived" in columns and record.archived != preferred.archived:
                changes.append(
                    DatabaseChange(
                        record.db_path,
                        "threads",
                        "id",
                        session.id,
                        "archived",
                        int(record.archived),
                        int(preferred.archived),
                        1,
                    )
                )
            if "title" in columns and not record.title and session.title:
                issues.append(
                    DoctorIssue(session.id, "missing_database_title", str(record.db_path), True)
                )
                changes.append(
                    DatabaseChange(
                        record.db_path,
                        "threads",
                        "id",
                        session.id,
                        "title",
                        record.title,
                        session.title,
                        1,
                    )
                )
    index_change = _index_repair(home, sessions, issues, errors)
    file_changes = () if index_change is None else (index_change,)
    return DoctorPlan(
        home,
        tuple(sessions),
        tuple(issues),
        tuple(changes),
        file_changes,
        tuple(errors),
    )
