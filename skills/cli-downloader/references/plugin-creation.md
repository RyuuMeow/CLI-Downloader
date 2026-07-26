# Plugin Creation Standard

## Discovery checklist

Confirm these facts before choosing a design:

- Canonical site name, domains, and representative item, page, search, and listing URLs.
- Desired operations: single item, individual page, list browsing and selection, batch download, or custom actions.
- Public, account-based, or age-gated access; login URL and proof that a session is truly authenticated.
- Direct asset URLs, signed or expiring URLs, torrent support, pagination, and stable item/asset identifiers.
- Required metadata and folder naming fields.
- Expected request interval, concurrency, retryable status codes, and server-provided cooldown behavior.
- Test fixtures the user is permitted to provide or capture.

Do not request passwords or raw long-lived secrets in chat. Use the application's settings and secret mechanisms.

## URL-first research workflow

Start from a real URL supplied or approved by the user. Gather enough evidence to implement the smallest reliable plugin:

1. Fetch the page and record final URL, status, content type, redirects, canonical URL, and visible item identity.
2. Identify whether data comes from HTML, embedded JSON, XHR/fetch APIs, GraphQL, or a combination. Prefer a stable structured endpoint when its use is permitted.
3. Follow one representative item, one listing or author page, one next-page request, and one asset request when those capabilities are in scope.
4. Record required request headers, referer rules, cookies, CSRF tokens, signatures, pagination parameters, rate-limit headers, and asset expiry behavior without persisting secret values.
5. Compare anonymous and authenticated behavior when login is required. Define a lightweight endpoint or response signal that proves authentication.
6. If tests are worthwhile, save only sanitized, deterministic plugin-local fixtures. Remove account identifiers, cookies, tokens, and signed URLs.
7. Stop researching when every planned route, parser, asset, authentication, pagination, and error path has evidence. Do not crawl the site broadly.

Before implementation, write a compact design table mapping each supported URL to its manifest rule, operation, source endpoint/parser, result kind, strategy, and validation approach. Mark inferred behavior separately from observed behavior.

## Required layout

```text
Plugins/<plugin_id>/
|- plugin.json
|- plugin.py
|- README.md
`- manifests/
   `- site.json
```

`plugin.json` identifies `plugin_id`, display name, semantic version, description, `plugin.py:Plugin` entrypoint, and manifest filename. The manifest file must live below `manifests/`.

## Contract boundaries

The plugin should provide the current repository's equivalents of:

```python
class Plugin:
    site_id = "example"
    display_name = "Example"

    def configure(self, settings, secrets): ...       # optional
    def get_manifest(self): ...
    def can_handle_url(self, url): ...
    def classify_url(self, url): ...
    async def execute(self, request): ...
    async def resolve(self, request): ...
    async def list_assets(self, item): ...
    async def choose_strategy(self, item): ...
    async def fetch(self, asset): ...                 # optional
```

Import and return `cli_downloader.core` contracts. Do not invent parallel result models. Keep URLs and stable logical IDs in metadata; do not construct output paths. Implement `fetch()` only for authenticated, signed, refreshed, or nonstandard asset retrieval. Preserve HTTP status and headers so core retry logic can honor `Retry-After`.

## Manifest design

Declare:

- Exact domains and anchored URL regex rules with named captures.
- One operation per supported behavior and typed, bounded parameters.
- Namespaced settings and secret declarations without secret defaults.
- `browser_auth` for system-managed login when applicable.
- Conservative `network` concurrency, interval, retry statuses, attempts, and maximum backoff.
- Scalar output metadata fields and a relative folder template.

Use stable gallery/item IDs in `ResolvedItem.metadata`. Use stable `asset_id`, `image_id`, `page_id`, or index metadata for retry reconciliation. Never expose secret values through results, logs, exceptions, task metadata, or history.

## Plugin README requirement

Generate `Plugins/<plugin_id>/README.md` after implementation so it describes the actual plugin rather than the initial plan. Write it in the user's preferred language or the language they explicitly request. If no preference is known, ask when language choice matters; otherwise use the language of the user's plugin request. Include:

1. Plugin introduction and supported site.
2. Supported URL formats and whether each produces a single item, page, or selectable batch.
3. CLI and interactive usage examples, including list selection when supported.
4. Authentication steps and content-access limitations without exposing secrets.
5. Every supported setting with full names, types, defaults, ranges or enum values, and effects.
6. Supported download strategies such as `direct`, `torrent`, or `auto`, including prerequisites and fallback behavior.
7. Supported template variables, the default folder template, and useful alternative examples.
8. Network or rate-limit behavior users should understand.
9. Known limitations and troubleshooting for routing, login, empty results, expired assets, and optional external clients.

Derive settings, enums, defaults, output fields, and templates from `manifests/site.json`. Cross-check prose against `plugin.py` and any plugin-local tests. Remove sections for capabilities the plugin does not implement.

## Optional plugin-local quality checks

External plugins do not have to provide unit tests or use Ruff. Decide based on parsing complexity, regression risk, and the plugin's own development conventions.

- Keep any plugin tests inside the plugin repository or plugin package, never in CLI-Downloader's core `tests/` directory.
- Prefer tests for fragile parsers, pagination, authentication proof, asset identity, and strategy selection when the plugin has a test setup.
- Run Ruff only when the plugin declares a Ruff configuration or the user requests it. Do not impose the core project's Ruff policy on an independent plugin.
- Always perform manifest parsing, static plugin inspection, and representative `--dry-run` checks even when no unit-test or Ruff setup exists.

## Validation order

1. Parse JSON and run static plugin inspection without importing code.
2. Run plugin-local tests when they exist and are relevant.
3. Run plugin-local Ruff only when configured or requested.
4. Confirm unsupported URLs fail clearly and safely.
5. Run plugin activation only with approval.
6. Demonstrate `download <url> --dry-run --json`.
7. Mark live authentication and downloads unverified until tested through the user-visible flow.

Do not consider cookie presence alone proof of authentication. When supported by the site, validate with a lightweight authenticated endpoint or account-state signal.
