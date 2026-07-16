from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import questionary
from questionary import Choice
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .delete import DeletionPlan, build_deletion_plan
from .discovery import Session, SessionScope, StaleGroup, discover_sessions, stale_groups
from .planner import MigrationPlan, build_plan
from .storage import (
    ApplyError,
    ConcurrentChangeError,
    PlanValidationError,
    ProcessRunningError,
    apply_deletion,
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
            validate=lambda value: (
                True if Path(value).expanduser().is_dir() else "Enter an existing directory"
            ),
        ).ask()

    def choose_session(self, group: StaleGroup) -> str | None:
        choices = [
            Choice(
                f"{session.title or '(untitled)'}  {session.id[:8]}  "
                f"({'archived' if session.archived else 'active'})",
                session.id,
            )
            for session in group.sessions
        ]
        return questionary.select("Session", choices=choices).ask()

    def choose_action(self) -> str | None:
        return questionary.select(
            "Action",
            choices=[Choice("Move session", "move"), Choice("Delete session", "delete")],
        ).ask()

    def confirm_apply(self) -> bool:
        return bool(questionary.confirm("Apply these changes?", default=False).ask())

    def confirm_delete(self, session: Session) -> bool:
        return bool(
            questionary.confirm(
                f"Delete session '{session.title or session.id}'? "
                "Project files will not be deleted.",
                default=False,
            ).ask()
        )


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
    parser.add_argument("--delete", metavar="SESSION_ID", help="Delete one local Codex session")
    parser.add_argument("--apply", action="store_true", help="Apply the displayed plan")
    parser.add_argument("--include-archived", action="store_true", help="Include archived sessions")
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


def render_deletion_plan(plan: DeletionPlan, console: Console, *, applying: bool) -> None:
    console.rule("Delete plan" if applying else "Delete dry run")

    session_table = Table(title="Session", box=None)
    session_table.add_column("ID")
    session_table.add_column("Title")
    session_table.add_column("Working directory", overflow="fold")
    session_table.add_column("State")
    if plan.session is not None:
        session_table.add_row(
            plan.session.id,
            plan.session.title or "(untitled)",
            plan.session.cwd,
            "archived" if plan.session.archived else "active",
        )
    console.print(session_table)

    actions = Table(title="Planned modifications", box=None)
    actions.add_column("Action")
    actions.add_column("Location", overflow="fold")
    actions.add_column("Rows", justify="right")
    for action in plan.database_actions:
        actions.add_row(
            action.action,
            f"{action.path.name}: {action.table} ({action.where_clause})",
            str(action.row_count),
        )
    for deletion in plan.file_deletions:
        actions.add_row("delete file", str(deletion.path), "")
    for update in plan.file_updates:
        location = f"{update.area}: {update.path}"
        if update.area == "global-state-delete" and plan.session is not None:
            location = f"remove thread-workspace-root-hints[{plan.session.id}]: {update.path}"
        actions.add_row("update file", Text(location), "")
    console.print(actions)
    console.print(
        f"[bold]{plan.deleted_rows}[/bold] row(s) deleted, "
        f"[bold]{plan.cleared_assignments}[/bold] assignment(s) cleared, and "
        f"[bold]{len(plan.file_deletions)}[/bold] rollout file(s) deleted."
    )
    for warning in plan.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    for error in plan.errors:
        console.print(f"[red]Error:[/red] {error}")


def _apply_deletion_plan(
    plan: DeletionPlan,
    console: Console,
    process_checker: Callable[[], list[str]],
) -> int:
    try:
        result = apply_deletion(plan, process_checker=process_checker)
    except (
        PlanValidationError,
        ConcurrentChangeError,
        ProcessRunningError,
        ApplyError,
    ) as error:
        console.print(f"[red]Apply failed:[/red] {error}")
        if isinstance(error, ApplyError):
            console.print(f"Backup: {error.backup_dir}")
        return 1
    console.print("[green]Deletion applied and verified.[/green]")
    console.print(f"Backup: {result.backup_dir}")
    return 0


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
    if args.delete is not None and (args.old or args.new):
        parser.error("--delete cannot be combined with --old or --new")
    if args.delete is not None and not args.delete.strip():
        parser.error("--delete requires a non-empty session ID")
    if bool(args.old) != bool(args.new):
        parser.error("--old and --new must be supplied together")

    if args.delete is not None:
        try:
            deletion_plan = build_deletion_plan(args.codex_home, args.delete)
        except (OSError, ValueError) as error:
            console.print(f"[red]Error:[/red] {error}")
            return 1
        render_deletion_plan(deletion_plan, console, applying=bool(args.apply))
        if deletion_plan.errors:
            return 1
        if not deletion_plan.has_changes:
            console.print("No changes required.")
            return 0
        if not args.apply:
            console.print("Dry run only; no data was modified.")
            return 0
        return _apply_deletion_plan(deletion_plan, console, process_checker)

    interactive = not args.old
    scope: SessionScope = "all" if args.include_archived else "active"
    old = args.old
    new = args.new
    selected_session: Session | None = None
    if interactive:
        selected_scope = prompts.choose_scope()
        if selected_scope is None:
            console.print("Cancelled.")
            return 0
        scope = selected_scope
        try:
            groups = stale_groups(discover_sessions(args.codex_home), scope=scope)
        except (OSError, RuntimeError, ValueError) as error:
            console.print(f"[red]Error:[/red] {error}")
            return 1
        if not groups:
            console.print("No sessions with missing working directories were found.")
            return 0
        old = prompts.choose_old(groups)
        if old is None:
            console.print("Cancelled.")
            return 0
        selected_group = next(group for group in groups if group.path == old)
        session_id = prompts.choose_session(selected_group)
        if session_id is None:
            console.print("Cancelled.")
            return 0
        selected_session = next(
            session for session in selected_group.sessions if session.id == session_id
        )
        selected_action = prompts.choose_action()
        if selected_action is None:
            console.print("Cancelled.")
            return 0
        if selected_action == "delete":
            try:
                deletion_plan = build_deletion_plan(args.codex_home, selected_session.id)
            except (OSError, ValueError) as error:
                console.print(f"[red]Error:[/red] {error}")
                return 1
            render_deletion_plan(deletion_plan, console, applying=True)
            if deletion_plan.errors:
                return 1
            if not deletion_plan.has_changes:
                console.print("No changes required.")
                return 0
            if not prompts.confirm_delete(selected_session):
                console.print("Cancelled.")
                return 0
            return _apply_deletion_plan(deletion_plan, console, process_checker)
        new = prompts.choose_new(old)
        if new is None:
            console.print("Cancelled.")
            return 0

    try:
        plan = build_plan(
            args.codex_home,
            old,
            new,
            scope=scope,
            session_id=selected_session.id if selected_session else None,
        )
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
