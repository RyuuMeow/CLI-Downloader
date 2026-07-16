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
5. P5-22 Separate Owner Shutdown From User Cancellation
6. P5-23 Recover Checkpoints Without Retry Or Cancellation Semantics
7. P5-24 Gate Owner Readiness And Verify Restart Lifecycles

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

Completed. Investigation confirmed that the reported behavior was an execution-boundary defect,
plus an unsafe cross-thread control path rather than an IPC routing failure:

- Owner IPC calls the same `TaskHierarchyService.pause()` path as the Shell, but returns a
  successful terminal response after persisting pause intent. It does not wait for the Pipeline's
  later safe-boundary acknowledgement.
- Batch controls were also being called directly from Textual/IPC threads, mutating asyncio state
  (`paused_ids`, resume/cancel Events, and pipeline state) outside the owning event loop. This was
  the direct cause of intermittent pause acknowledgement and unreleased queue capacity.

The reproduction suites in `tests/test_pause_contract.py`, `tests/test_batch_execution_runner.py`,
and `tests/test_download_service.py` now cover active-root pause, waiting-root pause, lease release,
stream boundaries, and controls issued from a foreign thread. The remaining direct, torrent,
terminal-race, and applied-control details are tracked by P5-20 and P5-21.

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

- The cross-root tests encode the documented defects and pass once P5-19 admission and lease
  unwinding are active.
- Tests distinguish `Pausing` from `Paused` and never treat persisted intent as resource release.
- The reproduction is independent of Textual timing and exercises the same Pipeline control path
  that Owner dispatch invokes; transport-level control coverage remains in P5-21.

## P5-19 Make Scheduler Admission Pause-Aware

### Status

Completed. Root suspension, pending-waiter eligibility, central Item lease release/reacquisition,
standalone Item admission, and Batch resolution/download semaphore release checkpoints are
implemented and covered by deterministic cross-root tests.

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

### Status

In progress. Batch direct streams now checkpoint each completed asset immediately, wait at chunk
boundaries through the pause-aware control signal, and release both central and pipeline admission
while paused. Successful Batch torrent submission invokes an admission-release callback so Core no
longer occupies an Item slot during client polling. Standalone torrent polling releases its Item
lease after the persisted client handle is known. Standalone completed-output hashes and direct
partial checkpoints are verified; remaining work is completed-asset verification for interrupted
Batch roots and a live-client restart matrix.

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

### Status

Completed for the in-process and fully wired owner paths. `TaskHierarchyService` exposes a bounded
`TaskControlReceipt`, Owner control responses carry requested/applied status, revision, quiescence,
and phase fields, and IPC control handlers report a non-success pending phase instead of claiming
that a pause was complete. Batch pause/resume/cancel mutations are now dispatched to the owning
asyncio loop, eliminating the foreign-thread race that could leave a lease held. Process-level
restart timing remains under P5-22 and P5-24.

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

## P5-22 Separate Owner Shutdown From User Cancellation

### Status

In progress. A typed stop signal now distinguishes `user_cancel` from `owner_shutdown`; Batch and
standalone Item workers can checkpoint to `PLANNED` without emitting cancellation. Shell shutdown
also publishes `STOPPING` and closes the IPC listener before draining. Standalone terminal
boundaries now re-check owner shutdown and increment task revisions for checkpoints. Bounded drain
now rotates the execution fence before the owner lease is released, late non-cooperative workers
are discarded, and qBittorrent shutdown leaves the client task intact. Remaining work is full
process-level restart coverage and repeated shutdown callback coverage:

- Historical cancellation-on-shutdown reproductions are retained in the acceptance tests as
  regression cases; the typed signal now routes owner shutdown through checkpoint paths.
- Fully wired owner readiness, same-request retry after RECOVERING rejection, and paused Batch child
  normalization are covered; OS-process close/reopen timing remains.

### Shutdown Control Model

- Replace the shared boolean cancellation Event with typed execution stop reasons at minimum:
  `user_cancel`, `owner_shutdown`, and `owner_lost`/recovery fencing where required.
- `user_cancel` retains the current terminal cancellation semantics. `owner_shutdown` requests a
  resumable checkpoint and lease release; it must not emit cancellation History or Completion.
- On graceful Shell close, stop accepting new Owner requests, publish `STOPPING` immediately,
  suspend Scheduler roots, and ask active Item and Batch workers to reach the P5-20 checkpoint
  boundary.
- Preserve `desired_control=PAUSE` for already paused Tasks. Running Tasks retain run intent and
  become recoverable without being reported as user-paused or canceled.
- Do not send qBittorrent cancel/delete for owner shutdown. Persist the client handle and last
  observed state for startup reconciliation.

### Bounded Drain And Fencing

- Keep shutdown bounded, but do not release the Owner lease while an old worker can still commit
  an unfenced terminal result. Fence writes by owner session, run ID, generation, and revision.
- If the graceful checkpoint deadline expires, persist an explicit interrupted checkpoint and
  invalidate the old run before process ownership is released.
- Close accepted IPC waits with a typed owner-stopping/disconnected outcome containing the durable
  Task ID and revision. Clients remain bounded and never resubmit a replacement automatically.
- Make repeated shutdown callbacks and late worker completion idempotent.

### Acceptance Criteria

- Closing the Shell never changes a running or paused Task to `CANCELED` unless the user explicitly
  requested cancel first.
- A paused Item or Batch remains durably paused with no automatic startup command.
- A running Item or Batch becomes resumable with its completed checkpoints intact.
- Torrent handles remain in the external client and are reconciled rather than canceled.
- No worker from the old owner can update the Task after a new owner has claimed its data root.

## P5-23 Recover Checkpoints Without Retry Or Cancellation Semantics

