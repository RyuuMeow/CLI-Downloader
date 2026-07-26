# Official Plugin Patterns

Use these remote official plugin repositories as focused examples. Open or clone only the repository relevant to the requested feature, inspect its current manifest and implementation, and do not assume it exists in the user's local CLI-Downloader checkout.

## Pixiv: selectable batch page

Reference: [RyuuMeow/Pixiv-Plugin](https://github.com/RyuuMeow/Pixiv-Plugin)

Inspect the current plugin entrypoint, site manifest, and any list-page implementation in that repository. Search for `ListPageData`, `SelectionMode.MULTIPLE`, pagination, and the selected-download action rather than relying on a fixed file path or version folder.

The user URL is declared as `input_kind: batch`. The operation fetches profile and work IDs, paginates stable artwork IDs, converts them to core `ListItemData`, and returns both:

- `ListPageData` for renderer-independent batch data.
- A core `Page` containing `ListView(selection_mode=SelectionMode.MULTIPLE)`, `Pagination`, and a `Button` with `ActionKind.DOWNLOAD` for `download_selected`.

Keep selection state and download orchestration in core. The plugin supplies item IDs, titles, summaries, pagination metadata, actions, and source URLs. Do not create tasks directly in the plugin.

Also use Pixiv as the reference for:

- Manifest-managed browser login.
- Authenticated JSON requests using a filtered session.
- Multi-page artwork assets and Ugoira packaging.
- Manifest settings with string enums and bounded numeric values.
- Output fields and author/artwork folder templates.

## Direct, torrent, and auto strategy

The operation manifest exposes a `strategy` enum of `auto`, `direct`, and `torrent`. The requested choice is persisted in item metadata. `choose_strategy()` then:

- Builds direct image-page assets for `direct`.
- Retrieves and selects a torrent candidate for explicit `torrent`.
- Chooses torrent in `auto` only when the configured client supports it and candidates exist; otherwise it falls back to direct.

Return a core `DownloadStrategyPlan`; do not call qBittorrent from the plugin. Core owns torrent-client integration and task execution. Preserve stable gallery identity and make missing torrent candidates an explicit, tested error for an explicit torrent request.

## Choosing a pattern

- For author, search, tag, favorites, or collection URLs, begin with the Pixiv selectable-page pattern.
- For multiple delivery mechanisms, use the direct/torrent/auto strategy contract above.
- For login, use Pixiv's system-managed browser session pattern, but implement site-specific validation rather than assuming a required cookie proves login.
- Combine patterns through core contracts. Do not copy site-specific HTTP assumptions, private endpoints, selectors, or cookies into another plugin.
