# Development Plan

Updated: 2026-07-15

This document contains active work only. Completed P2, P3, P4, P5-02 through P5-15, and P5-18 tasks
have been removed. The application is not released, so compatibility with removed legacy
databases, imports, layouts, commands, and CLI argument forms remains out of scope.

## Active Order

| Order | Task | Status | Depends on |
| --- | --- | --- | --- |
| 1 | P5-16 Harden IPC Recovery, Security, And Packaging | Completed | P5-14, P5-15 |
| 2 | P5-17 Add The Agent-Control Workflow And End-To-End Verification | In progress | P5-14 through P5-16 |

## Direction And Boundaries

- An executable download slot represents one Item: a standalone Item task or one Batch child.
  Batch parents and individual files inside an Item do not consume additional scheduler slots.
- Core owns scheduling, queue persistence, fairness, retries, control, and task-state projection.
  Plugins classify and resolve site content and may declare site policy metadata, but they do not
  create private worker pools that bypass Core scheduling.
- The Scheduler decides which Item may execute. The Network limiter separately controls request
  concurrency, pacing, retry, and backoff. A torrent client owns its own long-running transfer
  queue after Core submits a torrent successfully.
- User-facing progress keeps file counts, byte counts, and percentages as separate typed values.
  Display code may defensively clamp values, but invalid client metrics must first be normalised at
  the integration boundary before they are persisted.
- Human-readable CLI syntax should use positional resource identifiers after an explicit action.
  Internal task correlation must use application context rather than hidden public CLI arguments.
- `pyproject.toml` remains the single version source. `CHANGELOG.md` owns curated release notes,
  while Git tags trigger verified packaging and publication.
- Exactly one process per user data root may own live download execution. The owner may be the
  interactive Shell or one foreground CLI command, but never both at the same time.
- The interactive Shell is the preferred long-lived Scheduler owner. External `clid` processes
  use a local, current-user-only IPC channel to submit typed commands instead of constructing a
  second in-memory Scheduler.
- IPC changes where a command executes, not its public wait contract. A normal foreground command
  waits for its terminal result; `--detach` returns only after durable acceptance by an active
  Shell owner.
- Waiting through IPC must never mean waiting forever for a dead owner. Connection, handshake,
  acknowledgement, and liveness deadlines are bounded independently from the optional overall
  download timeout.
- Tasks list projections are read models, not full execution graphs. A Batch row must not expand
  all children, assets, attempts, or filesystem state merely to render summary progress.

## Completed P5-14 Forward Download Requests With Wait And Detach Modes

- Added typed `OwnerPipeServer`/`OwnerPipeClient` transport with authenticated handshake, bounded
  connection setup, request/event framing, and scope-derived endpoint metadata. Windows currently
  uses authenticated loopback transport; Named Pipe ACL hardening remains in P5-16.
- Added `clid download --detach` and explicit `--wait`. A ready Shell owns forwarded work; normal
  mode waits for the owner terminal event, while detach returns only after an Item placeholder or
  Batch parent is durably created. Without a Shell, detach is rejected rather than silently
  starting an invisible worker; normal commands retain foreground ownership.
- Forwarded payloads preserve URL, profile, strategy, selection, force, request ID, and response
  mode. Direct Item reservations persist source/title metadata before plugin download work; selected
  Batch acceptance is emitted immediately after its parent and entries are persisted.
- Added acceptance/progress/terminal event handling, owner disconnect deadlines, and explicit
  `owner_disconnected` for list operations without `--select`; no fabricated Task is returned.
- Added protocol, transport, parser, and four-row Item/Batch × wait/detach tests, plus duplicate
  Task-ID assertions. P5-15 will extend the same channel to task controls and richer Shell refresh.

## Completed P5-15 Route Task Control Through The Scheduler Owner

- Added owner `TASK_CONTROL` dispatch for `pause`, `resume`, `cancel`, `retry`, and `hard-retry`,
  plus Batch `resume`/`retry`. External commands keep positional Task IDs; `tasks pause TASK_ID`
  is now available and read-only commands remain local.
- The Shell resolves controls through its shared `TaskHierarchyService`. Item resume and hard retry
  reuse the existing owner-owned CLI flow, while Batch resume/retry reuse the persisted Batch
  pipeline. No replacement Scheduler or download pipeline is created by the client.
