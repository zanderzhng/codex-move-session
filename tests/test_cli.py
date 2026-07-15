import sqlite3
from pathlib import Path

from rich.console import Console
from test_apply import read_thread_cwd
from test_planner import create_codex_fixture

from codex_move_session.cli import PromptAdapter, run


def recording_console() -> tuple[Console, object]:
    console = Console(record=True, width=120, color_system=None)
    return console, console


class FakePrompts(PromptAdapter):
    def __init__(self, old: str, new: str, *, confirm: bool) -> None:
        self.old = old
        self.new = new
        self.confirmed = confirm

    def choose_scope(self) -> str | None:
        return "all"

    def choose_old(self, groups: list[object]) -> str | None:
        return self.old

    def choose_new(self, old: str) -> str | None:
        return self.new

    def confirm_apply(self) -> bool:
        return self.confirmed


def test_noninteractive_is_dry_run_and_describes_changes(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, _ = create_codex_fixture(home, old)
    console, recorder = recording_console()

    exit_code = run(
        ["--old", str(old), "--new", str(new), "--codex-home", str(home)],
        console=console,
    )

    output = recorder.export_text()
    assert exit_code == 0
    assert "Dry run" in output
    assert "Moved project" in output
    assert "stage1_outputs.raw_memory" in output
    assert "rollout" in output
    assert read_thread_cwd(state) == str(old)


def test_noninteractive_apply_updates_data(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, _ = create_codex_fixture(home, old)
    console, _ = recording_console()

    exit_code = run(
        [
            "--old",
            str(old),
            "--new",
            str(new),
            "--codex-home",
            str(home),
            "--apply",
        ],
        console=console,
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert read_thread_cwd(state) == str(new)


def test_interactive_selects_stale_path_previews_and_confirms_apply(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, _ = create_codex_fixture(home, old)
    console, _ = recording_console()
    prompts = FakePrompts(str(old), str(new), confirm=True)

    exit_code = run(
        ["--codex-home", str(home)],
        console=console,
        prompts=prompts,
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert read_thread_cwd(state) == str(new)


def test_interactive_cancel_leaves_data_unchanged(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, _ = create_codex_fixture(home, old)
    console, _ = recording_console()

    exit_code = run(
        ["--codex-home", str(home)],
        console=console,
        prompts=FakePrompts(str(old), str(new), confirm=False),
        process_checker=lambda: [],
    )

    assert exit_code == 0
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT cwd FROM threads").fetchone()[0] == str(old)
