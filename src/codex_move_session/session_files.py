from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RolloutRecord:
    id: str
    path: Path
    cwd: str
    title: str
    archived: bool
    updated_at_ms: int


def rollout_files(home: Path) -> list[Path]:
    files: list[Path] = []
    for directory in (home / "sessions", home / "archived_sessions"):
        if directory.is_dir():
            files.extend(path for path in directory.rglob("rollout-*.jsonl") if path.is_file())
    return sorted(files)


def _text_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "content", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            value = _text_from_payload(item)
            if value:
                return value
    return ""


def read_rollout(path: Path, home: Path) -> RolloutRecord | None:
    session_id = ""
    cwd = ""
    title = ""
    timestamp_ms = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload")
                if item.get("type") == "session_meta" and isinstance(payload, dict):
                    value = payload.get("id")
                    if isinstance(value, str):
                        session_id = value
                    value = payload.get("cwd")
                    if isinstance(value, str):
                        cwd = value
                    for key in ("thread_name", "title"):
                        value = payload.get(key)
                        if isinstance(value, str) and value.strip():
                            title = value.strip()
                            break
                if not title and item.get("type") in {"event_msg", "response_item"}:
                    title = _text_from_payload(payload)
    except OSError:
        return None
    if not session_id:
        match = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27,}|thread-[\w.-]+)\.jsonl$", path.name)
        session_id = match.group(1) if match else ""
    if not session_id:
        return None
    with suppress(OSError):
        timestamp_ms = path.stat().st_mtime_ns // 1_000_000
    return RolloutRecord(
        id=session_id,
        path=path,
        cwd=cwd,
        title=title,
        archived=home / "archived_sessions" in path.parents,
        updated_at_ms=timestamp_ms,
    )


def load_session_index(path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return entries
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return entries
    for raw in lines:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        session_id = item.get("id") or item.get("session_id")
        if isinstance(session_id, str) and session_id:
            entries[session_id] = item
    return entries


def load_history_titles(path: Path) -> dict[str, str]:
    titles: dict[str, str] = {}
    if not path.is_file():
        return titles
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return titles
    with handle:
        for raw in handle:
            try:
                item = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            session_id = item.get("session_id")
            text = item.get("text")
            if (
                isinstance(session_id, str)
                and session_id not in titles
                and isinstance(text, str)
                and text.strip()
            ):
                titles[session_id] = text.strip().replace("\n", " ")
    return titles
