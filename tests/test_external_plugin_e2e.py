import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cli_downloader.application.batch_service import RuntimePackageResolver
from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.cli.app import main
from cli_downloader.cli.textual_renderer import CorePageApp
from cli_downloader.core.batch import BatchEntry, FailureStage, RetryPlan, RetryScope
from cli_downloader.core.operations import ListPageData, ResultKind
from cli_downloader.core.view_session import ViewSession
from cli_downloader.core.widgets import ActionKind
from cli_downloader.plugins.runtime import build_application_runtime


class ExternalPluginEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_plugin_downloads_retries_and_registers_settings(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_download_plugin(root / "app" / "Plugins" / "downloadable")
            runtime = build_application_runtime(
                application_root=root / "app",
                local_app_data=root / "local",
            )

            result = await OperationDispatcher(runtime).dispatch(
                "https://downloadable.test/gallery/one",
                request_id="request-1",
            )
            self.assertEqual(result.result_kind, ResultKind.GALLERY)
            self.assertEqual(
                runtime.settings_registry.schema.defaults()["plugins.downloadable.quality"],
                "original",
            )

            entry = BatchEntry(
                entry_id="entry-1",
                parent_id="parent-1",
                plugin_id="downloadable",
                site_id="downloadable",
                candidate_id="one",
                source_operation_id="gallery",
                source_url="https://downloadable.test/gallery/one",
            )
            retried = await RuntimePackageResolver(runtime).retry(
                entry,
                RetryPlan(
                    plan_id="retry-1",
                    start_at=FailureStage.RESOLVE,
                    scope=RetryScope.ENTRY,
                    target_ids=(entry.entry_id,),
                    reason="test",
                ),
                _NeverCancelled(),
                lambda _reason, _payload: None,
            )
            self.assertEqual(retried.assets[0].filename, "one.txt")

            output = StringIO()
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": str(root / "data")}):
                with redirect_stdout(output):
                    exit_code = await asyncio.to_thread(
                        main,
                        ["download", entry.source_url, "--json"],
                        runtime=runtime,
                    )
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(Path(payload["output_files"][0]).read_bytes(), b"plugin asset")

    async def test_plugin_routes_renders_and_dispatches_pagination_offline(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            marker = Path(directory) / "activated.txt"
            _write_external_plugin(root / "external", marker)

            disabled_runtime = build_application_runtime(plugin_roots=(root,))
            self.assertFalse(marker.exists())
            self.assertIsNone(disabled_runtime.plugin_for_id("external_plugin"))

            runtime = build_application_runtime(
                plugin_roots=(root,),
                enable_external_code=True,
            )
            self.assertTrue(marker.exists())
            dispatcher = OperationDispatcher(runtime)
            first = await dispatcher.dispatch(
                "https://external.test/list",
                request_id="request-1",
            )

            self.assertEqual(first.result_kind, ResultKind.LIST_PAGE)
            self.assertIsInstance(first.data, ListPageData)
            app = CorePageApp(
                first.page,
                ViewSession(
                    session_id="session-1",
                    plugin_id="external_plugin",
                    request_id="request-1",
                ),
            )
            async with app.run_test() as pilot:
                await pilot.press("right")

            action = app.return_value.action
            self.assertEqual(action.kind, ActionKind.INVOKE_OPERATION)
            second = await dispatcher.dispatch(
                "https://external.test/list",
                request_id="request-2",
                params=dict(action.params),
            )
            self.assertEqual(second.data.page_info.current_page, 2)
            self.assertEqual(second.data.items[0].item_id, "item-2")

    async def test_dispatcher_rejects_external_invalid_result_and_widget(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            _write_external_plugin(root / "external", Path(directory) / "activated.txt")
            runtime = build_application_runtime(
                plugin_roots=(root,),
                enable_external_code=True,
            )
            dispatcher = OperationDispatcher(runtime)

            invalid_result = await dispatcher.dispatch(
                "https://external.test/list",
                request_id="request-1",
                params={"mode": "invalid_result"},
            )
            invalid_widget = await dispatcher.dispatch(
                "https://external.test/list",
                request_id="request-2",
                params={"mode": "invalid_widget"},
            )

            self.assertEqual(invalid_result.data.code, "invalid_plugin_result")
            self.assertEqual(invalid_widget.data.code, "invalid_plugin_result")


class ExternalPluginCLITests(unittest.TestCase):
    def test_scan_only_then_json_download_uses_explicit_activation_gate(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            marker = Path(directory) / "activated.txt"
            _write_external_plugin(root / "external", marker)

            scan_output = StringIO()
            with redirect_stdout(scan_output):
                scan_exit = main(["plugins", "list", "--root", str(root)])
            self.assertEqual(scan_exit, 0)
            self.assertFalse(marker.exists())

            download_output = StringIO()
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with redirect_stdout(download_output):
                    download_exit = main(
                        [
                            "--plugin-root",
                            str(root),
                            "--enable-external-code",
                            "download",
                            "https://external.test/list",
                            "--json",
                        ]
                    )

            payload = json.loads(download_output.getvalue())
            self.assertEqual(download_exit, 0)
            self.assertTrue(marker.exists())
            self.assertEqual(payload["result_kind"], "interaction_required")
            self.assertEqual(payload["data"]["required_fields"], ["results"])
            self.assertIsNotNone(payload["page"])


def _write_external_plugin(plugin_folder: Path, marker: Path) -> None:
    plugin_folder.mkdir(parents=True)
    (plugin_folder / "manifests").mkdir()
    _write_json(
        plugin_folder / "plugin.json",
        {
            "plugin_id": "external_plugin",
            "display_name": "External Plugin",
            "version": "0.1.0",
            "entrypoint": "plugin.py:Plugin",
            "manifest": "site.json",
        },
    )
    _write_json(
        plugin_folder / "manifests" / "site.json",
        {
            "site_id": "external",
            "display_name": "External Site",
            "domains": ["external.test"],
            "url_rules": [
                {
                    "name": "list",
                    "pattern": r"^https://external\.test/list$",
                    "input_kind": "batch",
                    "operation": "browse",
                }
            ],
            "operations": {
                "browse": {
                    "params": {
                        "page": {
                            "type": "integer",
                            "default": 1,
                            "min": 1,
                            "max": 2,
                        },
                        "mode": {
                            "type": "string",
                            "default": "normal",
                            "enum": ["normal", "invalid_result", "invalid_widget"],
                        },
                    }
                }
            },
        },
    )
    source = f"""from pathlib import Path
from cli_downloader.core.operations import ListItemData, ListPageData, OperationResult, PageInfo, ResultKind, RouteMatch
from cli_downloader.core.widgets import Action, ActionKind, ListItem, ListView, Page, Pagination, SelectionMode, Title
from cli_downloader.sites.manifest import site_manifest_from_json_file

Path({str(marker)!r}).write_text("activated", encoding="utf-8")

class Plugin:
    site_id = "external"
    display_name = "External Site"

    def get_manifest_name(self):
        return "site.json"

    def get_manifest(self):
        return site_manifest_from_json_file(Path(__file__).parent / "manifests" / "site.json")

    def can_handle_url(self, url):
        return url == "https://external.test/list"

    def classify_url(self, url):
        manifest = self.get_manifest()
        route = manifest.route_url(url)
        if route is None or route.operation_schema is None:
            return None
        return RouteMatch(
            plugin_id="external_plugin",
            site_id=self.site_id,
            rule_name=route.rule_name,
            operation_id=route.operation,
            normalized_url=url,
            captures=route.captures,
            parameter_schema={{
                name: schema.to_dict()
                for name, schema in route.operation_schema.params.items()
            }},
        )

    async def execute(self, request):
        mode = request.params.get("mode", "normal")
        if mode == "invalid_result":
            return {{"result_kind": "list_page"}}
        page_number = request.params.get("page", 1)
        item = ListItemData(
            item_id=f"item-{{page_number}}",
            source_url=f"https://external.test/item/{{page_number}}",
            title=f"Item {{page_number}}",
        )
        next_action = None
        if page_number == 1:
            next_action = Action(
                action_id="next_page",
                kind=ActionKind.INVOKE_OPERATION,
                operation_id="browse",
                params={{"page": 2}},
            )
        page = Page(
            header=(Title(text="External Results"),),
            content=(
                ListView(
                    widget_id="results",
                    items=(ListItem(widget_id=item.item_id, title=item.title),),
                    selection_mode=SelectionMode.SINGLE,
                ),
                Pagination(
                    widget_id="pagination",
                    current_page=page_number,
                    total_pages=2,
                    next_action=next_action,
                ),
            ),
        )
        if mode == "invalid_widget":
            object.__setattr__(page, "content", (object(),))
        return OperationResult(
            request_id=request.request_id,
            result_kind=ResultKind.LIST_PAGE,
            data=ListPageData(
                title="External Results",
                items=(item,),
                page_info=PageInfo(
                    current_page=page_number,
                    page_size=1,
                    total_items=2,
                    total_pages=2,
                ),
            ),
            page=page,
            available_actions=("next_page",) if next_action else (),
        )

    async def resolve(self, request):
        raise NotImplementedError

    async def list_assets(self, item):
        raise NotImplementedError

    async def choose_strategy(self, item):
        raise NotImplementedError
"""
    (plugin_folder / "plugin.py").write_text(source, encoding="utf-8")


class _NeverCancelled:
    def is_set(self) -> bool:
        return False

    async def wait_if_paused(self) -> None:
        return None


def _write_download_plugin(plugin_folder: Path) -> None:
    plugin_folder.mkdir(parents=True)
    (plugin_folder / "manifests").mkdir()
    _write_json(
        plugin_folder / "plugin.json",
        {
            "plugin_id": "downloadable",
            "display_name": "Downloadable",
            "version": "1.0.0",
            "entrypoint": "plugin.py:Plugin",
            "manifest": "site.json",
        },
    )
    _write_json(
        plugin_folder / "manifests" / "site.json",
        {
            "site_id": "downloadable",
            "display_name": "Downloadable",
            "domains": ["downloadable.test"],
            "url_rules": [
                {
                    "name": "gallery",
                    "pattern": r"^https://downloadable\.test/gallery/(?P<slug>[a-z]+)$",
                    "input_kind": "single",
                    "operation": "gallery",
                }
            ],
            "operations": {"gallery": {"params": {}}},
            "settings": {
                "quality": {
                    "type": "string",
                    "default": "original",
                    "enum": ["original", "preview"],
                }
            },
        },
    )
    source = """from pathlib import Path
from cli_downloader.core.models import Asset, AssetKind, AssetList, DownloadStrategy, DownloadStrategyPlan, ResolveRequest, ResolvedItem
from cli_downloader.core.operations import GalleryData, OperationResult, ResultKind, RouteMatch
from cli_downloader.sites.manifest import site_manifest_from_json_file

class Plugin:
    site_id = "downloadable"
    display_name = "Downloadable"

    def get_manifest(self):
        return site_manifest_from_json_file(Path(__file__).parent / "manifests" / "site.json")

    def can_handle_url(self, url):
        return url.startswith("https://downloadable.test/")

    def classify_url(self, url):
        route = self.get_manifest().route_url(url)
        if route is None:
            return None
        return RouteMatch(
            plugin_id="downloadable",
            site_id=self.site_id,
            rule_name=route.rule_name,
            operation_id=route.operation,
            normalized_url=url,
            captures=route.captures,
            parameter_schema={},
        )

    async def execute(self, request):
        slug = request.route.captures["slug"]
        return OperationResult(
            request_id=request.request_id,
            result_kind=ResultKind.GALLERY,
            data=GalleryData(
                source_url=request.source_url,
                site_id=self.site_id,
                gallery_id=slug,
                title=slug.title(),
                expected_asset_count=1,
            ),
        )

    async def resolve(self, request):
        slug = request.url.rsplit("/", 1)[-1]
        return ResolvedItem(
            site_id=self.site_id,
            source_url=request.url,
            title=slug.title(),
            metadata={"gallery_id": slug},
        )

    async def list_assets(self, item):
        slug = item.metadata["gallery_id"]
        return AssetList((Asset(
            url=f"https://downloadable.test/assets/{slug}.txt",
            kind=AssetKind.FILE,
            filename=f"{slug}.txt",
        ),))

    async def choose_strategy(self, item):
        return DownloadStrategyPlan(
            strategy=DownloadStrategy.DIRECT,
            reason="direct",
            assets=await self.list_assets(item),
        )

    async def fetch(self, asset):
        return b"plugin asset"
"""
    (plugin_folder / "plugin.py").write_text(source, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
