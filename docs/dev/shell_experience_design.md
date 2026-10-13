# Shell Experience Design

Updated: 2026-07-15

This document is the authoritative interaction and visual specification for the
persistent Textual Shell. Core widget and operation contracts are recorded in
the system design and accepted ADRs.

## Experience Goals

- The current command must be immediately visible.
- A command, its output, status, and elapsed time form one visual block.
- Information uses headings, spacing, tables, and indentation to expose
  hierarchy instead of presenting one stream of equal-weight text.
- Interactive errors use plain language. JSON is reserved for explicit
  `--json`, redirected streams, and automation.
- Output supports mouse selection and clipboard copying.
- Internal storage terms never leak into user-facing Batch navigation.

## URL Input

Exactly one absolute HTTP(S) URL submitted in the command field is normalized
to `download <url>`. This includes text inserted by terminal drag-and-drop, but
no classification, network request, or file write occurs before Enter. The
normalized command is the single history and Console Block entry and continues
through the existing Plugin routing, confirmation, queue, and Batch UI.

Commands, URLs with surrounding arguments, local paths, unsupported schemes,
and malformed input are not rewritten.

## External Agent Commands

The visible Shell is the task dashboard and the sole long-lived Scheduler owner.
An Agent first reads `clid shell status --json`; after authorization it may use
`clid shell ensure --visible --wait-ready --json` and submit a validated
`clid download <url> --detach --json`. The Console and Tasks tab then show the
same accepted Task as a user-submitted download. Detach returns acceptance only,
so the Agent must query `tasks show` and leave the final status visible in the
existing Tasks and History views.

Pause, resume, cancel, retry, and Batch controls are owner-routed commands. If
the IPC owner disappears, the Console reports an uncertain completion with the
known Task ID; the UI must not silently create another Task. A normal command
without a Shell remains a foreground owner fallback and may block until its
terminal result, while `--detach` without a ready Shell is rejected.

When the user exits while persisted work is unfinished, the Shell shows a warning,
marks active run checkpoints as recoverable planned work, signals in-flight workers
to stop at their next safe boundary, and stops accepting new owner requests before
releasing the ownership lock. A later Shell startup reconciles those checkpoints;
it never creates a replacement Task merely because the previous Shell closed.

## Console Blocks

Each submitted command creates one full-width gray block. Its orange command
header remains visible while the body changes from running to a terminal result.
Every row is padded to the same width, so multiline output reads as one region
instead of separate highlighted fragments. Background progress and completion
update the originating block rather than appending an unrelated line at the
bottom.

Blocks use a restrained terminal-safe symbol vocabulary:

| Meaning | Symbol |
| --- | --- |
| Command | `>` |
| Completed | `+` |
| Failed | `x` |
| Warning | `!` |
| Running | `...` |
| Hierarchy | `|` and `+-` |

Unicode symbols may replace these when terminal capability is verified, but
meaning must not depend on glyphs or color alone.

Known structured results should render as titled sections, key/value groups, or
tables. Raw output remains readable inside the same block. Shell parse, policy,
and unsupported-command errors are always human-readable and suggest a valid
next action where one exists.

`clear` and `cls` remove visible Console blocks only. They do not delete command
history, tasks, batches, files, or database records.

The implemented Console stores process-local `ShellCommandBlock` values and
re-renders the selectable RichLog with aligned rows when a block changes.
Background work retains its originating block reference, so later input cannot
redirect progress or completion. JSON objects and lists are presented as
headings, key/value groups, numbered lists, or ASCII tables while explicit JSON
and redirected execution remain unchanged.

## Palette

The Shell uses a dark neutral gray base with orange as the interaction accent.
Semantic success and failure colors remain distinct so the interface is not a
single-hue theme.

| Role | Color |
| --- | --- |
| Background | `#171717` |
| Surface | `#222222` |
| Divider | `#3a3a3a` |
| Accent | `#e58a32` |
| Focus and hover | `#ffaa55` |
| Primary text | `#e8e8e8` |
| Muted text | `#999999` |
| Success | `#67b279` |
| Error | `#d96c6c` |

Focus, status, and hierarchy must remain understandable in monochrome terminals.

## Selection And Copying

Console text must allow drag selection through Textual's selection support and
copy selected text with the platform-supported binding. Each command block also
provides a copy-whole-block action as a fallback. Copy actions must not trigger
operation cancellation, and selection must not interfere with scrolling.

The implemented RichLog uses Textual's native selection and `Ctrl+C` behavior.
The Copy latest control and hidden `Ctrl+Shift+C` binding copy the complete most
recent block through Textual's clipboard API.

