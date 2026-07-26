from __future__ import annotations

import json
from urllib.parse import urlparse

from cli_downloader.core.models import (
    Asset,
    AssetKind,
    AssetList,
    DownloadStrategy,
    DownloadStrategyPlan,
    ResolveRequest,
    ResolvedItem,
)
from cli_downloader.core.operations import (
    GalleryData,
    ListItemData,
    ListPageData,
    OperationRequest,
    OperationResult,
    PageInfo,
    ResultKind,
    RouteMatch,
)
from cli_downloader.core.widgets import (
    Action,
    ActionKind,
    Button,
    ListItem,
    ListView,
    Page,
    Pagination,
    SelectionMode,
    Text,
    Title,
)
from cli_downloader.plugins.runtime import RuntimePlugin, build_application_runtime
from cli_downloader.sites.manifest import (
    InputKind,
    OperationSchema,
    ParameterSchema,
    SiteManifest,
    UrlRule,
)


def get_example_manifest() -> SiteManifest:
    return SiteManifest(
        site_id="example",
        display_name="Example Site",
        domains=("example", "example.test"),
        url_rules=(
            UrlRule(
                name="example_item",
                pattern=r"^example://(?P<section>[A-Za-z0-9_-]+)/(?P<slug>[^/?#]+)$",
                input_kind=InputKind.SINGLE,
                operation="single_download",
                description="Offline test item.",
            ),
            UrlRule(
                name="example_gallery",
                pattern=r"^https://example\.test/gallery/(?P<slug>[A-Za-z0-9_-]+)$",
                input_kind=InputKind.SINGLE,
                operation="resolve_gallery",
                description="Offline generic gallery fixture.",
            ),
            UrlRule(
                name="example_list",
                pattern=r"^https://example\.test/list(?:\?page=(?P<page>[12]))?$",
                input_kind=InputKind.PAGE,
                operation="browse_list",
                description="Offline paginated list fixture.",
            ),
        ),
        operations={
            "single_download": OperationSchema(
                name="single_download",
                supported=True,
                requires_live_network_confirmation=False,
                params={
                    "strategy": ParameterSchema(
                        type="string",
                        enum=("auto", "direct"),
                        default="auto",
                        editable=False,
                        description="Test downloads always use generated local files.",
                    ),
                },
            ),
            "resolve_gallery": OperationSchema(
                name="resolve_gallery",
                supported=True,
                requires_live_network_confirmation=False,
                params={},
            ),
            "browse_list": OperationSchema(
                name="browse_list",
                supported=True,
                requires_live_network_confirmation=False,
                params={
                    "page": ParameterSchema(type="integer", min=1, max=2),
                },
            ),
        },
    )