- Control responses now contain ordered acceptance, control acknowledgement, persisted revision when
  available, desired control, owner session/PID, and a terminal result. Owner request caching replays
  completed responses for duplicate request IDs without invoking the handler twice.
- Textual Shell registers an external-control refresh callback, so Tasks projections are refreshed
  immediately after an owner acknowledgement while the existing periodic refresh remains recovery.
- Added Item and Batch control tests, parser coverage, owner transport duplicate replay coverage,
  local fallback behavior, and scope-aware owner handshakes. Crash-safe request persistence and
  reconnect hardening are delivered in P5-16.

## Completed P5-16 Harden IPC Recovery, Security, And Packaging

### Status

Completed.

### Scope

- Use Windows Named Pipe as the primary local IPC implementation and a private Unix socket for
  POSIX development. A constrained Windows runner that denies Named Pipe creation may use an
  ephemeral loopback fallback, but it remains bound to 127.0.0.1 and requires the same scoped
  authentication key; normal Windows installs never advertise a TCP endpoint.
- Derive the Named Pipe name from a stable CLI-Downloader namespace, the current Windows user SID,
  a collision-resistant canonical data-root fingerprint, and the IPC protocol generation. Do not
  expose the raw filesystem path in the Pipe or Mutex name.
- Include the complete owner-scope fingerprint in discovery metadata and the authenticated
  handshake. A client that reaches an endpoint for another store must reject it even if endpoint
  discovery data is stale or a shortened name fingerprint were ever to collide.
- Apply payload-size, connection-count, request-timeout, idle-timeout, protocol-version, command,
  and argument validation before dispatch. Redact secrets from owner metadata, diagnostics, crash
  logs, and IPC errors.
- Implement broken-Pipe handling plus heartbeat-based liveness detection for half-open or stalled
  connections. Bound connect, handshake, acknowledgement, and response-idle waits without imposing
  a default timeout on the total duration of a healthy download.
- Make request acknowledgement crash-safe. Persist request identity with the Task/submission so a
  client retry after owner failure returns or recovers the same work instead of duplicating it.
- On owner crash or forced exit, release the OS lock automatically, preserve Task checkpoints, and
  let the next owner reconcile stale `active_run_id`, desired control, direct progress, Batch
  checkpoints, and torrent-client state before becoming ready.
- Define graceful Shell exit while work is active: warn the user, stop accepting new commands,
  persist interruption intent, reach safe boundaries where possible, then release ownership.
- Add isolated multi-process tests using a temporary data root and offline fixture plugins for
  owner contention, status/start readiness, detach, wait, control, disconnect, crash, restart,
  stale metadata, protocol mismatch, broken Pipe, silent owner, and client reconnect.
- Include every required IPC module and launch helper in PyInstaller. Extend Portable smoke tests
  to verify `clid.cmd shell status`, packaged Shell mode, and the owner transport modules are
  included without opening an interactive window in CI.

### Acceptance Criteria

- Another Windows user or unauthenticated client cannot submit commands to the owner endpoint;
  the constrained-runner fallback still requires the scope-derived authentication key.
- Killing the Shell while a foreground `clid` waits always releases that client with a structured
  nonzero result within the liveness deadline. Accepted work remains recoverable, and the client
  never promotes itself or submits a replacement Task automatically.
- Killing the owner during Item, Batch, paused, and torrent work leaves recoverable durable state
  and does not permanently block a new owner.
- No deterministic test requires a real website, external plugin installation, qBittorrent, or a
  visible desktop session.
- Installer, Portable, source, and test runtimes derive compatible owner identity from the intended
  user data root and do not collide across isolated stores.
- Two Portable copies using the same data root deliberately discover one owner; two copies using
  distinct data roots do not share a Pipe, lock, metadata, or Scheduler. An incompatible runtime
  on the same root reports `incompatible_owner` rather than creating a second endpoint.

### Delivered Evidence

- Added complete scope fingerprints to endpoint metadata and both handshake peers. Missing one-sided
  fingerprints are rejected, stale metadata from another data root is rejected, and endpoint
  metadata is atomically replaced with no raw path or secret.
- Added bounded connect, handshake, acceptance, and response-idle waits, a 32-connection cap,
  heartbeat events, broken-connection `owner_disconnected` results, and durable request/event replay
  after an owner restart. A retry with the same request ID cannot invoke the handler twice.
