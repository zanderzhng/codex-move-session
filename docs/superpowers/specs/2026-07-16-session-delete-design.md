# Per-Session Move And Delete Design

## Goal

Let users choose one stale Codex session and either repair its moved project path or safely delete
the session. Both operations remain dry-run first. Deletion follows the useful safety properties in
CodexPlusPlus: discover duplicate local records, back up affected rows and rollout data, delete
related records transactionally, and retain recovery material.

## Scope

This change adds:

- An interactive session selector after the stale-directory selector.
- A per-session `Move` or `Delete` action selector.
- Per-session path migration in the interactive workflow.
- Non-interactive deletion with `--delete SESSION_ID` and optional `--apply`.
- A deletion plan that previews every database row, relationship update, and rollout file affected.
- Full touched-database and touched-file backups, optimistic concurrency checks, verification, and
  automatic rollback for deletion.

This change does not:

- Delete, move, or otherwise modify project source directories.
- Add a manual `--undo` command. Successful deletion backups remain available for future recovery
  tooling.
- Add multi-select or whole-directory deletion.
- Change the existing non-interactive `--old OLD --new NEW` behavior; it continues to migrate all
  matching sessions.

## User Experience

### Interactive

The interactive sequence is:

1. Choose active, archived, or all sessions.
2. Choose a missing working directory.
3. Choose one session from that directory. Each choice shows title, abbreviated ID, and state.
4. Choose `Move session` or `Delete session`.
5. For move, choose an existing destination directory.
6. Display the complete dry-run plan.
7. Confirm execution, defaulting to `No`.

Moving a selected session changes only that session's records and content. Other sessions under the
same old directory remain stale and can be handled on a later run.

Deleting a selected session removes only that session's local Codex data. The confirmation text
names the session and explicitly says that the project directory will not be deleted.

Cancellation at any prompt exits successfully without writes.

### Non-Interactive

Deletion uses:

```console
codex-move-session --delete SESSION_ID
codex-move-session --delete SESSION_ID --apply
```

The first command is a dry run. The second applies the displayed plan. `--delete` is mutually
exclusive with `--old` and `--new`. `--include-archived` does not restrict an explicit session ID;
an exact ID can delete either an active or archived local session.

Existing path migration remains:

```console
codex-move-session --old OLD --new NEW
codex-move-session --old OLD --new NEW --apply
```

## Per-Session Move Semantics

`build_plan` gains an optional exact session-ID filter. Interactive move supplies the selected ID;
non-interactive path migration leaves the filter unset.

For a filtered move, the plan updates:

- Every matching `threads` record for the selected ID across compatible session databases.
- The selected session's sandbox-policy paths.
- Every JSON string in rollout files referenced by the selected session.
- The selected ID's `stage1_outputs.raw_memory` and `rollout_summary` values.
- The selected ID's value in `thread-workspace-root-hints`, when present.

A filtered move does not rewrite project-wide root lists, labels, or `cap_sid`, because those values
are shared with unselected sessions. Unfiltered path migration retains the existing project-wide
rewrite behavior.

## Deletion Plan

A new deletion planner accepts `CODEX_HOME` and an exact session ID and produces an immutable
`DeletionPlan`. The plan contains the selected session metadata, database row deletions, database
field updates, rollout file deletions, warnings, and errors.

The planner inspects all compatible database candidates, not only the preferred record, so a thread
duplicated across legacy and current stores is removed everywhere.

Known related data is handled only when the table and required columns exist:

| Store | Match | Action |
| --- | --- | --- |
| `threads` | `id = SESSION_ID` | Delete row |
| `thread_dynamic_tools` | `thread_id = SESSION_ID` | Delete rows |
| `thread_goals` | `thread_id = SESSION_ID` | Delete rows |
| `thread_spawn_edges` | parent or child ID matches | Delete rows |
| `stage1_outputs` | `thread_id = SESSION_ID` | Delete rows |
| `agent_job_items` | `assigned_thread_id = SESSION_ID` | Set assignment to `NULL` |
| `automation_runs` | `thread_id = SESSION_ID` | Delete rows |
| `inbox_items` | `thread_id = SESSION_ID` | Delete rows |