## Cross-Page List Selection

Selection is a session-level basket of core `ListItemData`, keyed by stable item
identity. Updating one page replaces selection only for items visible on that
page. Items selected on other pages remain in the basket, and the Operation view
shows the total selected count.

Selectable rows use the color-independent `[ ]` and `[ x ]` markers. List item
numbers are dimmer than their content. The Operation toolbar occupies one row
and list content expands through the remaining pane height.

Confirmation passes the selected candidate data directly to the batch
application service. It does not re-dispatch the current list URL with item ids,
because the current page cannot reconstruct candidates selected on earlier
pages. Browsing and selection still create no persisted task before confirmation.

## Unified Task Explorer

The Shell uses one Tasks tab rather than separate Tasks and Batches tabs. Its
top level contains standalone `⬇ Item` downloads and `📦 Batch` containers.
Semantic text remains visible beside colored symbols so status does not depend
on emoji rendering or color alone.

The view uses progressive levels:

1. Tasks: kind, display name, source site, file progress, status, and creation time.
2. Batch contents: Item tasks included in the selected Batch.
3. Item details: files, completion counts, status, and actionable failure text.

The header presents a title and breadcrumb such as `Tasks / Batch / Item`.
The toolbar opens the selected source URL or download folder and exposes only
valid pause, resume, cancel, retry, and record-deletion actions. Batch actions
cascade to descendants. Record deletion never deletes downloaded files.
Detail rows use short ASCII labels such as `[F]`, `[S]`, `[L]`, and `[R]` so
file, status, location, and retry information remain distinct without relying
on color.

Batch display names are immutable creation-time snapshots. They prefer the
Plugin's common list title, such as an author or search title, and fall back to
a short system parent id. The type icon already identifies a Batch, so the name
does not receive a forced `Batch` suffix and has no rename workflow. Labels such
as Parent, Entry, Child, and Asset remain implementation terms and are not shown
to users.

Enter moves deeper, while Back or Escape returns one level. Before Plugin
enumeration determines the total, an Item shows Preparing; afterward progress
is completed files over total files. Batch progress aggregates descendant Item
and file counts and refreshes while work is active. Failure details translate
ownership to Site extension or Downloader and explain the failed stage in
user-facing language.

The active Tasks level refreshes every two seconds while mutable work or a Shell
background job exists, preserving the selected row. Terminal task views stay
idle until a command, control, navigation, or manual refresh changes them. Fixed
operational columns keep stable widths; Name receives the remaining terminal
width and ellipsizes first when the Shell narrows. The full title stays visible
in Task details.

The implemented toolbar uses compact familiar symbols with hover tooltips and
hides actions that do not apply to the selected state. The Shell receives only
`TaskNode` and `TaskFileNode` projections; Batch entries, executable children,
and asset records remain behind `TaskHierarchyService`.

## Settings Explorer

The Settings tab is a hierarchical editor with a title and breadcrumb, such as
`Settings / Plugins / Example Plugin / Network`. Root sections are General, Download,
Network, and Plugins. Core schemas and enabled Plugin schemas drive visible
rows and editability; secrets are masked and support set or clear actions only.
Saving writes validated config and secrets separately, and the changed values
apply when a new task builds its config snapshot.

Enum-backed fields use a Select control so values such as download Strategy
cannot be mistyped. Other scalar and secret types retain their dedicated input
and validation behavior.

Changes persist to the appropriate config or secret service and apply to new
tasks. Active tasks retain the validated settings snapshot captured when they
were created.

## Download Result Summary

Interactive Console completion output shows only user-facing fields: result,
title, file progress, output folder, and elapsed time. Nested internal records,
diagnostics, package data, and raw metadata stay out of the default Shell.
Automation retains complete structured output through explicit JSON mode.
Batch summaries add completed Items against the total, dry runs identify the
matched site and input type, and common errors render as one plain-language
message without internal exception names.

`help` uses a Command, Use, and Example table. `tasks` uses a compact table with
type, display name, progress, status, and creation time; `tasks show <id>` owns
source, location, file statistics, and failure details. Platform values,
pipeline containers, and internal metadata remain available only in JSON or
diagnostic output. Errors show the reason, stage, retryability, and suggested
action without recursively rendering nested result objects.

Console commands cover record operations that are inefficient in the visual
explorer. `tasks delete` accepts explicit ids or bounded status/age filters,
supports dry-run, requires confirmation for a real bulk change, and always
preserves downloaded files.

## Deferred

- User-selectable themes.
- Localization.
- Persistent Console block history across process restarts.
- Mouse-first navigation or a desktop GUI.
