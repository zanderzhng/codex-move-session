import sqlite3
from pathlib import Path

from codex_move_session.sqlite_read import open_sqlite_snapshot


def test_snapshot_reads_database_without_creating_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE items (value TEXT)")
        db.execute("INSERT INTO items VALUES ('main')")
    before = {item.name for item in tmp_path.iterdir()}

    with open_sqlite_snapshot(path) as db:
        assert db.execute("SELECT value FROM items").fetchone()[0] == "main"

    assert {item.name for item in tmp_path.iterdir()} == before


def test_snapshot_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE items (value TEXT)")
        writer.execute("INSERT INTO items VALUES ('from-wal')")
        writer.commit()
        assert Path(f"{path}-wal").is_file()

        with open_sqlite_snapshot(path) as db:
            assert db.execute("SELECT value FROM items").fetchone()[0] == "from-wal"
    finally:
        writer.close()
