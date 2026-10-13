# System Structure

Updated: 2026-07-06

## Repository Layout

```text
CLI-Downloader/
|- cli_downloader/
|  |- application/
|  |- cli/
|  |- config/
|  |- core/
|  |- integrations/
|  |- plugins/
|  |- sites/
|  `- storage/
|- Plugins/
|  `- <plugin_id>/
|- docs/
|  |- Index.md
|  |- CLI-Downloader.md
|  |- dev/
|  `- structure/
|- tests/
|- README.md
`- pyproject.toml
```

## Package Responsibilities

| Path | Responsibility |
| --- | --- |
| `cli_downloader/application` | Download orchestration and task lifecycle use cases |
| `cli_downloader/cli` | One-shot commands, shell, terminal renderer adapters, and input handling |
| `cli_downloader/config` | `SettingsRegistry`, core-owned global schemas, namespaced Plugin registration, loading, validation, profiles, and secret references |
| `cli_downloader/core` | Domain models, operation and view contracts, events, errors, and pipeline contracts |
| `cli_downloader/integrations` | Platform actions such as opening URLs, paths, or external tools |
| `cli_downloader/plugins` | Plugin metadata, scanning, activation, validation, and runtime bootstrap helpers |
| `cli_downloader/sites` | Site interfaces, manifest schemas, and the generic registry |
| `cli_downloader/storage` | SQLite stores, output paths, and file persistence |
| `tests` | Offline-first unit and integration tests |

## Documentation Layout

```text
docs/
|- Index.md
|- CLI-Downloader.md
|- dev/
|  |- CodeStyle.md
|  |- HANDOFF.md
|  |- TODO.md
|  |- plan.md
|  |- progress.md
|  |- risk.md
|  |- RFC/
|  `- shell_experience_design.md
`- structure/
   |- SystemDesign.md
   |- SystemStructure.md
   |- ADR/
   `- Analysis-YYYY-MM-DD.md
```

`HANDOFF.md` and RFC proposal files only exist while needed. Accepted decisions
move to ADRs and completed implementation records move to `progress.md`.

## Scheduler Owner And IPC Modules

The single-owner boundary is implemented by the following maintained modules:

| Path | Responsibility |
| --- | --- |
| `cli_downloader/core/owner_scope.py` | Canonical data-root/user identity, OS lease, discovery metadata, and lifecycle status |
| `cli_downloader/core/owner_protocol.py` | Versioned handshake, request/event schemas, wait policy, liveness, and request ledger |
| `cli_downloader/core/owner_transport.py` | Named Pipe/Unix-socket transport, scoped endpoint metadata, bounded connections, and durable response replay |
| `cli_downloader/core/shell_control.py` | `shell status/start/ensure` readiness and visible Shell launch contract |
| `cli_downloader/cli/_shell_wiring.py` | Shell-owned download/task-control dispatch, shutdown boundaries, and external refresh notifications |

`clid download --detach --json` and owner-routed Task controls are public
frontends. Agents must use these commands instead of opening the database,
injecting terminal keystrokes, or starting a second Scheduler.

## Site Package Layout

Core site contracts live under:

```text
cli_downloader/sites/
|- base.py
|- manifest.py
`- registry.py
```

Project Plugins use:

```text
<application-root>/Plugins/<plugin_id>/
|- plugin.json
|- manifests/
|  `- <manifest>.json
`- plugin.py
```

Installed versions use:

```text
%LOCALAPPDATA%/CLI-Downloader/plugins/<plugin_id>/<version>/
```

Project Plugins take precedence over the registered installed version for the
same id. The runtime scans fixed roots only, reports the selected source, and
rejects duplicates within one root. Every site integration lives in an external
Plugin; Core contains no built-in site implementation.
`Plugins/README.md` is the developer-facing package and interface guide.

`Plugins/pixiv` is the first production external Plugin. It owns Pixiv AJAX,
Referer, optional session, artwork, user-list, original-image, and ugoira logic;
the core only receives common Gallery, ListPage, Asset, and settings objects.

## Phase 6 Extension Points

- `cli_downloader/core/batch.py`: implemented batch parent, preparation entry,
  executable package, child, asset, failure, retry, checkpoint, attempt, and
  status-transition contracts plus derived parent counts and status.
- `cli_downloader/storage/state_database.py`: implements unified SQLite Task,
  execution, Completion, and History persistence, including atomic
  package-to-child handoff.
- `cli_downloader/application/batch_pipeline.py`: implemented bounded plugin
  resolution and system download queues, shared global and optional per-site
  concurrency, progress events, immediate persisted handoff, and cooperative
  parent cancellation.
