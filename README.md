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

Delete every session whose working directory is a project or one of its descendants:

```console
codex-move-session --delete-project /previous/project
codex-move-session --delete-project /previous/project --apply
```

Limit deletion to one stored copy when an ID exists in both active and archived storage:

```console
codex-move-session --delete SESSION_ID --delete-scope archived
```

The remaining copy is preserved, and its database row is redirected to the surviving rollout.
Project-wide apply creates and verifies a separate backup for each session and stops at the first
failure.

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

Inspect inconsistencies between rollout files, databases, and `session_index.jsonl`:

```console
codex-move-session --doctor
codex-move-session --doctor --repair
codex-move-session --doctor --repair --apply
```

Doctor reports orphaned or missing rollout files and duplicate copies. Safe repairs redirect stale
`threads.rollout_path` values, align archived state, recover blank titles, and create or supplement
`session_index.jsonl`. It does not synthesize an entire database row for an orphaned rollout.

To create a missing destination as part of an applied migration:

```console
codex-move-session \
  --old /previous/project \
  --new /current/project \
  --create-new \
  --apply
```

## What Changes

The migration is driven by sessions whose `cwd` equals the old directory or is below it. It can
update:

- Every matching `threads.cwd` and structured sandbox-policy path across compatible databases in
  `CODEX_HOME/sqlite/` and legacy `state_5.sqlite`.
- All exact old-root references in JSON string values inside affected rollout JSONL files,
  including metadata, messages, commands, tool calls, and tool output.
- `raw_memory` and `rollout_summary` for affected thread IDs in Codex memory databases.
- Known workspace roots and thread hints in `.codex-global-state.json` and `cap_sid`.
- Desktop sidebar project ordering, collapsed workspace groups, and workspace labels.

It does not move project files. The destination must already exist before apply unless
`--create-new` is provided. Prompt history, logs, caches, and previous backups are not rewritten.

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

Session discovery combines compatible SQLite databases with active and archived rollout files. This
exposes rollout-only sessions and recovers missing titles from `session_index.jsonl`, rollout content,
or `history.jsonl`.

Session deletion uses the same safeguards. `--delete` and `--delete-project` are dry-runs unless
`--apply` is present, and
apply refuses to proceed while Codex is running or if the planned database rows or files changed
after the preview. It also rechecks the database set and refuses to delete a rollout shared by
another session. Before deletion, it backs up the affected databases, rollout file, and other changed
Codex state. It also removes the session index entry, thread pins and sidebar ordering when no stored
copy remains. After writing, it verifies database integrity, confirms the planned rows and rollout
file are gone, and checks the remaining file updates. If deletion or verification fails, open
transactions, captured original file content, and scoped original-row restoration automatically
roll back the touched session data. The retained backup remains available for audit or recovery, and
the tool reports if any automatic rollback step could not be completed.

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
