# Development Plan

Updated: 2026-07-16

This document contains only current architectural boundaries and unfinished roadmap items.
Completed implementation records belong in `CHANGELOG.md` and Git history. The application has
not been released, so compatibility with removed legacy databases, imports, layouts, commands,
and CLI argument forms remains out of scope.

## Active Order

There are no active implementation tasks.

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