- `cli_downloader/application/download_limiter.py`: provides the process-wide,
  configurable Gallery slot limit shared by standalone and Batch downloads.
- `cli_downloader/application/batch_service.py`: confirms list selections,
  records separate browse history, builds core packages through runtime plugins,
  and executes assets through the system-owned downloader.
- `cli_downloader/application/batch_retry.py`: derives parent retry plans,
  persists attempt history, refreshes packages, reconciles logical assets, and
  returns prepared work to the shared batch pipeline.

- `cli_downloader/core/operations.py`: implemented routing, operation request,
  result, gallery, asset, list, continuation, diagnostic, and serialization
  contracts.
- `cli_downloader/core/widgets.py`: implemented renderer-independent pages,
  widgets, actions, bindings, references, serialization, and bounded tree
  validation.
- `cli_downloader/core/view_session.py`: implemented immutable in-memory focus,
  value, selection, navigation, page, and operation state.
- `cli_downloader/core/browser_auth.py`: validates and filters browser sessions
  and applies HTTP Cookie matching without exposing raw values to renderers.
- `cli_downloader/core/errors.py`: includes structured public contract
  validation errors.
- `cli_downloader/cli/rendering.py`: implemented render-mode selection,
  JSON/plain serialization, and non-interactive interaction requirements.
- `cli_downloader/cli/textual_renderer.py`: provides the reusable core Page
  view plus the standalone compatibility App for keyboard input, session
  updates, selection-change events, action outcomes, pagination, and navigation.
- `cli_downloader/cli/shell.py`: provides the persistent Textual command console,
  command history, one Item/Batch task explorer, shared CLI dispatch, embedded
  plugin operation views, cross-page candidate selection, direct batch handoff,
  structured command-block presentation, gray-orange styling, progressive
  task/file browsing, lifecycle controls, background progress/cancellation,
  copying, redirected-stream fallback, and the planned single-URL quick-download
  normalization at the command boundary.
- `cli_downloader/integrations/browser_login.py`: launches an installed Edge or
  Chrome with a temporary isolated profile and generated local bridge extension,
  then accepts only tokenized localhost session data for Manifest-permitted
  domains.
- `cli_downloader/application/download_service.py`: supports cooperative direct
  download cancellation and asset progress while retaining completed files.
- `cli_downloader/storage/state_batch_store.py`: lists batch parent ids in
  newest-first order for activity inspection through the unified state database.
- `cli_downloader/application/operation_dispatcher.py`: implemented hostname
  routing, plugin classification, manifest validation, invocation, and core
  result enforcement.
- `cli_downloader/sites/base.py`: legacy download adapter and generic operation
  plugin protocols.
- `tests/example_site.py`: supplies the explicitly injected offline Site fixture
  used by Core integration tests without shipping it in the application runtime.
- `cli_downloader/plugins/runtime.py`: implements fixed-root bootstrap,
  external-code policy, source precedence diagnostics, and immutable plugin,
  site, hostname, manifest, settings, and asset-fetcher indexes.

Exact modules are created only when their corresponding Plan task is
implemented.

## Unified Task Boundary

ADR-0008 replaces separate public Task and Batch lifecycles with one task
application API and persisted read model:

- task records identify `item` or `batch` kind and optional parent task;
- Item records expose preparation, package, file, attempt, and checkpoint data;
- Batch records derive progress and status from descendant Items;
- the Shell uses one hierarchical Tasks explorer and one shared action set;
- existing batch pipeline tables may remain internal while the common boundary
  is introduced and verified.

The implemented boundary is split by responsibility:

- `cli_downloader/core/task_tree.py` owns renderer-independent task kinds,
  states, progress, task nodes, and file-detail nodes;
- `cli_downloader/application/task_hierarchy_service.py` projects both stores,
  exposes roots, children, and files, and routes common lifecycle actions;
- `cli_downloader/storage/state_task_store.py` retains authoritative Item and
  Batch task records;
- `cli_downloader/storage/state_batch_store.py` retains internal Batch
  preparation, package, executable child, asset, and attempt records;
- `cli_downloader/application/batch_pipeline.py` applies cooperative control at
  Plugin request and file boundaries;
- `cli_downloader/application/batch_retry.py` can retry a whole Batch or one
  Item from its persisted checkpoints.

Output path construction remains in `cli_downloader/storage`. Plugin Manifests
may register scalar output labels and a default relative template; namespaced
user settings may override that default. Application services persist the
effective template and values on task creation, while Plugins provide source
data but never create paths.