- Added transport, protocol, stale-metadata, silent-owner, owner-restart, and duplicate-request
  tests. PyInstaller explicitly includes the owner protocol, scope, transport, and multiprocessing
  connection modules; Portable smoke verifies `shell status`, Shell mode, per-user plugins, and
  mutable-data isolation.

## P5-17 Add The Agent-Control Workflow And End-To-End Verification

### Status

Planned.

### Agent Skill Contract

- Update the repository Agent Skill to check `clid shell status --json`, run
  `clid shell ensure --visible --wait-ready --json` when a visible Shell is absent, and use
  `clid download <url> --detach --json` only after explicit download authorization.
- Continue to dry-run and inspect URL routing before a live submission. List results still require
  explicit selections; the Agent may not guess a bulk download.
- Read the accepted Task ID, verify it with `tasks show`, and use owner-routed pause, resume, cancel,
  retry, and Batch controls as needed.
- Treat starting a visible Shell, downloading, deleting, plugin activation, and other mutations as
  permission-bearing actions. Status, Task, History, and dry-run inspection remain read-only.
- Document foreground fallback separately: without detach and without a Shell, the current CLI
  blocks as the temporary owner. The Agent workflow should prefer the visible Shell unless the user
  explicitly requests headless foreground execution.

### Documentation And Acceptance

- Update the three README languages, Agent Skill installation/usage guide, CLI help, Shell
  experience design, System Design, System Structure, and changelog with the final command and
  ownership contracts.
- Add one offline end-to-end simulation: no owner, Agent status check, visible-start abstraction,
  ready handshake, detached Item submission, immediate Task visibility, pause/resume, terminal
  completion, durable History, Shell restart, and continued single-owner enforcement.
- Add a selected Batch simulation covering the same submission and control path, including one
  restart during execution and one duplicate request retry.
- Verify that a normal waiting command returns the final exit code both through a Shell owner and
  through a foreground owner, while detach returns an acceptance result and never claims final
  success.

### Acceptance Criteria

- A compatible Agent can operate the currently visible Shell through public JSON commands without
  direct database access, terminal keystroke injection, raw IPC, or a second Scheduler.
- Users see Agent-created Tasks, progress, failures, and controls in the existing Shell pages.
- Starting a second Shell, retrying a request, reconnecting a client, or restarting after a crash
  never creates two active owners or duplicate accepted Tasks.
- The complete isolated Core/plugin verification gate, multi-process IPC suite, Portable smoke,
  build, and documentation checks pass.

## Future Version Backlog

The tasks in this section are intentionally excluded from the active P5 order. They may be
scheduled in a later release after torrent progress, scheduling, CLI, and release automation are
stable.

## P6-01 Add Workflow Provider Plugins And Post-Download Automation

### Status

Deferred to a future version. No P5 task depends on this work.

### Product Direction

- Add post-download automation without turning the first implementation into a general-purpose
  visual programming engine.
- Treat an installed Workflow Plugin as a provider of one or more processing capabilities.
- Treat a Workflow Instance as a user-owned trigger and configuration that selects one provider.
  One installed provider may therefore back multiple instances, such as separate Pixiv and
  gallery-site `ToEpub` rules.
- Keep plugin installation, update, source, and trust management in Plugins. Add a future Workflow
  page for instances, settings, execution status, retry, and output inspection.
- Preserve a path to a later graph-based workflow provider without requiring a node editor in the
  first release.

### Completion And Trigger Boundary

- Dispatch from an immutable, History-independent `ArtifactManifest` or `WorkflowContext`, never
  from a live Task that may be automatically deleted.
- Finalise Completion, History, the artifact manifest, and durable Workflow jobs in one transaction
  or transactional-outbox boundary before deleting an eligible completed Task.
- Include stable completion and run IDs, scope, task kind, plugin and site IDs, title, source URL,
  output directory, typed files, MIME information, and immutable metadata in the context.
- Support declarative filters for provider plugin, site, standalone Item, Batch child, Batch parent,
  terminal result, extension, MIME type, file count, and download strategy.
- Trigger Batch packaging once from the selected Batch scope rather than once per child.
- Deduplicate dispatch by Workflow Instance ID plus Completion identity and generation. Restart
  recovery resumes or retries an existing run instead of creating duplicate output.
- Keep Workflow run status independent from download status.

### Plugin Package And Manifest Contract