class ExampleAdapter:
    site_id = "example"
    display_name = "Example Site"

    def can_handle_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "example" or parsed.hostname == "example.test"

    def get_manifest(self) -> SiteManifest:
        return get_example_manifest()

    def classify_url(self, url: str) -> RouteMatch | None:
        route = self.get_manifest().route_url(url)
        if route is None or route.operation_schema is None:
            return None
        return RouteMatch(
            plugin_id="example",
            site_id=self.site_id,
            rule_name=route.rule_name,
            operation_id=route.operation,
            normalized_url=url.strip(),
            captures=route.captures,
            parameter_schema={
                name: schema.to_dict() for name, schema in route.operation_schema.params.items()
            },
        )

    async def execute(self, request: OperationRequest) -> OperationResult:
        if request.operation_id == "resolve_gallery":
            return self._gallery_result(request)
        if request.operation_id == "browse_list":
            return self._list_result(request)
        raise ValueError(f"unsupported example operation: {request.operation_id}")

    async def resolve(self, request: ResolveRequest) -> ResolvedItem:
        parsed = urlparse(request.url)
        if parsed.hostname == "example.test":
            slug = parsed.path.removeprefix("/gallery/") or "welcome"
        else:
            slug = "/".join(part for part in (parsed.netloc, parsed.path.strip("/")) if part)
        return ResolvedItem(
            site_id=self.site_id,
            source_url=request.url,
            title=f"Example Item: {slug or 'welcome'}",
            metadata={"slug": slug or "welcome", "profile": request.profile or "default"},
            tags=("example", "offline"),
        )

    async def list_assets(self, item: ResolvedItem) -> AssetList:
        slug = str(item.metadata.get("slug", "welcome"))
        filename_slug = slug.replace("/", "-")
        return AssetList(
            assets=(
                Asset(
                    url=f"https://example.test/assets/{slug}/cover.txt",
                    kind=AssetKind.FILE,
                    filename=f"{filename_slug}-cover.txt",
                    metadata={"role": "cover"},
                ),
                Asset(
                    url=f"https://example.test/assets/{slug}/metadata.json",
                    kind=AssetKind.FILE,
                    filename=f"{filename_slug}-metadata.json",
                    metadata={"role": "metadata"},
                ),
            )
        )

    async def choose_strategy(self, item: ResolvedItem) -> DownloadStrategyPlan:
        assets = await self.list_assets(item)
        return DownloadStrategyPlan(
            strategy=DownloadStrategy.DIRECT,
            reason="example_direct_assets",
            assets=assets,
        )

    def _gallery_result(self, request: OperationRequest) -> OperationResult:
        slug = request.route.captures["slug"]
        return OperationResult(
            request_id=request.request_id,
            result_kind=ResultKind.GALLERY,
            data=GalleryData(
                source_url=request.source_url,
                site_id=self.site_id,
                gallery_id=slug,
                title=f"Example Gallery: {slug}",
                authors=("CLI-Downloader",),
                tags=("example", "offline"),
                expected_asset_count=2,
                metadata={"network_request_performed": False},
            ),
        )

    def _list_result(self, request: OperationRequest) -> OperationResult:
        page_number = int(request.params.get("page", request.route.captures.get("page", 1)))
        slugs = ("welcome", "landscape") if page_number == 1 else ("portrait", "archive")
        items = tuple(
            ListItemData(
                item_id=slug,
                source_url=f"https://example.test/gallery/{slug}",
                title=slug.replace("-", " ").title(),
                summary=f"Offline preview for {slug}",
                tags=("example",),
                metadata={"network_request_performed": False},
            )
            for slug in slugs
        )
        previous_action = (
            Action(
                action_id="previous_page",
                kind=ActionKind.INVOKE_OPERATION,
                operation_id="browse_list",
                params={"page": page_number - 1},
            )
            if page_number > 1
            else None
        )
        next_action = (
            Action(
                action_id="next_page",
                kind=ActionKind.INVOKE_OPERATION,
                operation_id="browse_list",
                params={"page": page_number + 1},
            )
            if page_number < 2
            else None
        )
        return OperationResult(
            request_id=request.request_id,
            result_kind=ResultKind.LIST_PAGE,
            data=ListPageData(
                title="Example Galleries",
                items=items,
                page_info=PageInfo(
                    current_page=page_number,
                    page_size=2,
                    total_items=4,
                    total_pages=2,
                    previous_token=str(page_number - 1) if page_number > 1 else None,
                    next_token=str(page_number + 1) if page_number < 2 else None,
                ),
            ),
            page=Page(
                header=(
                    Title(text="Example Galleries"),
                    Text(text="Select a gallery to download."),
                ),
                content=(
                    ListView(
                        widget_id="results",
                        items=tuple(
                            ListItem(widget_id=item.item_id, title=item.title, text=item.summary)
                            for item in items
                        ),
                        selection_mode=SelectionMode.MULTIPLE,
                    ),
                    Pagination(
                        widget_id="pagination",
                        current_page=page_number,
                        total_pages=2,
                        previous_action=previous_action,
                        next_action=next_action,
                    ),
                ),
                actions=(
                    Button(
                        widget_id="download_selected",
                        text="Download selected",
                        action=Action(
                            action_id="download_selected",
                            kind=ActionKind.DOWNLOAD,
                        ),
                    ),
                ),
            ),
            available_actions=tuple(
                action.action_id for action in (previous_action, next_action) if action is not None
            )
            + ("download_selected",),
        )


class ExampleAssetFetcher:
    async def fetch(self, asset: Asset) -> bytes:
        role = asset.metadata.get("role")
        if role == "metadata":
            return json.dumps(
                {"source": asset.url, "kind": asset.kind.value, "role": role},
                indent=2,
            ).encode("utf-8")
        return f"Example asset generated for {asset.url}\n".encode()


def example_runtime_plugin() -> RuntimePlugin:
    adapter = ExampleAdapter()
    return RuntimePlugin("example", adapter, adapter.get_manifest(), ExampleAssetFetcher())


def build_example_runtime():
    return build_application_runtime(
        plugin_roots=(),
        builtin_plugins=(example_runtime_plugin(),),
    )
