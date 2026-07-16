# CLI Operations

Set `<cli>` to `clid` when installed, otherwise `python -m cli_downloader` from the repository root.

## Shell owner and Agent submissions

```text
<cli> shell status --json
<cli> shell ensure --visible --wait-ready --json
<cli> download <url> --dry-run --json
<cli> download <url> --detach --json
<cli> tasks show <task-id>
<cli> tasks pause <task-id>
<cli> tasks resume <task-id>
<cli> tasks cancel <task-id>
<cli> tasks retry <task-id>
<cli> tasks hard-retry <task-id>
<cli> batches resume <batch-id>
<cli> batches retry <batch-id>
```

Only use `shell ensure` and `download --detach` after the user authorizes starting a visible
Shell and creating the requested download. A detached response proves durable acceptance only;
query the returned Task ID for progress and terminal state. If the owner disconnects, inspect the
existing Task and History before retrying with the same request ID; never create a replacement Task
because the final event was lost.

When the user closes the visible Shell, it warns about unfinished Tasks, rejects new owner
requests, and persists active checkpoints for recovery. Poll `shell status --json` before
assuming a new owner is available; a missing terminal event is not proof that the download
failed.

## Read tasks

```text
<cli> tasks list --json
<cli> tasks list --status <status> --json
<cli> tasks show <task-id>
```

`<cli> tasks --json` is retained as a shorthand, but prefer the explicit `tasks list` form. Use the status values displayed by `<cli> tasks --help`.

## Read history

```text
<cli> history --json
<cli> history show <task-id> --json
<cli> history --query <keyword> --json
<cli> history --child --json
```

History output represents durable download records. It includes fields such as task ID, kind, plugin/site identity, source URL, status, timestamps, completed and total files, output files, and run generation.

## Add a download

Always plan first:

```text
<cli> download <url> --dry-run --json
```

Then, with user authorization:

```text
<cli> download <url> --json
<cli> download <url> --profile <name> --strategy <auto|direct|torrent> --json
<cli> download <list-url> --json
<cli> download <list-url> --select <item-id> --select <item-id> --json
```

A list or Batch URL without `--select` submits every item returned by that operation. Repeated
`--select` values restrict the Batch to an explicit subset. The interactive Shell continues to
present the plugin-provided selection page instead of applying the command-line default.

Do not use `--force` unless the user intends to create a new attempt despite an active or completed duplicate.

## Inspect plugins safely

```text
<cli> plugins list --root <plugin-root>
<cli> plugins inspect --root <plugin-root> --include-ignored
```

These commands validate packages without importing plugin code. Executing external code requires an explicit action:

```text
<cli> plugins activate --root <plugin-root> --enable-external-code
```

Normal project startup scans the repository `Plugins/` directory and the versioned LocalAppData plugin directory. Arbitrary roots require explicit external-code enablement.

## Verify development changes

For CLI-Downloader core changes, run the narrowest relevant tests first and then the repository verification script:

```text
.\Scripts\Verify.ps1
```

Do not apply this core-project verification requirement to an independent external plugin. Plugin-local unit tests and Ruff are optional and should run only when that plugin provides the corresponding setup or the user requests them.
