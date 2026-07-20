from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .session_files import (
    RolloutRecord,
    load_history_titles,
    load_session_index,
    read_rollout,
    rollout_files,
)
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
    rollouts: tuple[RolloutRecord, ...] = ()

    @property
    def issues(self) -> tuple[str, ...]:
        result: list[str] = []
        if not self.records:
            result.append("rollout_without_database")
        if self.records and not self.rollouts:
            result.append("database_without_rollout")
        if len(self.rollouts) > 1:
            result.append("duplicate_rollouts")
        known_paths = {str(item.path) for item in self.rollouts}
        if self.records and known_paths and any(
            record.rollout_path and record.rollout_path not in known_paths
            for record in self.records
        ):
            result.append("rollout_path_mismatch")
        return tuple(result)


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
    home = home.expanduser().resolve()
    grouped: dict[str, list[SessionRecord]] = {}
    for db_path in candidate_session_databases(home):
        for record in _read_records(db_path):
            grouped.setdefault(record.id, []).append(record)

    rollout_groups: dict[str, list[RolloutRecord]] = {}
    for path in rollout_files(home):
        rollout = read_rollout(path, home)
        if rollout is not None:
            rollout_groups.setdefault(rollout.id, []).append(rollout)

    index = load_session_index(home / "session_index.jsonl")
    history_titles = load_history_titles(home / "history.jsonl")
    sessions = []
    for session_id in grouped.keys() | rollout_groups.keys():
        records = grouped.get(session_id, [])
        rollouts = rollout_groups.get(session_id, [])
        preferred_record = (
            max(records, key=lambda record: (record.updated_at_ms, str(record.db_path)))
            if records
            else None
        )
        preferred_rollout = (
            max(rollouts, key=lambda item: (not item.archived, item.updated_at_ms, str(item.path)))
            if rollouts
            else None
        )
        index_title = index.get(session_id, {}).get("thread_name", "")
        if index_title == session_id:
            index_title = ""
        title = (
            (preferred_record.title if preferred_record else "")
            or (str(index_title) if index_title else "")
            or (preferred_rollout.title if preferred_rollout else "")
            or history_titles.get(session_id, "")
        )
        sessions.append(
            Session(
                id=session_id,
                title=title,
                cwd=(preferred_record.cwd if preferred_record else "")
                or (preferred_rollout.cwd if preferred_rollout else ""),
                archived=(
                    preferred_record.archived if preferred_record else preferred_rollout.archived
                ),
                updated_at_ms=max(
                    preferred_record.updated_at_ms if preferred_record else 0,
                    preferred_rollout.updated_at_ms if preferred_rollout else 0,
                ),
                rollout_path=(preferred_record.rollout_path if preferred_record else "")
                or (str(preferred_rollout.path) if preferred_rollout else ""),
                records=tuple(records),
                rollouts=tuple(sorted(rollouts, key=lambda item: str(item.path))),
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
