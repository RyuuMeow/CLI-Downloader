# CLI Command Reference

This document provides a comprehensive command-line reference for `CLI-Downloader` (`clid`).

---

## Global Options

Global options apply to the `clid` executable before specifying subcommands.

| Option | Type | Description |
| :--- | :--- | :--- |
| `-h`, `--help` | Flag | Show help message and exit. |
| `--version` | Flag | Display application version. |
| `--plugin-root <path>` | Path | Specify an additional directory path to scan for local plugins (can be specified multiple times). |
| `--enable-external-code` | Flag | Explicitly allow executing code inside external `plugin.py` files. |

---

## 1. Plugin Management (`clid plugins`)

Manage installed local plugins and browse the remote Marketplace catalog.

### Subcommands Overview

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `clid plugins list` | `[--root PATH] [--json]` | List all installed and activated plugins. |
| `clid plugins inspect` | `[--root PATH] [--include-ignored] [--json]` | Inspect plugin manifests without executing plugin code. |
| `clid plugins activate` | `[--root PATH] [--enable-external-code] [--json]` | Validate and activate plugins. |
| `clid plugins remote` | `[--force] [--json]` | View plugins available from the remote Marketplace catalog. |
| `clid plugins refresh` | `[--json]` | Force refresh the remote Marketplace catalog cache. |
| `clid plugins install` | `<target> [--root PATH] [--json]` | Install a plugin from a Git URL or Marketplace catalog ID. |
| `clid plugins update` | `<plugin_id> [--root PATH] [--json]` | Update an installed plugin to its latest version. |
| `clid plugins remove` | `<plugin_id> [--root PATH] [--json]` | Uninstall and remove a plugin. |
| `clid plugins rollback` | `<plugin_id> [--root PATH] [--json]` | Rollback a plugin to its previously installed version. |
| `clid plugins switch` | `<plugin_id> <version> [--root PATH] [--json]` | Switch the active version marker of an installed plugin. |
| `clid plugins versions` | `<plugin_id> [--root PATH] [--json]` | List all installed versions of a specific plugin. |

### Command Options & Examples

#### `clid plugins remote` & `clid plugins refresh`
| Parameter | Type | Description |
| :--- | :--- | :--- |
| `--force` | Flag | Bypass local 6-hour cache and fetch fresh remote marketplace data. |
| `--json` | Flag | Output results in JSON format. |

```bash
# View remote catalog entries
clid plugins remote

# Force refresh marketplace catalog cache
clid plugins refresh
```

#### `clid plugins install`
| Parameter | Type | Description |
| :--- | :--- | :--- |
| `target` | Positional | Catalog plugin ID (e.g. `douyin`, `pixiv`) or Git repository URL. |
| `--root` | Option | Custom plugins installation root directory. |
| `--json` | Flag | Output installation status as JSON. |

```bash
# Install via Marketplace ID (downloads release zip with dependencies if available)
clid plugins install douyin

# Install via Git URL
clid plugins install https://github.com/RyuuMeow/Douyin-Plugin.git
```

---

## 2. Download Execution (`clid download`)

Download galleries, media files, or perform dry-run inspections against supported site URLs.

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `url` | Positional | Target site URL to resolve and download. |
| `--dry-run` | Flag | Resolve URL and print downloadable asset plan without making file changes. |
| `--profile` | Option | Download profile or quality preset name. |
| `--strategy` | Enum (`auto`, `direct`, `torrent`) | Force download strategy (`auto` picks direct download or torrent client). |
| `--select` | Option (Multiple) | Specific asset or item IDs to select for downloading. |
| `--force` | Flag | Bypass duplicate task detection and force creating a new download task. |
| `--json` | Flag | Output task creation payload as JSON. |

### Usage Examples

```bash
# Inspect downloadable items without saving files
clid download "https://pixiv.net/artworks/123456" --dry-run --json

# Execute direct download
clid download "https://pixiv.net/artworks/123456"

# Select specific item IDs from a list URL
clid download "https://example.com/gallery" --select item-1 --select item-2
```

---

