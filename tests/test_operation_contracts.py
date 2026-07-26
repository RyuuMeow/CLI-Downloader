import json
import unittest

from cli_downloader.core.errors import ContractValidationError
from cli_downloader.core.operations import (
    AssetData,
    ContinuationData,
    DiagnosticSeverity,
    ErrorData,
    InteractionRequiredData,
    GalleryData,
    ListItemData,
    ListPageData,
    MessageData,
    OperationDiagnostic,
    OperationRequest,
    OperationResult,
    PageInfo,
    ProgressData,
    ResultKind,
    RouteMatch,
)
from cli_downloader.core.widgets import Page, Text


class RendererObject:
    pass


def make_route(**changes: object) -> RouteMatch:
    values: dict[str, object] = {
        "plugin_id": "example_plugin",
        "site_id": "example",
        "rule_name": "gallery_url",
        "operation_id": "resolve_gallery",
        "normalized_url": "https://example.test/gallery/42",
        "captures": {"gallery_id": "42"},
        "parameter_schema": {"quality": {"type": "string"}},
        "required_confirmations": ("live_network",),
    }
    values.update(changes)
    return RouteMatch(**values)


def make_gallery(**changes: object) -> GalleryData:
    values: dict[str, object] = {
        "source_url": "https://example.test/gallery/42",
        "site_id": "example",
        "gallery_id": "42",
        "title": "Example Gallery",
        "authors": ("Author",),
        "tags": ("tag",),
        "expected_asset_count": 2,
        "metadata": {"language": "en"},
    }
    values.update(changes)
    return GalleryData(**values)


