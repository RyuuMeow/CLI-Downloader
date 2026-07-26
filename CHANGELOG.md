# Changelog

All notable changes to CLI-Downloader will be documented in this file.

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
