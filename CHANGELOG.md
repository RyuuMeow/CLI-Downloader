# Changelog

All notable changes to CLI-Downloader will be documented in this file.

## Unreleased (0.1.4)

### Added

- Shared site HTTP admission across listing, pagination, preparation and asset downloads through versioned plugin HTTP services; exposed effective policy and coverage in Settings and `network status --json`.
- Routed external plugin operations through the live Owner with capability negotiation, preserving one shared network budget across CLI processes.
- Persisted site cooldowns, honored Retry-After across phases and restarts, and counted explicit redirects and safe per-request retries.


### Fixed

- Allowed Batch pause to complete while workers wait for Item admission or a full download queue, releasing pending admission before acknowledging pause.
- Kept Resume available while a Task is Pausing so users can withdraw a pending pause without starting a duplicate worker.

- Closed managed responses before releasing pause capacity, restarted unfinished assets safely on resume, and fenced HTTP starts during Owner shutdown.

- Kept controls attached to each live Batch and reused active workers on resume, including commands received through owner IPC.
- Shared Item admission across worker event loops, initial downloads and retries; removed the Batch two-worker ceiling and aligned round-robin queue display with scheduling preference.
- Released Item and request permits at pause boundaries and prevented canceled request waiters from leaking capacity.
- Paged Tasks roots as well as Batch children, aggregated root progress in bulk, and reused one scheduler snapshot per refresh. Slow refreshes no longer invalidate themselves; returning from details restores the page, and delayed events from hidden tables cannot overwrite file actions.
- Migrated the legacy gallery concurrency setting when no scheduler limit was explicitly configured.

## v0.1.3 - 2026-08-14

### Fixed

- Prevented expired or incomplete Plugin browser sessions from aborting CLI startup while preserving malformed-session and Cookie-domain validation, allowing users to reauthenticate from the Shell settings.

---

## v0.1.2 - 2026-07-28

### Added

- Added `clid plugins refresh` subcommand to force refresh the remote marketplace catalog cache.
- Added support for auto-detecting GitHub Release ZIP assets and unwrapping archive directories during plugin installation.
- Added comprehensive English CLI command reference and updated README documentation.

### Fixed

- Defaulted `clid plugins install` and management commands without `--root` to the per-user AppData directory (`installed_plugins_root()`), leaving `./Plugins` as a read-only dev override layer.
- Fixed Marketplace UI in `clid shell` overwriting status of installed/local development plugins as `Not Installed`.

---

## v0.1.1 - 2026-07-27

### Fixed

- Prevented `AttributeError` crash when invoking `clid plugins` without subcommands; now displays plugin command help.

---

## v0.1.0 - 2026-07-27

### Initial Release

Welcome to the initial release of **CLI-Downloader**, a modular CLI download manager with site adapter support and an interactive terminal interface.

#### Features

- **Core Engine & Storage**
  - SQLite task persistence with atomic state transitions.
  - Multi-level task hierarchy with partial download checkpointing and task recovery.
  - Item-level scheduler with configurable global and per-site concurrency limits.
  - Download rate limiter and byte-level progress reporting.

- **Plugin System**
  - External plugin directory loading and manifest (`site.json`) parsing.
  - Dynamic plugin catalog manager for listing, fetching, and installing plugin suites.

- **Terminal User Interface (TUI)**
  - Interactive terminal shell built with Textual.
  - Multi-tab layout: Tasks Explorer, Plugins Manager, Settings, and Activity Views.
  - Responsive table resizing, byte/MB progress rendering, and task controls.
  - Internationalization support (`en-US`, `zh-CN`, `zh-TW`).

- **CLI & Diagnostics**
  - Unified CLI command structure (`clid download`, `clid tasks`, `clid history`, `clid plugins`).
  - Actionable `clid doctor` system health check.
  - GitHub Release update check integrated into Settings and CLI.

- **Tooling & Packaging**
  - Standalone build specs (`clid.spec`) and Windows installer generation scripts.
  - Agent skill (`skills/cli-downloader`) for AI pair programming context.

---
