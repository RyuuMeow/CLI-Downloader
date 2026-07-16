# Development Plan

Updated: 2026-07-16

This document contains only current architectural boundaries and unfinished roadmap items.
Completed implementation records belong in `CHANGELOG.md` and Git history. The application has
not been released, so compatibility with removed legacy databases, imports, layouts, commands,
and CLI argument forms remains out of scope.

## Active Order

1. P5-18 Lock The Pause Contract With Cross-Root Reproductions
2. P5-19 Make Scheduler Admission Pause-Aware
3. P5-20 Add Quiescent Direct And Torrent Checkpoints
4. P5-21 Wait For Applied Controls Across IPC And UI

## Direction And Boundaries

- An executable download slot represents one Item: a standalone Item task or one Batch child.
  Batch parents and individual files inside an Item do not consume additional scheduler slots.
- Core owns scheduling, queue persistence, fairness, retries, control, and task-state projection.
  Plugins classify and resolve site content and may declare site policy metadata, but they do not
  create private worker pools that bypass Core scheduling.
- The Scheduler decides which Item may execute. The Network limiter separately controls request
  concurrency, pacing, retry, and backoff. A torrent client owns its long-running transfer queue
  after Core submits a torrent successfully.
- User-facing progress keeps file counts, byte counts, and percentages as separate typed values.
  Invalid client metrics are normalised at the integration boundary before persistence.
- Human-readable CLI syntax uses positional resource identifiers after an explicit action.
  Internal task correlation uses application context rather than hidden public CLI arguments.
- `pyproject.toml` is the single version source. `CHANGELOG.md` owns curated release notes,
  while Git tags trigger verified packaging and publication.
- Exactly one process per user data root may own live download execution. The owner may be the
  interactive Shell or one foreground CLI command, but never both at the same time.
- The interactive Shell is the preferred long-lived Scheduler owner. External `clid` processes
  use a local, current-user-only IPC channel to submit typed commands.
- IPC changes where a command executes, not its wait contract. A normal command waits for its
  terminal result; `--detach` returns after durable acceptance by an active Shell owner.
- IPC connection, handshake, acknowledgement, and liveness deadlines remain bounded so a dead
  owner cannot leave a client waiting indefinitely.
- Tasks list projections are bounded read models, not full execution graphs. A Batch row does not
  expand all children, assets, attempts, or filesystem state merely to render summary progress.
- Command-line Batch URLs default to all items returned by the operation; repeated `--select`
  values restrict the Batch to a subset. The Shell remains the interactive selection surface.
- Tasks and History remain independent: deleting a Task does not delete its durable History.

## P5-18 Lock The Pause Contract With Cross-Root Reproductions

### Status

Planned. Investigation confirmed that the current behavior is an execution-boundary defect, not
an IPC routing failure:

- Owner IPC calls the same `TaskHierarchyService.pause()` path as the Shell, but returns a
  successful terminal response after persisting pause intent. It does not wait for the Pipeline's
  later safe-boundary acknowledgement.
- A Batch download worker acquires the shared Item Scheduler lease around the whole child. Its
  `wait_if_paused()` calls happen inside that lease and inside Batch download/site semaphores, so a
  worker may be acknowledged as paused while it still consumes capacity.
- A child already waiting for an Item Scheduler lease is not withdrawn or made ineligible when its
  Batch is paused. It may acquire the newly released slot and then wait while holding it.
- Direct streaming checks pause between assets, not between stream chunks. The current asset may
  continue transferring after pause intent until the next asset boundary.
- The current pause tests prove only that a worker blocks at a cooperative boundary. They do not
  assert that another root can start, that active leases are zero, or that an IPC success means the
  task is actually quiescent. All 31 related existing tests pass despite the reported defect.

### Contract And Reproduction Tests

- Define pause request and pause application as distinct phases. Persisted `desired_control=pause`
  with a running status projects as `Pausing`; only the quiescent acknowledgement projects as
  `Paused`.
- Define the quiescent invariant: once a Task is `Paused`, it performs no new plugin resolution,
  HTTP reads, filesystem publication, or child state advancement; Core holds no Item Scheduler,
  download, or site-admission lease for that root; a submitted torrent is paused in its client.
- Add a deterministic two-root test with global concurrency `1`: root A is active, root B is
  queued, pausing A must let B start before A is resumed.
- Add the inverse race: pause a root while it is waiting for a slot and prove it cannot acquire the
  slot after another task releases it.
- Cover pause during resolution, Scheduler acquisition, direct streaming, between assets, torrent
  polling, and immediately before terminal completion.
- Inspect Scheduler active leases in tests through a bounded diagnostic snapshot rather than
  private timing sleeps.

### Acceptance Criteria

- The new cross-root tests fail against the current implementation for the documented reasons.
- Tests distinguish `Pausing` from `Paused` and never treat persisted intent as resource release.
- The reproduction is independent of Textual timing and works through the same Owner control path
  used by external `clid` commands.

## P5-19 Make Scheduler Admission Pause-Aware

### Scheduler Ownership

- Add root-level suspend/resume semantics to the central Item Scheduler. Suspended roots retain
  their deterministic sequence and fairness metadata but are ineligible for new grants.
- Make pause atomically prevent pending waiters from receiving a lease. Resume makes the root
  eligible again without inserting duplicate waiters or moving it ahead of older work.