- Evolve plugin package metadata from one mandatory Site entrypoint into declared capabilities such
  as `site.adapter` and `workflow.provider`.
- Allow standalone Workflow Plugins with no URL routing and packages that expose both capabilities
  when justified.
- Give each Workflow capability a versioned manifest and entrypoint declaring triggers, accepted
  artifacts, settings, secrets, permissions, outputs, and Core API requirements.
- Expose only declared operations; do not allow arbitrary private Python function calls by name.
- Keep execution behind a typed interface equivalent to
  `run(context, options, workspace, progress, cancellation) -> WorkflowResult`.
- Snapshot provider ID, version, definition, and settings for each run.
- Plan for isolated worker or subprocess execution; a dependency directory is not a security
  sandbox.

### Workflow Instances And Settings

- Store provider metadata separately from user-created Workflow Instances so one provider can back
  multiple differently configured instances.
- Give Core ownership of enabled state, triggers, filters, scope, output location, concurrency,
  retry, and input-retention policy.
- Let provider manifests register typed custom options such as title templates, author mapping,
  cover selection, page direction, or conversion quality.
- Reuse schema-rendered settings and approved Core option sources; ordinary configuration may not
  inject arbitrary UI code.
- Missing, disabled, or incompatible providers leave Instances visible as `Blocked` with actionable
  diagnostics.
- Keep secrets in the existing secret-storage boundary.

### Artifact And File Safety

- Give every run a staging workspace and publish outputs only after provider success and Core
  verification.
- Record generated files as derived artifacts linked to source completion instead of replacing
  History output fields.
- Make source deletion a Core-owned retention policy such as `keep` or
  `delete_after_verified_output`; providers may not delete arbitrary source paths.
- Preserve inputs after failure, cancellation, invalid output, or interrupted publication.
- Define collision, overwrite, atomic publication, cancellation, and rollback behavior before
  enabling rename, move, or delete operations.
- Require declared filesystem, network, external-process, and secret permissions.

### Future Workflow Page

- List Workflow Instances with Name, Provider, Trigger, Filter summary, Enabled, Last Run, and
  Status.
- Add a bounded detail/settings region for provider version, filters, options, permissions, recent
  runs, artifacts, and diagnostics.
- Support create, duplicate, enable, disable, validate, manual run, cancel, and retry.
- Keep installation and removal in Plugins; Workflow manages configured instances only.
- Start with a form and ordered processing summary. A future Graph Workflow provider may consume a
  versioned `nodes + edges` definition using the same execution and permission contracts.

### Acceptance Criteria

- A `ToEpub` provider registers its manifest and settings without Site routing methods.
- Two Workflow Instances use the same provider with different filters and options.
- Auto-deleting a completed Task does not prevent its durable Workflow job from executing.
- Restarting during processing does not duplicate derived artifacts.
- Verified EPUB publication may apply explicit delete-after-verification; every failure preserves
  source images.
- Missing providers, incompatible versions, rejected permissions, failures, and invalid outputs
  remain independently inspectable and retryable.
- Existing download, Completion, History, Task, and plugin discovery behavior remains unchanged
  when no Workflow Instance is enabled.

## Cross-Cutting Verification Gate

Each active P5 task must add focused tests and keep the isolated Core/plugin verification gate
green. Before the P5 series is release-ready, verify:

- qBittorrent metric normalisation, checkpoint recovery, control, and missing-client behavior;
- deterministic Round Robin and FIFO scheduling across standalone Items, Batch children, sites,
  pause/resume, cancellation, retry, restart, and execution leases;
- CLI parser, Shell forwarding, i18n, Doctor JSON, and positional Task/History actions;
- single Scheduler-owner arbitration, local IPC protocol, Shell status/start/ensure, wait/detach
  behavior, external Task control, crash recovery, and Agent-visible Task submission;
- 1000-child Tasks root and Batch-page projections, bounded storage queries, keyed DataTable diffs,
  progress coalescing, stale-refresh rejection, and Textual event-loop responsiveness;
- Changelog extraction, tag/version validation, Release workflow structure, Portable, Installer,
  and checksum asset naming;
- Ruff and compilation for Core, verification scripts, and maintained project plugins;
- isolated Plugin Manager, Shell, Torrent, Scheduler, packaging, and remaining Core suites;
- no live plugin installation, download, release publication, or external-site mutation in
  deterministic CI.