class RouteMatchTests(unittest.TestCase):
    def test_route_serializes_to_json_compatible_values(self) -> None:
        payload = make_route().to_dict()

        self.assertEqual(payload["captures"], {"gallery_id": "42"})
        self.assertEqual(payload["required_confirmations"], ["live_network"])
        json.dumps(payload)

    def test_invalid_identifier_returns_structured_error(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_route(plugin_id="Example Plugin")

        self.assertEqual(
            context.exception.to_dict(),
            {
                "error_type": "ContractValidationError",
                "code": "invalid_identifier",
                "path": "route.plugin_id",
                "message": "must use lowercase letters, numbers, dots, underscores, or hyphens",
            },
        )

    def test_relative_url_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_route(normalized_url="/gallery/42")

        self.assertEqual(context.exception.code, "invalid_url")
        self.assertEqual(context.exception.path, "route.normalized_url")

    def test_malformed_url_returns_structured_error(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_route(normalized_url="https://[invalid")

        self.assertEqual(context.exception.code, "invalid_url")
        self.assertEqual(context.exception.path, "route.normalized_url")

    def test_url_credentials_are_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_route(normalized_url="https://user:secret@example.test/gallery/42")

        self.assertEqual(context.exception.code, "url_credentials_forbidden")

    def test_non_web_route_scheme_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_route(normalized_url="javascript:alert(1)")

        self.assertEqual(context.exception.code, "unsupported_url_scheme")

    def test_route_json_values_are_deeply_immutable(self) -> None:
        route = make_route()

        with self.assertRaises(TypeError):
            route.parameter_schema["new"] = "value"


class OperationRequestTests(unittest.TestCase):
    def test_request_serializes_nested_route_and_references(self) -> None:
        request = OperationRequest(
            request_id="request-1",
            plugin_id="example_plugin",
            site_id="example",
            operation_id="resolve_gallery",
            source_url="https://example.test/gallery/42?raw=1",
            route=make_route(),
            params={"quality": "original", "pages": [1, 2]},
            profile="default",
            settings={"download.strategy": "direct", "quality": "original"},
            setting_refs={"quality": "sites.example.quality"},
            secret_refs={"session": "secrets.example.session"},
            confirmations=("live_network",),
        )

        payload = request.to_dict()
        self.assertEqual(payload["route"]["operation_id"], "resolve_gallery")
        self.assertEqual(payload["params"]["pages"], [1, 2])
        self.assertEqual(payload["settings"]["download.strategy"], "direct")
        self.assertNotIn("secret_value", json.dumps(payload))
        json.dumps(payload)

    def test_route_identity_must_match_request(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            OperationRequest(
                request_id="request-1",
                plugin_id="other_plugin",
                site_id="example",
                operation_id="resolve_gallery",
                source_url="https://example.test/gallery/42",
                route=make_route(),
            )

        self.assertEqual(context.exception.code, "route_mismatch")
        self.assertEqual(context.exception.path, "request.plugin_id")

    def test_plugin_objects_are_rejected_from_params(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            OperationRequest(
                request_id="request-1",
                plugin_id="example_plugin",
                site_id="example",
                operation_id="resolve_gallery",
                source_url="https://example.test/gallery/42",
                route=make_route(),
                params={"renderer": RendererObject()},
            )

        self.assertEqual(context.exception.code, "non_json_value")
        self.assertEqual(context.exception.path, "request.params.renderer")

    def test_json_cycles_are_rejected(self) -> None:
        params: dict[str, object] = {}
        params["cycle"] = params

        with self.assertRaises(ContractValidationError) as context:
            OperationRequest(
                request_id="request-1",
                plugin_id="example_plugin",
                site_id="example",
                operation_id="resolve_gallery",
                source_url="https://example.test/gallery/42",
                route=make_route(),
                params=params,
            )

        self.assertEqual(context.exception.code, "json_cycle")

    def test_non_finite_numbers_are_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            OperationRequest(
                request_id="request-1",
                plugin_id="example_plugin",
                site_id="example",
                operation_id="resolve_gallery",
                source_url="https://example.test/gallery/42",
                route=make_route(),
                params={"score": float("nan")},
            )

        self.assertEqual(context.exception.code, "invalid_json_number")


class GalleryAndAssetTests(unittest.TestCase):
    def test_gallery_and_cover_serialize(self) -> None:
        cover = AssetData(
            asset_id="cover",
            url="https://example.test/images/cover.jpg",
            filename="cover.jpg",
            index=0,
            headers={"User-Agent": "CLI-Downloader", "Accept": "image/jpeg"},
            referrer="https://example.test/gallery/42",
            metadata={"width": 320},
        )

        payload = make_gallery(cover=cover).to_dict()
        self.assertEqual(payload["cover"]["filename"], "cover.jpg")
        self.assertEqual(payload["expected_asset_count"], 2)
        json.dumps(payload)

    def test_asset_filename_cannot_escape_output_directory(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            AssetData(
                asset_id="asset-1",
                url="https://example.test/image/1",
                filename="../secret.txt",
            )

        self.assertEqual(context.exception.code, "invalid_filename")

    def test_asset_supports_magnet_urls(self) -> None:
        asset = AssetData(
            asset_id="torrent",
            url="magnet:?xt=urn:btih:0123456789abcdef",
        )

        self.assertEqual(asset.to_dict()["url"], "magnet:?xt=urn:btih:0123456789abcdef")

    def test_negative_expected_asset_count_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_gallery(expected_asset_count=-1)

        self.assertEqual(context.exception.path, "gallery.expected_asset_count")


class ListPageTests(unittest.TestCase):
    def test_list_page_serializes_items_filters_and_pagination(self) -> None:
        item = ListItemData(
            item_id="item-1",
            source_url="https://example.test/gallery/42",
            title="Result",
            summary="Summary",
            tags=("tag",),
            metadata={"rating": 4.5},
        )
        page = ListPageData(
            title="Search Results",
            items=(item,),
            page_info=PageInfo(
                current_page=1,
                page_size=20,
                total_items=21,
                total_pages=2,
                next_token="page-2",
            ),
            active_filters={"language": "en"},
            available_filters={"language": ["en", "ja"]},
        )

        payload = page.to_dict()
        self.assertEqual(payload["items"][0]["item_id"], "item-1")
        self.assertEqual(payload["page_info"]["next_token"], "page-2")
        self.assertEqual(payload["available_filters"]["language"], ["en", "ja"])
        json.dumps(payload)

    def test_duplicate_item_ids_are_rejected(self) -> None:
        item = ListItemData(
            item_id="duplicate",
            source_url="https://example.test/gallery/42",
            title="Result",
        )

        with self.assertRaises(ContractValidationError) as context:
            ListPageData(
                title="Search Results",
                items=(item, item),
                page_info=PageInfo(current_page=1, page_size=20),
            )

        self.assertEqual(context.exception.code, "duplicate_item_id")

    def test_current_page_cannot_exceed_total_pages(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            PageInfo(current_page=3, page_size=20, total_pages=2)

        self.assertEqual(context.exception.code, "invalid_page_range")

    def test_empty_result_page_allows_zero_total_pages(self) -> None:
        page = PageInfo(current_page=1, page_size=20, total_items=0, total_pages=0)

        self.assertEqual(page.to_dict()["total_pages"], 0)


class OperationResultTests(unittest.TestCase):
    def test_gallery_result_serializes_all_envelope_fields(self) -> None:
        result = OperationResult(
            request_id="request-1",
            result_kind=ResultKind.GALLERY,
            data=make_gallery(),
            available_actions=("download",),
            continuation=ContinuationData(
                operation_id="enumerate_assets",
                token="next-token",
                params={"start": 2},
            ),
            diagnostics=(
                OperationDiagnostic(
                    code="cached_metadata",
                    message="Metadata came from cache",
                    severity=DiagnosticSeverity.INFO,
                ),
            ),
        )

        payload = result.to_dict()
        self.assertEqual(payload["result_kind"], "gallery")
        self.assertEqual(payload["data"]["gallery_id"], "42")
        self.assertEqual(payload["continuation"]["operation_id"], "enumerate_assets")
        self.assertEqual(payload["diagnostics"][0]["severity"], "info")
        json.dumps(payload)

    def test_result_kind_must_match_data_type(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            OperationResult(
                request_id="request-1",
                result_kind=ResultKind.LIST_PAGE,
                data=make_gallery(),
            )

        self.assertEqual(context.exception.code, "result_data_mismatch")
        self.assertIn("ListPageData", context.exception.message)

    def test_page_content_must_use_core_page(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            OperationResult(
                request_id="request-1",
                result_kind=ResultKind.GALLERY,
                data=make_gallery(),
                page={"widget": RendererObject()},
            )

        self.assertEqual(context.exception.code, "invalid_page")
        self.assertEqual(context.exception.path, "result.page")

    def test_core_page_serializes_in_result(self) -> None:
        result = OperationResult(
            request_id="request-1",
            result_kind=ResultKind.GALLERY,
            data=make_gallery(),
            page=Page(content=(Text(text="Ready"),)),
        )

        self.assertEqual(result.to_dict()["page"]["content"][0]["text"], "Ready")

    def test_standard_result_data_types_serialize(self) -> None:
        item = ListItemData(
            item_id="item-1",
            source_url="https://example.test/gallery/42",
            title="Result",
        )
        cases = (
            (ResultKind.GALLERY, make_gallery()),
            (
                ResultKind.LIST_PAGE,
                ListPageData(
                    title="Results",
                    items=(item,),
                    page_info=PageInfo(current_page=1, page_size=20),
                ),
            ),
            (ResultKind.MESSAGE, MessageData(message="Done")),
            (ResultKind.PROGRESS, ProgressData(current=1, total=2)),
            (
                ResultKind.INTERACTION_REQUIRED,
                InteractionRequiredData(
                    message="Choose an item",
                    required_fields=("results",),
                ),
            ),
            (
                ResultKind.ERROR,
                ErrorData(code="network_error", message="Unavailable", retryable=True),
            ),
        )

        for result_kind, data in cases:
            with self.subTest(result_kind=result_kind):
                payload = OperationResult(
                    request_id="request-1",
                    result_kind=result_kind,
                    data=data,
                ).to_dict()
                json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