## 3. Task Lifecycle & Batch Management (`clid tasks`, `clid batches`)

Manage background download tasks and multi-item batches.

### Task Management (`clid tasks`)

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `clid tasks list` | `[--status STATUS] [--json]` | List existing download tasks (optionally filter by status). |
| `clid tasks show` | `<task_id> [--json]` | Show detailed status and file progress for a specific task. |
| `clid tasks pause` | `<task_id> [--json]` | Pause an active or queued download task. |
| `clid tasks retry` | `<task_id> [--json]` | Retry a failed download task. |
| `clid tasks cancel` | `<task_id> [--json]` | Cancel an active task. |
| `clid tasks delete` | `<task_id> [--json]` | Remove a task record from persistent storage. |

#### Task Options

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `--status` | Option | Filter list by status (`pending`, `downloading`, `paused`, `completed`, `failed`). |
| `--json` | Flag | Output details in JSON format. |

```bash
# List all active or failed tasks as JSON
clid tasks list --status failed --json

# Pause a running task
clid tasks pause task-123456
```

### Batch Management (`clid batches`)

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `clid batches list` | `[--json]` | List multi-item parent batch tasks. |
| `clid batches show` | `<batch_id> [--json]` | View child task hierarchy under a batch task. |

---

## 4. History & Configuration (`clid history`, `clid config`)

### Download History (`clid history`)

Inspect durable records of completed or archived download runs.

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `clid history` | `[--task-id ID] [--query KEYWORD] [--child] [--json]` | List or search historical download records. |
| `clid history show` | `<history_id> [--json]` | Show full details for a history record. |
| `clid history delete` | `<history_id> [--json]` | Delete a history entry. |

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `--task-id` | Option | Filter history entries belonging to a specific task ID. |
| `--query` | Option | Search history by title, URL, or site keyword. |
| `--child` | Flag | Include child item history records. |
| `--json` | Flag | Output in JSON format. |

```bash
# Search history records matching keyword
clid history --query "pixiv" --json
```

### Configuration (`clid config`)

View or update application persistent settings.

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `clid config show` | `[--json]` | Display current configuration settings. |
| `clid config set` | `<key> <value> [--json]` | Update a configuration key. |

---

## 5. Plugin Operations & Sites (`clid operation`, `clid sites`)

### Interactive Plugin Operations (`clid operation`)

Run custom interactive forms, search feeds, or batch selection operations provided by plugins.

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `clid operation list` | `[--plugin-id ID] [--json]` | List interactive operations exposed by installed plugins. |
| `clid operation run` | `<plugin_id> <operation_name> [--json]` | Execute a plugin operation. |

### Supported Sites (`clid sites`)

List site handlers registered by active plugins.

```bash
clid sites --json
```

---

## 6. Diagnostics & App Updates (`clid doctor`, `clid diagnose`, `clid check-update`)

### System & Environment Health (`clid doctor`, `clid diagnose`)

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `clid doctor` | `[--json]` | Run quick system dependency check (Python, Git, paths). |
| `clid diagnose` | `[--json]` | Run deep system, storage, and plugin diagnostic suite. |

### Application Release Update (`clid check-update`)

Check if a new release of `CLI-Downloader` itself is available on GitHub Releases.

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `--json` | Flag | Output version comparison payload as JSON (`status`, `current_version`, `latest_version`). |

```bash
# Check application version status
clid check-update --json
```


## Network diagnostics (`clid network status`)

`clid network status --json` reads the active Shell owner's site request counters,
limits, waiting counts by phase, cooldown deadlines and plugin coverage. It includes
no request URLs, cookies or tokens and makes no external site requests. Without a
ready owner it returns `owner_available: false`; it does not start one or reconstruct
live counters from SQLite. Older owners must be restarted for this capability.

Networked `clid operation --json <plugin_id> <operation_id> [key=value ...]` commands
are forwarded to the active Owner. A live older Owner lacking operation forwarding
is rejected; Core does not execute a second local network operation as a fallback.
Operation help remains offline. If an accepted operation loses its terminal response,
its completion is unknown and Core does not automatically replay it.