- Make active workers unwind and release their Item lease at a declared checkpoint before pause is
  acknowledged. Reacquisition after resume goes through the normal FIFO/Round Robin and per-site
  policy; resume does not restore a private slot.
- Make cancellation remove suspended/pending admissions and keep lease release idempotent across
  pause/resume/cancel races.
- Use the same suspension model for standalone Items and Batch children so one execution path
  cannot bypass the other.

### Remove Duplicate Admission Ownership

- Keep Batch worker counts and bounded queues as pipeline backpressure, not as a second download
  scheduler.
- Remove or narrow Batch download/site semaphores that duplicate the central global/per-site Item
  policy. No worker may block on pause while holding a legacy admission semaphore.
- Keep resolution concurrency separate from Item download concurrency, but stop admitting new
  resolution work for a suspended root and never wait for resume while holding a resolution permit.
- Expose active and pending Scheduler snapshots by root so Tasks diagnostics and tests can prove
  capacity release.

### Acceptance Criteria

- With one global slot, pausing the active root grants the slot to another eligible root.
- Paused roots consume zero Item admission capacity and cannot acquire a site slot in the
  background.
- Resume preserves Batch child order and the selected FIFO/Round Robin fairness policy.
- Repeated pause/resume and pause/cancel races leak no waiters, leases, or queue capacity.

## P5-20 Add Quiescent Direct And Torrent Checkpoints

### Direct Downloads

- Change the Batch executor contract from one opaque whole-child call to checkpointed asset
  outcomes so completed assets are persisted before an unfinished child yields its lease.
- Check pause during streamed reads as well as between assets. Close the active response and writer
  at a controlled boundary, persist an unfinished checkpoint, unwind admission leases, and only
  then acknowledge pause.
- Define partial-file integrity explicitly. Range-capable fetchers may resume from a verified
  offset; unsupported fetchers restart the unfinished asset safely. A partial file and its own
  prefix hash must never be promoted as if it were a complete asset.
- Keep completed assets completed across resume and do not duplicate output paths, History rows,
  progress, or Completion identities.
- Apply the same checkpoint/lease behavior to standalone Item downloads instead of retaining an
  Item lease while `_wait_until_runnable()` polls a paused Task.

### Torrent Downloads

- Treat successful torrent submission as the end of Core Item admission. qBittorrent owns the
  long-running transfer slot; Core polling must not retain a download Scheduler lease.
- On Batch or Item pause, send the client pause request and wait for a bounded observed paused state
  before acknowledging Core pause. Resume and cancel use the persisted torrent handle.
- Preserve the existing safety rule for a missing client task, while distinguishing an externally
  completed torrent from a lost pre-submission checkpoint.

### Acceptance Criteria

- After pause acknowledgement, direct byte progress and filesystem publication remain unchanged
  until resume while another root can use the released slot.
- Resuming never finalises a truncated `.part` file and never repeats already completed assets.
- Torrent polling does not consume Core Item concurrency after submission; pause/resume/cancel are
  mirrored to the client and survive restart reconciliation.

## P5-21 Wait For Applied Controls Across IPC And UI

### Control Result Model

- Return a typed control receipt with separate `requested`, `applied`, `actual_status`,
  `desired_control`, revision, and quiescence fields.
- Keep the first IPC acceptance/control event fast, but do not emit a successful terminal pause
  result until the Coordinator receives the resource-safe Pipeline acknowledgement.
- Bound the applied-control wait. Owner crash, worker failure, or timeout returns the last durable
  Task ID/revision and an explicit pending or failed phase; an external `clid` process must never
  block indefinitely or silently submit a replacement command.
- Make resume wait until the root is eligible/requeued and cancel wait until active admission and
  external-client controls have reached their defined boundary.

### Projection And Verification

- Show `Pausing` while intent is pending and `Paused` only after quiescence in Tasks, JSON, Shell,
  and IPC responses. Refresh the Tasks page on both requested and applied transitions.
- Add Owner integration coverage for Shell-local and external CLI controls, including disconnect,
  timeout, rapid pause/resume, and terminal-completion races.
- Run Batch Pipeline, Scheduler, direct/torrent, Task state, recovery, Owner protocol/forwarding,
  Shell projection, and the full repository verification gate.

### Acceptance Criteria

- When external `clid ... pause` reports success, the Task is durably `Paused`, its root owns no
  Core admission lease, and another eligible Task has been allowed to run.
- An in-progress pause is reported as `Pausing`, never as a completed pause.
- IPC loss cannot leave the caller waiting forever, duplicate a control request, or change the
  existing Task/History separation.

## Future Version Backlog

The following work is intentionally deferred and has no dependency from current work.

## P6-01 Add Workflow Provider Plugins And Post-Download Automation

### Status

Deferred to a future version.

### Product Direction

- Add post-download automation without turning the first implementation into a general-purpose
  visual programming engine.
- Treat an installed Workflow Plugin as a provider of one or more processing capabilities.
- Treat a Workflow Instance as a user-owned trigger and configuration that selects one provider.
  One provider may back multiple instances, such as separate Pixiv and gallery-site `ToEpub`
  rules.
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
- Missing, disabled, or incompatible providers leave Instances visible as `Blocked` with
  actionable diagnostics.
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
