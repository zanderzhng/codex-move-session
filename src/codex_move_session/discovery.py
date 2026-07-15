from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .sqlite_read import open_sqlite_snapshot

SessionScope = Literal["active", "archived", "all"]


@dataclass(frozen=True)
class SessionRecord:
    id: str
    title: str
    cwd: str
    archived: bool
    updated_at_ms: int
    rollout_path: str
    sandbox_policy: str | None
    db_path: Path


@dataclass(frozen=True)
class Session:
    id: str
    title: str
    cwd: str
    archived: bool
    updated_at_ms: int
    rollout_path: str
    records: tuple[SessionRecord, ...]


@dataclass(frozen=True)
class StaleGroup:
    path: str
    sessions: tuple[Session, ...]

    @property
    def count(self) -> int:
        return len(self.sessions)


def candidate_session_databases(home: Path) -> list[Path]:
    candidates: list[Path] = []
    sqlite_dir = home / "sqlite"
    if sqlite_dir.is_dir():
        candidates.extend(
            path
            for path in sorted(sqlite_dir.iterdir())
            if path.is_file() and path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}
        )
    legacy = home / "state_5.sqlite"
    if legacy.is_file():
        candidates.append(legacy)
    return candidates


def _read_records(path: Path) -> list[SessionRecord]:
    try:
        with open_sqlite_snapshot(path) as db:
            db.row_factory = sqlite3.Row
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
            ).fetchone()
            if table is None:
                return []
            columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            if not {"id", "cwd"}.issubset(columns):
                return []

            def column(name: str, fallback: str) -> str:
                return f'"{name}"' if name in columns else fallback

            updated = (
                '"updated_at_ms"'
                if "updated_at_ms" in columns
                else '"updated_at" * 1000'
                if "updated_at" in columns
                else '"created_at_ms"'
                if "created_at_ms" in columns
                else "0"
            )
            query = f"""SELECT id,
                {column('title', "''")} AS title,
                cwd,
                {column('archived', '0')} AS archived,
                {updated} AS updated_at_ms,
                {column('rollout_path', "''")} AS rollout_path,
                {column('sandbox_policy', 'NULL')} AS sandbox_policy
                FROM threads"""
            rows = db.execute(query).fetchall()
            return [
                SessionRecord(
                    id=str(row["id"]),
                    title=row["title"] or "",
                    cwd=row["cwd"] or "",
                    archived=bool(row["archived"]),
                    updated_at_ms=int(row["updated_at_ms"] or 0),
                    rollout_path=row["rollout_path"] or "",
                    sandbox_policy=row["sandbox_policy"],
                    db_path=path,
                )
                for row in rows
            ]
    except sqlite3.Error:
        return []


def discover_sessions(home: Path) -> list[Session]:
    grouped: dict[str, list[SessionRecord]] = {}
    for db_path in candidate_session_databases(home):
        for record in _read_records(db_path):
            grouped.setdefault(record.id, []).append(record)

    sessions = []
    for session_id, records in grouped.items():
        preferred = max(records, key=lambda record: (record.updated_at_ms, str(record.db_path)))
        sessions.append(
            Session(
                id=session_id,
                title=preferred.title,
                cwd=preferred.cwd,
                archived=preferred.archived,
                updated_at_ms=preferred.updated_at_ms,
                rollout_path=preferred.rollout_path,
                records=tuple(records),
            )
        )
    return sorted(sessions, key=lambda session: (session.updated_at_ms, session.id), reverse=True)


def stale_groups(sessions: list[Session], *, scope: SessionScope) -> list[StaleGroup]:
    if scope not in {"active", "archived", "all"}:
        raise ValueError(f"unknown session scope: {scope}")
    grouped: dict[str, list[Session]] = {}
    for session in sessions:
        if not session.cwd or not Path(session.cwd).is_absolute() or Path(session.cwd).is_dir():
            continue
        if scope == "active" and session.archived:
            continue
        if scope == "archived" and not session.archived:
            continue
        grouped.setdefault(session.cwd, []).append(session)
    return [
        StaleGroup(path=path, sessions=tuple(items))
        for path, items in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