### Status

In progress. Startup recovery now uses `resume` instead of `retry`, returns active Batch children
and assets to schedulable queued states without creating retry attempts, skips verified standalone
output files when the checkpoint still exists, clears stale standalone run leases before
re-admission, and preserves recovered Task revision/run-generation identity. Direct streams persist
an unfinished `.part` checkpoint and safely rebuild it on resume; stale owner fences are rotated
before re-admission and late writes are rejected. Batch root recovery is transactional, partial
explicitly partial checkpoints are never promoted to complete assets, and the last observed torrent
state is persisted on owner shutdown. Remaining work is completed-asset verification for
interrupted Batch roots and end-to-end restart reconciliation against a live torrent client:

- Recovery now normalises active Batch children/assets back to queued states and clears stale Task
  run leases for both `RUNNING` and `PLANNED` records before `resume` admission. Parent, graph, and
  asset transitions commit atomically; a failed graph checkpoint rolls back the complete recovery
  transaction. The latest full verification gate before this slice passed with 602 core tests.

### Recovery Matrix

- Normalise Task status, desired control, active run lease, Batch execution rows, and History in one
  transactional recovery operation per root.
- `PAUSED + PAUSE`: clear only the stale owner/run lease, retain checkpoints, and enqueue nothing.
- Interrupted nonterminal `RUN`: clear the stale lease, retain run intent, and enqueue
  `tasks resume` or `batches resume`; do not route through retry.
- Explicitly canceled, failed, partially completed, skipped, and completed roots remain terminal
  and are never auto-resumed.
- Preserve run generation until the recovery worker successfully claims a new generation.

### Item And Batch Checkpoints

- Make Batch resume convert persisted `RESOLVING`, `QUEUED`, and `DOWNLOADING` work into schedulable
  recovery inputs without changing them to failure/cancellation states or creating retry attempts.
- Persist completed Batch asset outcomes incrementally and never enqueue them again. Reconcile an
  unfinished direct asset using the P5-20 partial-file policy.
- Give standalone Items the same typed per-asset checkpoint identity. Recovery skips verified
  completed assets and safely resumes or restarts only the unfinished asset.
- Keep the original Task IDs, Batch child IDs, History records, output ownership markers, and
  Completion identities. Recovery must not create duplicate tasks or files.
- Reconcile standalone and Batch torrent checkpoints by client handle. Completed/missing/external
  states follow the defined torrent safety matrix without resubmitting a known torrent.

### Acceptance Criteria

- A running direct Item and Batch continue after restart without re-downloading verified completed
  assets and without adding retry/cancel History.
- A downloading Batch child is actually scheduled by `batches resume`.
- Paused roots remain paused across any number of restarts and resume only on an explicit command.
- Stale active run IDs are cleared for both Task kinds before recovery admission.
- Torrent recovery reuses the persisted client task and preserves its paused/running state.

## P5-24 Gate Owner Readiness And Verify Restart Lifecycles

### Status

In progress. The premature pre-listener `READY` publication was removed, and shutdown now marks
the owner `STOPPING` before closing IPC admission. Mutation requests remain rejected during
`RECOVERING`; Textual and plain/embedded Shell paths now dispatch tracked startup recovery
commands before publishing `READY`. Same-request retry after a typed RECOVERING rejection is now
safe, and fully wired readiness/DETACH behavior is covered. Remaining work is process-level
WAIT/DETACH restart testing and a deterministic second-restart readiness race test.

### Readiness Audit

- Keep discovery lifecycle at `RECOVERING` while plugins, stores, checkpoint normalisation, and
  recovery admission are being prepared.
- Start the IPC endpoint before advertising an endpoint-capable state. Do not publish `READY`
  before the pipe listener exists and startup recovery commands have been dispatched to tracked
  owner workers (the recovery workers themselves may continue after the prompt is available).
- Define whether read-only queries are available during `RECOVERING`; reject or queue downloads and
  controls with a typed recovering response instead of letting forwarding race a missing pipe.
- External forwarding must not treat discovery metadata alone as proof that the Owner can accept a
  command. `shell ensure --wait-ready` returns only after the same readiness barrier.
- Publish `STOPPING` when shutdown begins, not only after `shell.run()` returns, and reject new
  mutation requests consistently during the drain.

### End-To-End Restart Matrix

- Add process-level or fully wired Shell-owner tests using deterministic fake fetchers and a
  temporary data root. Exercise graceful close and abrupt owner loss separately.
- Cover standalone Item and Batch roots in queued, resolving, downloading, pausing, paused,
  resumed, and torrent-submitted states.
- For each state, assert persisted Task/Batch/asset rows, desired control, active run lease, History,
  Completion, Scheduler admission, startup commands, and final output identity before and after a
  second owner starts.
- Cover an external WAIT client and DETACH client during shutdown. Both remain bounded; an accepted
  Task ID remains authoritative even if its terminal event is lost.
- Test rapid close/reopen, recovery followed by pause/cancel, missing plugin/client, auto-delete on
  completion, and a second restart during recovery.
- Replace state-only recovery expectations that encode `CANCELLED + retry` with the resumable
  checkpoint contract from P5-23.

### Acceptance Criteria

- `READY` means the IPC endpoint is reachable and startup recovery has crossed its defined
  admission barrier.
- Closing and reopening the Shell preserves paused work and resumes running work exactly once.
- Graceful shutdown, crash recovery, and explicit cancel produce three distinguishable durable
  outcomes.
- No restart path duplicates a Task, Batch child, torrent submission, output file, History terminal
  event, or Completion identity.
- The focused lifecycle suites and full verification gate pass without timing sleeps or live
  network dependencies.

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
