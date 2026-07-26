# Handoff

2026-07-11

## Current State

Verification: **605 tests discovered; one pre-existing qBittorrent integration flake
(`WinError 10053`) in the aggregate run.** Isolated torrent (32/32) and plugin-manager (20/20)
reruns pass outside the sandbox; Ruff format/lint, compileall, and wheel build pass.

Branch: `refactor/database-management`

## Recently Completed

### P3-18 Unified Task State And Storage (completed 2026-07-11)

The direct-cutover refactor is complete. Production state now lives in one `state.sqlite3` with
separate logical Task, Execution, Completion, and History ownership. Task deletion removes the
execution graph while preserving History, Completion evidence, and downloaded files.

- TaskRecord is the sole persisted Task lifecycle authority; BatchParent exposes no lifecycle
  status.
- Batch controls are Coordinator-owned with revision/run-generation CAS, terminal race retries,
  and late-control no-op handling.
- Restart recovery releases stale RUNNING Batch leases before retry, and paused resume claims one
  recovery generation while rejecting duplicate concurrent claims.
- Legacy stores, import journals, compatibility migrations, and public per-entry controls are
  removed because the software is unreleased.

### P3-17 Extensions (2026-07-10 session)

Extended the database management refactor with several fixes and features:

- **`tasks hard-retry`** — batch parents now rebuild via `BatchService.confirm_selection()` (keep batch structure); standalone tasks use `plan_task()`. Default output is Rich summary table; `--json` for JSON.
- **`auto_delete_on_complete`** — fixed for standalone tasks (moved delete after bookkeeping so history and result are correct); added batch support in `_run_selected_list_batch()` and `_run_batch_retry()`.
- **`tasks cancel`** — CLI subcommand: positional `TASK_ID`, `--all --yes`, `--json`. Uses existing `TaskHierarchyService.cancel()`.
- **Shell whitelist** — `cancel`/`retry`/`resume` added to `_is_tasks_command()`.
- **Pause persistence** — `_batch_control("pause")` now creates a `TaskRecord(status=PAUSED)` when none exists. `list_roots()` filters out duplicate batch parent records.
- **Recovery fix** — `_recover_interrupted_downloads()` skips paused batches entirely (preserves mid-operation entry/child state for manual resume). TUI resume falls back to `batches retry` when pipeline is not configured.

## Next: H1-08 Split cli/app.py (P2, ⏳)

See `docs/dev/plan.md` for the maintained implementation plan.

`_output.py` is extracted (255 lines), and `_download_ops.py` now owns Batch retry/resume runtime
assembly, shared download wiring, list browse recording, and selected list batch execution.
Remaining extraction: generic list/download flow in `_download_ops.py`, then `_inspect_ops.py` and
`_shell_wiring.py`.

### Future Work (unscheduled)

- External plugin dependency isolation
- Additional renderer backends

## Known Issues

### Plugins Table Column Resize

Same issue as before — column resize on initial tab activation. See shell.py `_resize_plugin_columns()`.

### Tasks Table Formatting

Rich Table overflow with long UUIDs — consider `overflow="ellipsis"`.
