---
name: cli-downloader
description: Operate the CLI-Downloader project and create external site plugins through URL-first research, implementation, testing, and user documentation. Use when an agent needs to inspect persisted tasks or download history, add a URL download task, diagnose URL routing, analyze a website from representative URLs, or interview a user and implement a documented plugin under the project's Plugins directory.
---

# CLI-Downloader

Use the project's public CLI for normal operation. Modify Python code only when creating or repairing a plugin or when the user explicitly requests a core change.

## Establish the runtime

1. Locate the repository root by finding `pyproject.toml` with project name `cli-downloader`.
2. Prefer `clid` when installed. Otherwise run `python -m cli_downloader` from the repository root.
3. Run `<cli> --help` before relying on an option that may differ from this skill.
4. Use `--json` for agent-readable output whenever the command supports it.
5. Treat inspection and `--dry-run` as read-only. Ask for confirmation before live network access, file downloads, task deletion, history deletion, or external plugin activation unless the user already requested that exact action.
6. Never print authentication cookies, tokens, secret configuration values, or unredacted session data.

## Use the visible Shell owner

The interactive Shell is the single Scheduler owner. Prefer it when an Agent needs a
user-visible task, progress, or control surface:

1. Check ownership first with `<cli> shell status --json`.
2. If no compatible Shell is ready, ask for permission to start one, then run
   `<cli> shell ensure --visible --wait-ready --json`. Do not launch a second Shell or
   construct a private Scheduler.
3. After explicit download authorization and a successful dry run, submit with
   `<cli> download <url> --detach --json`. Detach is accepted only by a ready Shell and
   returns durable acceptance, not final download success.
4. Read the accepted `task_id`, verify it with `<cli> tasks show <task-id>`, and report the
   task to the user before doing further control work.
5. Use `<cli> tasks pause <task-id>`, `<cli> tasks resume <task-id>`,
   `<cli> tasks cancel <task-id>`, `<cli> tasks retry <task-id>`, or
   `<cli> tasks hard-retry <task-id>` for Item controls. Use the corresponding
   `<cli> batches resume|retry <batch-id>` command for Batch parents.
6. Treat `owner_disconnected` as an uncertain completion: keep the known Task ID, inspect
   the Task and History, and never submit a replacement request automatically. A repeated
   request ID is safe to retry because the owner replays the durable response.

Without a Shell, a normal foreground command may temporarily own the Scheduler and wait for
its terminal result. Do not use that fallback when the user expects a visible Shell or when
the Agent must remain responsive; `--detach` without a ready Shell is rejected.

When a visible Shell is closing, it warns about unfinished persisted Tasks, stops accepting
new owner requests, and marks active checkpoints for recovery. Wait for the Shell status to
become absent before starting a new owner; do not submit replacement downloads merely because
the closing Shell did not emit a final event.

Read [references/cli-operations.md](references/cli-operations.md) for exact task, history, download, and plugin-diagnostic commands.

## Inspect tasks and history

- Use `tasks list --json` to inspect current persisted work.
- Use `tasks show <task-id>` when one task needs detail.
- Use `history --json` to inspect durable download records.
- Narrow history with `--task-id`, `--query`, or `--child` instead of parsing the SQLite database directly.
- Do not create state merely to answer a read request. An empty result is valid.
- Summarize identifiers, site, title, status, progress or file counts, and relevant timestamps. Keep tasks (current control/lifecycle) distinct from history (durable download records).

## Add a download task

1. Obtain the exact source URL and any requested profile, strategy, or list selections.
2. Run `download <url> --dry-run --json` to verify plugin routing and planned operation.
3. If no plugin handles the URL, report that result and offer the plugin-creation workflow.
4. If the plan is valid, run the live `download` command only when the user has authorized the download and its expected network and file writes.
5. For list results, inspect candidates first and pass explicit repeated `--select <item-id>` values. Never guess a bulk selection.
6. Read the resulting task ID from output, then verify it with `tasks show <task-id>` or `tasks list --json`.

## Create a website plugin

Read [references/plugin-creation.md](references/plugin-creation.md) before creating or changing a plugin. Read [references/official-plugin-patterns.md](references/official-plugin-patterns.md) when the requested behavior resembles an existing official plugin.

Follow this sequence:

1. Interview the user before implementation. Ask for the website, starting URL, desired item/list/page behavior, authentication, output naming, preferred direct or torrent strategy, and rate-limit constraints. Ask only the unanswered questions, in small groups.
2. Explore representative URLs before designing code. With authorization, fetch the starting page and follow only the minimum useful item, list, pagination, metadata, and asset/API requests. Record request methods, URL shapes, redirects, content types, required headers/cookies, pagination tokens, stable IDs, error responses, and whether asset URLs expire. Prefer browser or HTTP evidence over assumptions.
3. Produce a short evidence-based capability plan: supported URL rules, operations, data source for each operation, authentication proof, asset strategy, settings, secrets, output fields/templates, network policy, and unsupported behavior. Ask the user to resolve choices that materially change scope.
4. Inspect `Plugins/README.md`, core contracts, and the closest official plugin repository linked in [references/official-plugin-patterns.md](references/official-plugin-patterns.md). Fetch the remote reference when it is not present locally. Treat current core contracts as authoritative when an example differs from the installed CLI-Downloader version.
5. Create `Plugins/<plugin_id>/` with `plugin.json`, `plugin.py`, `manifests/site.json`, and `README.md`. Use lowercase letters, digits, hyphens, or underscores for the ID.
6. Keep responsibilities separated: the plugin classifies and resolves site data; core owns task creation, queues, retries, filesystem paths, downloads, and history.
7. Declare settings, secrets, browser authentication, network limits, operations, routes, and output metadata in the manifest. Never embed user secrets.
8. Prefer system-managed browser authentication. Implement site-specific session validation when cookie presence alone does not prove login.
9. Add plugin-local fixtures or unit tests only when their maintenance value justifies them. Never place external plugin tests in the CLI-Downloader core project's `tests/` directory. Mock network calls in any automated plugin tests.
10. Write the plugin `README.md` from the implemented manifest and behavior. Use the user's preferred language, ask when the preference is unknown and material, and follow an explicitly requested language. Include introduction, supported URLs and operations, usage examples, authentication, settings, download strategies, template variables, default and example folder templates, limitations, and troubleshooting. Do not document unimplemented options.
11. Validate statically before executing code and activate only with explicit authorization. Run plugin-local tests or Ruff only when the plugin includes and configures them; neither is mandatory. Do not run or modify the core project's unit tests merely to validate an external plugin.
12. Demonstrate routing with `download <representative-url> --dry-run --json`. Perform a live download only when explicitly authorized.

Use [assets/plugin-template](assets/plugin-template) as a starting layout, including its README structure, not as a substitute for inspecting current contracts. Replace every placeholder and remove unsupported capabilities.

## Report results

State what was read or changed, the exact command or plugin path, validation performed, and any behavior that still requires live-site verification. Distinguish passing offline checks from confirmed user-visible live behavior.
