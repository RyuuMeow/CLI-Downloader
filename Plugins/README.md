# CLI-Downloader Plugin Development Guide

`Plugins/` is the development directory for plugins. Each plugin has its own folder. On startup, the system automatically scans and registers plugins here. Workspace plugins with the same ID will override installed versions.

## Directory Structure

```text
Plugins/<plugin_id>/
|- plugin.json
|- plugin.py
|- requirements.txt          # optional runtime dependencies
|- requirements-dev.txt      # optional local test/lint dependencies
|- <plugin_package>/         # optional site-specific implementation package
|- tests/                     # optional plugin-owned tests and fixtures
`- manifests/
   `- site.json
```

A plugin's website implementation, HTTP clients, parsers, fixtures, and tests must be kept inside the plugin directory. The Core framework only handles the universal contracts, loaders, lifecycle, and orchestration. Core tests must not directly import a specific website's parser, adapter, or fixture. If a plugin uses a sibling package, the entrypoint loading will use the plugin's version directory as the import root, allowing the exact same code to run from either the project's `Plugins/` directory or an installed version in LocalAppData.

Plugin tests can be run independently from the project root, for example for `<plugin-id>`:

```powershell
$env:PYTHONPATH = "Plugins/<plugin-id>"
python -m unittest discover -s Plugins/<plugin-id>/tests -t .
```

Tests must mock the network by default; checks requiring a live-site must not be deterministic release gates for Core or plugins.

Officially installed versions are located at:

```text
%LOCALAPPDATA%/CLI-Downloader/plugins/<plugin_id>/<version>/
```

Next to the version directory, there can be a plain text file named `active`, whose content is the active version name. When `active` is missing, the highest version is used.

## plugin.json

```json
{
  "plugin_id": "example",
  "display_name": "Example",
  "version": "0.1.0",
  "description": "Example plugin description.",
  "entrypoint": "plugin.py:Plugin",
  "manifest": "site.json"
}
```

`plugin_id` should use lowercase alphanumeric characters, `-`, or `_`. `description` is used to display a brief introduction in the Shell plugin list. The entrypoint file must be in the plugin root directory, and the Manifest must be in `manifests/`.

Optional `requires_python` and `requires_core_api` use version conditions (e.g., `>=3.11` or `>=0.1.0`). `requirements.txt` only describes runtime dependencies; when the Plugin Manager installs or updates a plugin, these dependencies are placed in the specific version's own `deps/` folder. It will not run pip during plugin discovery/import, nor will it modify the Core environment. `requirements-dev.txt` is strictly for local testing and linting by plugin authors and will not be included in the product installation process.

## Manifest

The Manifest declares domains, URL rules, operation parameters, settings, secret fields, and output tags. The Runtime validates the JSON before loading the Python code.

```json
{
  "site_id": "example",
  "display_name": "Example",
  "domains": ["example.com"],
  "url_rules": [
    {
      "name": "gallery",
      "pattern": "^https://example\\.com/gallery/(?P<id>[a-z0-9]+)$",
      "input_kind": "single",
      "operation": "gallery"
    }
  ],
  "operations": {
    "gallery": {"params": {}}
  },
  "settings": {
    "quality": {
      "type": "string",
      "default": "original",
      "enum": ["original", "preview"]
    }
  },
  "output": {
    "fields": ["author_name", "artwork_id"],
    "default_folder_template": "{author_name}/{artwork_id}-{title}"
  },
  "secrets": {}
}
```

Plugin dependencies should use reproducible version ranges or lock versions to prevent unexpected upstream updates. If hash integrity is required, pip's `--hash` lines can be used in the requirements file. If installation fails, the original active version will be retained, and the reason will be displayed in Runtime diagnostics.

Settings are registered as `plugins.<plugin_id>.<key>`. Secrets are only passed via secret references and should not be placed in results, logs, or error messages.

When a website login is required, prefer registering a system-managed browser login in the Manifest instead of asking the user to copy cookies manually:

```json
"browser_auth": {
  "login_url": "https://accounts.example.com/login",
  "cookie_domains": ["example.com"],
  "required_cookies": ["session_id"]
}
```

Core will create an `auth.session` secret, open an isolated login window, and only persist the Cookie Jar for allowed domains. `required_cookies` only judges if login is complete and does not limit the actual cookies saved. A plugin can obtain the static JSON Session in `configure()` via `secrets["auth.session"]` and safely generate the Cookie Header based on the request URL using `cookie_header_for_url()`.

`output.fields` can only list scalar fields placed by the plugin in `ResolvedItem.metadata`. The system will automatically provide `site_id`, `gallery_id`, `title`, and `task_id`, and register an optional `plugins.<plugin_id>.download.folder_template` override. Plugins must not return actual paths; the core will validate the template, sanitize filenames, and save a snapshot of the valid template and fields to the task.

## Plugin Class

The plugin must provide the following interfaces:

```python
class Plugin:
    site_id = "example"
    display_name = "Example"

    def get_manifest(self): ...
    def can_handle_url(self, url): ...
    def classify_url(self, url): ...

    async def execute(self, request): ...
    async def resolve(self, request): ...
    async def list_assets(self, item): ...
    async def choose_strategy(self, item): ...
```

- `classify_url` returns a core `RouteMatch`, and its content must match the rules in the Manifest.
- `execute` receives a core `OperationRequest` and returns a core `OperationResult`.
- `resolve` converts a confirmed download URL to a `ResolvedItem`.
- `list_assets` returns a complete `AssetList`.
- `choose_strategy` returns a `DownloadStrategyPlan`.
- ListPage, Gallery, Widget, Action, and Enum must uniformly use the universal objects from `cli_downloader.core`.
- Plugins must not create output paths, tasks, or download queues; these are managed by the system.

If a plugin has settings or secret fields, it can choose to add a synchronous configuration entry. `secrets` only contains the actual values within this plugin's namespace and will not enter the OperationResult or logs:

```python
def configure(self, settings, secrets):
    self._timeout = settings["http.timeout_seconds"]
    self._session = secrets.get("session")
```

The Runtime's Batch retry will re-call `resolve` and `choose_strategy`, and compare missing items using stable Gallery/Asset identification data. It is recommended to put `gallery_id` in `ResolvedItem.metadata`, and stable `asset_id`, `image_id`, `page_id`, or `index` in `Asset.metadata`.

## Network Throttling

Plugins with actual network requests should declare safe defaults in `manifests/site.json`:

```json
"network": {
  "max_concurrency": 2,
  "min_interval_seconds": 1.0,
  "retryable_status_codes": [429],
  "max_attempts": 5,
  "max_backoff_seconds": 300.0
}
```

The system will register numeric settings as `plugins.<plugin_id>.network.*` overrides, and share the request gate between resolution and downloading for the same website. Plugins do not need to manually retry 429 errors; retain the original `HTTPError` status and headers so the system can respect `Retry-After` and maintain cooldown states.

## Custom Fetching

General HTTP/HTTPS Assets do not require additional code; the system will download them directly. When Cookies, signatures, URL refreshes, or special protocols are needed, a plugin may optionally implement:

```python
async def fetch(self, asset):
    return b"file content"
```

`fetch` must return `bytes`. Validation, retries, file naming, and writing are still handled by the system.
