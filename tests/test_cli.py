import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from rich.console import Console
from test_apply import read_thread_cwd
from test_delete import thread_count
from test_planner import create_codex_fixture, insert_sibling_session

import codex_move_session.cli as cli
from codex_move_session.cli import PromptAdapter, run


def recording_console() -> tuple[Console, object]:
    console = Console(record=True, width=120, color_system=None)
    return console, console


class FakePrompts(PromptAdapter):
    def __init__(
        self,
        old: str | Path,
        new: str | Path,
        *,
        confirm: bool = False,
        session_id: str | None = "thread-1",
        action: str | None = "move",
    ) -> None:
        self.old = str(old)
        self.new = str(new)
        self.confirmed = confirm
        self.session_id = session_id
        self.action = action

    def choose_scope(self) -> str | None:
        return "all"

    def choose_old(self, groups: list[object]) -> str | None:
        return self.old

    def choose_new(self, old: str) -> str | None:
        return self.new

    def choose_session(self, group: object) -> str | None:
        return self.session_id

    def choose_action(self) -> str | None:
        return self.action

    def confirm_apply(self) -> bool:
        return self.confirmed

    def confirm_delete(self, session: object) -> bool:
        return self.confirmed


@dataclass(frozen=True)
class TwoSessionCliFixture:
    home: Path
    old: Path
    new: Path
    state: Path
    rollout: Path
    global_state: Path


def create_two_session_cli_fixture(tmp_path: Path) -> TwoSessionCliFixture:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    new.mkdir()
    state, rollout = create_codex_fixture(home, old)
    insert_sibling_session(home, state, old, thread_id="thread-2")
    return TwoSessionCliFixture(
        home,
        old,
        new,
        state,
        rollout,
        home / ".codex-global-state.json",
    )


def fixture_bytes(fixture: TwoSessionCliFixture) -> tuple[bytes, bytes, bytes]:
    return (
        fixture.state.read_bytes(),
        fixture.rollout.read_bytes(),
        fixture.global_state.read_bytes(),
    )


def thread_cwd(path: Path, thread_id: str) -> str:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT cwd FROM threads WHERE id = ?", (thread_id,)).fetchone()[0]


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
        process_checker=lambda: [],
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


def test_noninteractive_apply_can_create_missing_destination(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    new = tmp_path / "new-project"
    state, _ = create_codex_fixture(home, old)

    exit_code = run(
        [
            "--old",
            str(old),
            "--new",
            str(new),
            "--create-new",
            "--apply",
            "--codex-home",
            str(home),
        ],
        console=recording_console()[0],
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert new.is_dir()
    assert read_thread_cwd(state) == str(new)


def test_delete_project_applies_to_every_matching_session(tmp_path: Path) -> None:
    fixture = create_two_session_cli_fixture(tmp_path)

    exit_code = run(
        [
            "--delete-project",
            str(fixture.old),
            "--apply",
            "--codex-home",
            str(fixture.home),
        ],
        console=recording_console()[0],
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert thread_count(fixture.state, "thread-1") == 0
    assert thread_count(fixture.state, "thread-2") == 0


def test_doctor_repair_is_dry_run_by_default(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    create_codex_fixture(home, old)
    index = home / "session_index.jsonl"
    console, recorder = recording_console()

    exit_code = run(
        ["--doctor", "--repair", "--codex-home", str(home)],
        console=console,
        process_checker=lambda: [],
    )

    output = recorder.export_text()
    assert exit_code == 0
    assert "Doctor repair plan" in output
    assert "Dry run only" in output
    assert not index.exists()


def test_startup_refuses_chatgpt_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".codex"
    console, recorder = recording_console()

    def unexpected_discovery(_home: Path) -> list[object]:
        raise AssertionError("discovery must not run while ChatGPT is active")

    monkeypatch.setattr(cli, "discover_sessions", unexpected_discovery)

    exit_code = run(
        ["--doctor", "--codex-home", str(home)],
        console=console,
        process_checker=lambda: ["ChatGPT Helper (Renderer)"],
    )

    output = recorder.export_text()
    assert exit_code == 1
    assert "Refusing to run" in output
    assert "ChatGPT Helper (Renderer)" in output
    assert not home.exists()


def test_noninteractive_delete_is_dry_run(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    state, rollout = create_codex_fixture(home, old)
    console, recorder = recording_console()

    exit_code = run(
        ["--delete", "thread-1", "--codex-home", str(home)],
        console=console,
        process_checker=lambda: [],
    )

    output = recorder.export_text()
    assert exit_code == 0
    assert "Delete dry run" in output
    assert "thread-workspace-root-hints[thread-1]" in output
    assert read_thread_cwd(state) == str(old)
    assert rollout.exists()


def test_noninteractive_delete_apply_removes_session(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    state, rollout = create_codex_fixture(home, old)

    exit_code = run(
        ["--delete", "thread-1", "--codex-home", str(home), "--apply"],
        console=recording_console()[0],
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert thread_count(state, "thread-1") == 0
    assert not rollout.exists()


def test_noninteractive_delete_reports_backup_failure_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".codex"
    old = tmp_path / "old-project"
    create_codex_fixture(home, old)
    console, recorder = recording_console()

    def fail_database_backup(source: Path, _destination: Path) -> None:
        raise sqlite3.OperationalError("simulated database backup failure")

    monkeypatch.setattr("codex_move_session.storage._backup_database", fail_database_backup)

    exit_code = run(
        ["--delete", "thread-1", "--codex-home", str(home), "--apply"],
        console=console,
        process_checker=lambda: [],
    )

    output = recorder.export_text()
    assert exit_code == 1
    assert "Apply failed:" in output
    assert "database backup" in output
    assert "Backup:" in output
    assert "Traceback" not in output


def test_parser_rejects_delete_with_move_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        run(["--delete", "thread-1", "--old", "/old", "--new", "/new"])


def test_parser_rejects_empty_delete_session_id(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        run(
            ["--delete", "", "--codex-home", str(tmp_path / ".codex")],
            prompts=FakePrompts("/old", "/new"),
        )


def test_interactive_discovery_error_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console, recorder = recording_console()

    def fail_discovery(home: Path) -> list[object]:
        raise OSError("session database unavailable")

    monkeypatch.setattr(cli, "discover_sessions", fail_discovery)

    exit_code = run(
        ["--codex-home", str(tmp_path / ".codex")],
        console=console,
        prompts=FakePrompts("/old", "/new"),
        process_checker=lambda: [],
    )

    assert exit_code == 1
    assert "Error: session database unavailable" in recorder.export_text()


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


def test_interactive_can_delete_one_selected_session(tmp_path: Path) -> None:
    fixture = create_two_session_cli_fixture(tmp_path)
    prompts = FakePrompts(
        fixture.old,
        fixture.new,
        confirm=True,
        session_id="thread-1",
        action="delete",
    )

    exit_code = run(
        ["--codex-home", str(fixture.home)],
        console=recording_console()[0],
        prompts=prompts,
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert thread_count(fixture.state, "thread-1") == 0
    assert thread_count(fixture.state, "thread-2") == 1


def test_interactive_delete_default_confirmation_leaves_all_data_unchanged(
    tmp_path: Path,
) -> None:
    fixture = create_two_session_cli_fixture(tmp_path)
    original = fixture_bytes(fixture)
    console, recorder = recording_console()

    exit_code = run(
        ["--codex-home", str(fixture.home)],
        console=console,
        prompts=FakePrompts(fixture.old, fixture.new, action="delete"),
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert "Cancelled." in recorder.export_text()
    assert fixture_bytes(fixture) == original


def test_interactive_session_selection_cancel_leaves_all_data_unchanged(
    tmp_path: Path,
) -> None:
    fixture = create_two_session_cli_fixture(tmp_path)
    original = fixture_bytes(fixture)
    console, recorder = recording_console()

    exit_code = run(
        ["--codex-home", str(fixture.home)],
        console=console,
        prompts=FakePrompts(fixture.old, fixture.new, session_id=None),
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert "Cancelled." in recorder.export_text()
    assert fixture_bytes(fixture) == original


def test_interactive_action_selection_cancel_leaves_all_data_unchanged(
    tmp_path: Path,
) -> None:
    fixture = create_two_session_cli_fixture(tmp_path)
    original = fixture_bytes(fixture)
    console, recorder = recording_console()

    exit_code = run(
        ["--codex-home", str(fixture.home)],
        console=console,
        prompts=FakePrompts(fixture.old, fixture.new, action=None),
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert "Cancelled." in recorder.export_text()
    assert fixture_bytes(fixture) == original


def test_interactive_move_changes_only_selected_session(tmp_path: Path) -> None:
    fixture = create_two_session_cli_fixture(tmp_path)
    prompts = FakePrompts(
        fixture.old,
        fixture.new,
        confirm=True,
        session_id="thread-1",
        action="move",
    )

    exit_code = run(
        ["--codex-home", str(fixture.home)],
        console=recording_console()[0],
        prompts=prompts,
        process_checker=lambda: [],
    )

    assert exit_code == 0
    assert thread_cwd(fixture.state, "thread-1") == str(fixture.new)
    assert thread_cwd(fixture.state, "thread-2") == str(fixture.old)
