from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import questionary
from questionary import Choice
from rich.console import Console
from rich.table import Table

from . import __version__
from .discovery import SessionScope, StaleGroup, discover_sessions, stale_groups
from .planner import MigrationPlan, build_plan
from .storage import (
    ApplyError,
    ConcurrentChangeError,
    PlanValidationError,
    ProcessRunningError,
    apply_plan,
    running_codex_processes,
)


class PromptAdapter:
    def choose_scope(self) -> SessionScope | None:
        return questionary.select(
            "Sessions to inspect",
            choices=[
                Choice("Active sessions", "active"),
                Choice("Archived sessions", "archived"),
                Choice("All sessions", "all"),
            ],
            default="active",
        ).ask()

    def choose_old(self, groups: list[StaleGroup]) -> str | None:
        choices = [
            Choice(
                f"{group.path}  ({group.count} session{'s' if group.count != 1 else ''})",
                group.path,
            )
            for group in groups
        ]
        return questionary.select("Moved directory", choices=choices).ask()

    def choose_new(self, old: str) -> str | None:
        return questionary.path(
            f"New directory for {old}",
            only_directories=True,
            validate=lambda value: True
            if Path(value).expanduser().is_dir()
            else "Enter an existing directory",
        ).ask()

    def confirm_apply(self) -> bool:
        return bool(questionary.confirm("Apply these changes?", default=False).ask())


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-move-session",
        description="Repair local Codex sessions after moving a project directory.",
    )
    parser.add_argument("--old", help="Previous absolute project directory")
    parser.add_argument("--new", help="New absolute project directory")
    parser.add_argument("--apply", action="store_true", help="Apply the displayed plan")
    parser.add_argument(
        "--include-archived", action="store_true", help="Include archived sessions"
    )
    parser.add_argument(
        "--codex-home", type=Path, default=_default_codex_home(), help="Codex data directory"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def render_plan(plan: MigrationPlan, console: Console, *, applying: bool) -> None:
    console.rule("Apply plan" if applying else "Dry run")
    console.print(f"[bold]Old:[/bold] {plan.old}")
    console.print(f"[bold]New:[/bold] {plan.new}")

    sessions = Table(title=f"Sessions ({len(plan.sessions)})", box=None)
    sessions.add_column("ID")
    sessions.add_column("Title")
    sessions.add_column("State")
    for session in plan.sessions:
        state = "archived" if session.archived else "active"
        sessions.add_row(session.id, session.title or "(untitled)", state)
    console.print(sessions)

    changes = Table(title="Planned modifications", box=None)
    changes.add_column("Area")
    changes.add_column("Location", overflow="fold")
    changes.add_column("Replacements", justify="right")
    for change in plan.database_changes:
        changes.add_row(
            "database",
            f"{change.path.name}: {change.table}.{change.column} [{change.key}]",
            str(change.replacements),
        )
    for change in plan.file_changes:
        changes.add_row(change.area, str(change.path), str(change.replacements))
    console.print(changes)
    console.print(
        f"[bold]{plan.replacement_count}[/bold] path replacement(s) across "
        f"{len(plan.database_changes)} database field(s) and "
        f"{len(plan.file_changes)} file(s)."
    )
    for warning in plan.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    for error in plan.errors:
        console.print(f"[red]Error:[/red] {error}")


def run(
    argv: Sequence[str] | None = None,
    *,
    console: Console | None = None,
    prompts: PromptAdapter | None = None,
    process_checker: Callable[[], list[str]] = running_codex_processes,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    console = console or Console()
    prompts = prompts or PromptAdapter()
    if bool(args.old) != bool(args.new):
        parser.error("--old and --new must be supplied together")

    interactive = not args.old
    scope: SessionScope = "all" if args.include_archived else "active"
    old = args.old
    new = args.new
    if interactive:
        selected_scope = prompts.choose_scope()
        if selected_scope is None:
            console.print("Cancelled.")
            return 0
        scope = selected_scope
        groups = stale_groups(discover_sessions(args.codex_home), scope=scope)
        if not groups:
            console.print("No sessions with missing working directories were found.")
            return 0
        old = prompts.choose_old(groups)
        if old is None:
            console.print("Cancelled.")
            return 0
        new = prompts.choose_new(old)
        if new is None:
            console.print("Cancelled.")
            return 0

    try:
        plan = build_plan(args.codex_home, old, new, scope=scope)
    except (OSError, ValueError) as error:
        console.print(f"[red]Error:[/red] {error}")
        return 1
    render_plan(plan, console, applying=bool(args.apply))
    if plan.errors:
        return 1
    if not plan.has_changes:
        console.print("No changes required.")
        return 0

    should_apply = bool(args.apply)
    if interactive:
        should_apply = prompts.confirm_apply()
    if not should_apply:
        console.print("Dry run only; no data was modified.")
        return 0
    try:
        result = apply_plan(plan, process_checker=process_checker)
    except (PlanValidationError, ConcurrentChangeError, ProcessRunningError, ApplyError) as error:
        console.print(f"[red]Apply failed:[/red] {error}")
        if isinstance(error, ApplyError):
            console.print(f"Backup: {error.backup_dir}")
        return 1
    console.print("[green]Migration applied and verified.[/green]")
    if result.backup_dir:
        console.print(f"Backup: {result.backup_dir}")
    return 0


def main() -> int:
    return run()