Memory databases named `memories_*.sqlite` are included when searching for `stage1_outputs`.
Unknown schemas are not modified.

The planner also removes the selected key from `thread-workspace-root-hints` in
`.codex-global-state.json`. It does not rewrite project-level roots or prompt history.

If the session ID is absent from every `threads` table, planning fails with a clear error rather
than treating deletion as successful.

## Rollout File Safety

Every non-empty `rollout_path` from the selected thread's records is considered. A rollout file is
eligible for deletion only when:

- Its resolved path is within `CODEX_HOME/sessions` or `CODEX_HOME/archived_sessions`.
- No non-selected thread record references the same resolved path.
- The path is absolute, is a regular file, is not a symlink, and can be read for backup.

A missing rollout file produces a warning because the database session can still be removed. A path
outside the allowed roots, a shared rollout path, or an unreadable existing file is a plan error and
blocks apply.

The tool rejects rollout symlinks instead of following them.

## Dry-Run Rendering

The deletion preview displays:

- Session ID, title, working directory, and active/archive state.
- Each database, table, action, and affected row count.
- Each rollout file scheduled for deletion.
- Global-state keys scheduled for removal.
- Total database rows deleted, assignments cleared, and files deleted.
- Warnings and blocking errors.

The heading is `Delete plan` during apply and `Delete dry run` otherwise. A dry run performs no
writes and creates no backup.

## Apply And Recovery

Deletion apply uses the same Codex process guard as migration and follows this sequence:

1. Reject a plan with errors or no selected session.
2. Refuse to run while Codex desktop, CLI, or app-server processes are detected.
3. Re-read every planned row and file and reject concurrent changes.
4. Create a timestamped backup under `CODEX_HOME/backups/` containing standalone SQLite snapshots,
   original file bytes, and a manifest with action `delete` and the session ID.
5. Apply all row deletes and assignment clears in transactions, one database at a time.
6. Atomically update global state and remove eligible rollout files.
7. Verify the selected rows are absent, assignments are cleared, global-state keys are absent, and
   rollout files no longer exist.
8. On any failure, restore every touched database and file from the backup, then report whether the
   rollback was complete.

Backups can contain private conversations and remain local. Successful output prints the backup
directory.

## Architecture

Deletion is implemented as a separate planner and executor rather than generalizing migration into
a large operation framework:

- `delete.py` owns deletion plan types and read-only planning.
- `storage.py` retains shared process checks and low-level backup/restore helpers, and exposes a
  deletion executor alongside migration apply.
- `cli.py` owns argument validation, interactive action selection, and plan rendering.
- `planner.py` receives the narrow optional session filter for interactive move.

This keeps the stable migration path recognizable while reusing the proven backup, SQLite, and
rollback mechanisms where their contracts match.

## Error Handling

- Unsupported optional tables are skipped; malformed known tables with missing match columns are
  skipped without mutation.
- SQLite read, backup, transaction, verification, and restore errors are surfaced with the affected
  path.
- A database change after preview raises a concurrency error before destructive writes.
- A file deletion failure triggers restoration of already changed databases and files.
- A rollback failure reports both the original error and every incomplete restore while preserving
  the backup directory for manual recovery.

## Testing

Tests use temporary Codex homes and cover:

- Interactive selection of one session followed by move or delete.
- Cancellation and default-negative confirmation without writes.
- Non-interactive delete dry-run and `--apply`.
- Argument conflicts between `--delete` and `--old`/`--new`.
- Per-session move leaving sibling sessions unchanged.
- Deleting duplicate thread records from multiple session databases.
- Deleting known related and memory rows while clearing, not deleting, assigned jobs.
- Removing the selected global-state hint without changing project-level roots.
- Missing rollout warnings and shared, external, unreadable, or symlinked rollout rejection.
- Codex process refusal and optimistic concurrency refusal.
- Automatic rollback when a database operation or file removal fails.
- Post-delete verification and retained backup manifests.
- Windows-safe SQLite restore behavior.

The full test, lint, build, and GitHub Actions Windows/macOS/Linux matrix remain release gates.
