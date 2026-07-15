from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def open_sqlite_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    wal_path = Path(f"{path}-wal")
    if not wal_path.exists():
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            yield db
        finally:
            db.close()
        return

    with tempfile.TemporaryDirectory(prefix="codex-move-session-sqlite-") as directory:
        snapshot = Path(directory) / path.name
        snapshot_wal = Path(f"{snapshot}-wal")
        for attempt in range(3):
            try:
                before = (_signature(path), _signature(wal_path))
                shutil.copyfile(path, snapshot)
                shutil.copyfile(wal_path, snapshot_wal)
                after = (_signature(path), _signature(wal_path))
            except FileNotFoundError as error:
                if attempt == 2:
                    message = f"SQLite files kept changing during snapshot: {path}"
                    raise RuntimeError(message) from error
                continue
            if before == after:
                break
        else:
            raise RuntimeError(f"SQLite files kept changing during snapshot: {path}")
        db = sqlite3.connect(snapshot)
        try:
            yield db
        finally:
            db.close()


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns
