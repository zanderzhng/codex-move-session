# codex-move-session

`codex-move-session` repairs local Codex sessions after a project directory is renamed or moved.
It supports macOS, Linux, and Windows, discovers both current and legacy Codex session databases,
and repairs absolute paths in session history and generated memories.

> [!WARNING]
> This is an unofficial tool that modifies undocumented Codex data formats. Inspect the dry-run
> carefully, close every Codex process before applying, and retain the generated backup.

## Install

Run without installing:

```console
uvx codex-move-session
```

Install as a persistent uv tool:

```console
uv tool install codex-move-session
codex-move-session --version
```

Before the first PyPI release, run directly from GitHub:

```console
uvx --from git+https://github.com/zanderzhng/codex-move-session codex-move-session
```

Python 3.10 or newer is required.

## Usage

Running without `--old`, `--new`, or `--delete` opens the interactive workflow. It finds session
working directories that no longer exist, lets you filter active or archived sessions, select a
session, and choose whether to move or delete it. The selected action displays every planned data
store change and asks for confirmation before applying it.

```console
codex-move-session
```

For scripts, provide both paths. This is a dry-run and does not write anything:

```console
codex-move-session --old /previous/project --new /current/project
```

Close Codex, review the dry-run, then apply the same migration:

```console
codex-move-session --old /previous/project --new /current/project --apply
```

Delete one local session by ID. Deletion is also a dry-run by default:

```console
codex-move-session --delete SESSION_ID
```

After reviewing the deletion plan, apply it explicitly:

```console
codex-move-session --delete SESSION_ID --apply
```

Deletion removes the local session and related database rows, related memory, and its rollout file
after creating a backup. It never deletes project files.

Include archived sessions or select another Codex profile:

```console
codex-move-session \
  --old /previous/project \
  --new /current/project \
  --include-archived \
  --codex-home ~/.codex-work
```

`CODEX_HOME` is respected when `--codex-home` is not given.

## What Changes

The migration is driven by sessions whose `cwd` equals the old directory or is below it. It can
update:

- Every matching `threads.cwd` and structured sandbox-policy path across compatible databases in
  `CODEX_HOME/sqlite/` and legacy `state_5.sqlite`.
- All exact old-root references in JSON string values inside affected rollout JSONL files,
  including metadata, messages, commands, tool calls, and tool output.
- `raw_memory` and `rollout_summary` for affected thread IDs in Codex memory databases.
- Known workspace roots and thread hints in `.codex-global-state.json` and `cap_sid`.

It does not move project files. The destination must already exist before apply. Prompt history,
logs, caches, and previous backups are not rewritten.

Windows paths use case-insensitive matching and support drive, UNC, extended, and mixed-separator
forms. macOS and Linux matching is case-sensitive. Similar names such as `/project-copy` are not
treated as descendants of `/project`.

## Safety

Dry-run is always the default. Apply mode:

1. Refuses to run while a Codex desktop, CLI, or app-server process is detected.
2. Verifies that files and database values did not change after planning.
3. Creates a timestamped touched-data backup under `CODEX_HOME/backups/`.
4. Uses SQLite transactions and atomic file replacement.
5. Runs post-write database and content verification.
6. Restores every touched store if writing or verification fails.

Each backup contains `manifest.json`, standalone SQLite snapshots, and the original content of every
changed file. Backups can contain private conversations and local paths; do not publish them.

Session deletion uses the same safeguards. `--delete` is a dry-run unless `--apply` is present, and
apply refuses to proceed while Codex is running or if databases or files changed after the preview.
Before deletion, it backs up the affected databases, rollout file, and other changed Codex state.
After writing, it verifies database integrity, confirms the planned rows and rollout file are gone,
and checks the remaining file updates. If deletion or verification fails, it rolls back every
touched store from the backup and reports if any rollback step could not be completed.

## Development

```console
uv sync --all-groups
uv run ruff check .
uv run pytest
uv build
```

Tests construct temporary Codex profiles and never modify the real profile.

## License

MIT
