# CLI-Downloader

## Purpose

CLI-Downloader is a modular command-line download manager rebuilt from zero.
The old `CLI-Eh-Downloader` project is a behavior and fixture reference, not a
codebase or compatibility foundation.

## Goals

- Provide one general CLI and interactive shell for supported download sites.
- Discover site support exclusively through versioned external plugins.
- Keep site-specific parsing and network behavior outside the downloader core.
- Persist download tasks, history, and output state consistently.
- Support both interactive keyboard-driven use and non-interactive JSON use.
- Make plugin contracts testable, versionable, and renderer independent.

## Target Users

- Users who manage image galleries and similar downloadable collections from a
  terminal.
- Plugin developers adding support for additional sites.
- Automation users consuming structured non-interactive output.

## Scope

- Windows-first Python 3.11+ command-line application.
- No site-specific downloader implementation is bundled with Core.
- Project and LocalAppData plugin discovery with explicitly gated activation.
- Planned installation and updates from official or user-approved Git sources.
- Single-item, page, list/search, and eventual batch workflows.
- Direct-download and torrent-strategy planning.
- SQLite task and history persistence.

## Constraints

- External plugins are trusted local Python code and are not sandboxed.
- Only explicit download actions may perform live network requests or write
  files; inspection and dry-run paths remain side-effect free.
- Plugin public requests, results, actions, and view objects are core-defined.
- Interactive behavior must remain usable in a terminal.

## Non-Goals

- Copying or preserving the old project's internal architecture.
- Migrating old configuration or history formats.
- Providing arbitrary plugin-defined GUI widgets.
- Silently downloading, updating, or activating remote plugin code.
- Promising security isolation for activated Python plugins.
