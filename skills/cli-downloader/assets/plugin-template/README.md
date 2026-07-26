# Example Plugin

Replace this file after implementation. Use the user's preferred or explicitly requested language. Document only behavior verified by the manifest, code, and available validation evidence.

## Introduction

Describe the supported website and what users can download or browse.

## Supported URLs and operations

| URL format | Result | Operation |
| --- | --- | --- |
| `https://example.com/items/<item_id>` | Single item | `single_download` |

## Usage

```powershell
clid download "https://example.com/items/123" --dry-run --json
clid download "https://example.com/items/123" --json
```

Add interactive browsing and repeated `--select <item-id>` examples only when batch selection is implemented.

## Authentication

Explain the supported login workflow and access limitations. Never include real cookies, tokens, or account data.

## Settings

| Setting | Type | Default | Allowed values | Effect |
| --- | --- | --- | --- | --- |
| `plugins.example.example_setting` | string | `value` | `value`, `other` | Replace with an implemented setting. |

Remove the table when the plugin has no settings.

## Download strategies

Describe implemented `direct`, `torrent`, or `auto` behavior, prerequisites, and fallback rules. Remove this section when only direct downloads are supported.

## Template variables

| Variable | Meaning |
| --- | --- |
| `{item_id}` | Stable website item identifier. |
| `{title}` | Item title. |

Default folder template: `{item_id}-{title}`

Example alternatives:

```text
{item_id}/{title}
```

## Network behavior

Document request pacing, concurrency, retries, and server cooldown handling that affect users.

## Limitations and troubleshooting

List unsupported URL types and practical checks for routing failures, login expiry, empty listings, expired asset URLs, and missing optional clients.
