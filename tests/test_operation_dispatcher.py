import unittest
from dataclasses import dataclass

from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.config.loader import LoadedConfig
from cli_downloader.config.schema import SettingsRegistry
from cli_downloader.core.operations import (
    MessageData,
    OperationRequest,
    OperationResult,
    ResultKind,
    RouteMatch,
)
from cli_downloader.plugins.runtime import RuntimePlugin
from cli_downloader.sites.manifest import (
    InputKind,
    OperationSchema,
    ParameterSchema,
    SettingSchema,
    SiteManifest,
    UrlRule,
)


def _manifest(*, supported: bool = True) -> SiteManifest:
    return SiteManifest(
        site_id="sample",
        display_name="Sample",
        domains=("sample.test",),
        url_rules=(
            UrlRule(
                name="gallery",
                pattern=r"^https://sample\.test/g/(?P<id>\d+)$",
                input_kind=InputKind.SINGLE,
                operation="resolve_gallery",
            ),
        ),
        operations={
            "resolve_gallery": OperationSchema(
                name="resolve_gallery",
                supported=supported,
                requires_live_network_confirmation=True,
                params={
                    "limit": ParameterSchema(type="integer", default=10, min=1, max=20),
                    "mode": ParameterSchema(
                        type="string",
                        default="safe",
                        enum=("safe", "full"),
                        editable=False,
                    ),
                },
            ),
        },
        settings={"quality": SettingSchema(type="string", default="original")},
        secrets={"session": SettingSchema(type="string", secret=True)},
    )


def _route(manifest: SiteManifest) -> RouteMatch:
    operation = manifest.operations["resolve_gallery"]
    return RouteMatch(
        plugin_id="sample_plugin",
        site_id="sample",
        rule_name="gallery",
        operation_id="resolve_gallery",
        normalized_url="https://sample.test/g/1",
        captures={"id": "1"},
        parameter_schema={name: schema.to_dict() for name, schema in operation.params.items()},
        required_confirmations=("live_network",),
    )


class SamplePlugin:
    site_id = "sample"
    display_name = "Sample"

    def __init__(self, manifest: SiteManifest) -> None:
        self.manifest = manifest
        self.requests: list[OperationRequest] = []
        self.route: object = _route(manifest)
        self.result: object | None = None

    def classify_url(self, url: str) -> object:
        return self.route

    async def execute(self, request: OperationRequest) -> object:
        self.requests.append(request)
        if self.result is not None:
            return self.result
        return OperationResult(
            request_id=request.request_id,
            result_kind=ResultKind.MESSAGE,
            data=MessageData(message="ok"),
        )


@dataclass
class RuntimeStub:
    entry: RuntimePlugin | None
    settings_registry: SettingsRegistry

    def entry_for_url(self, url: str) -> RuntimePlugin | None:
        return self.entry if url.startswith("https://sample.test/") else None


def _dispatcher(*, supported: bool = True) -> tuple[OperationDispatcher, SamplePlugin]:
    manifest = _manifest(supported=supported)
    plugin = SamplePlugin(manifest)
    entry = RuntimePlugin(
        plugin_id="sample_plugin",
        instance=plugin,  # type: ignore[arg-type]
        manifest=manifest,
    )
    settings_registry = SettingsRegistry()
    settings_registry.register_plugin("sample_plugin", manifest)
    return (
        OperationDispatcher(RuntimeStub(entry, settings_registry)),  # type: ignore[arg-type]
        plugin,
    )


class OperationDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_validated_request_to_selected_plugin(self) -> None:
        dispatcher, plugin = _dispatcher()

        result = await dispatcher.dispatch(
            "https://sample.test/g/1",
            request_id="request-1",
            params={"limit": 15},
            confirmations=("live_network",),
        )

        self.assertEqual(result.result_kind, ResultKind.MESSAGE)
        self.assertEqual(plugin.requests[0].params, {"limit": 15, "mode": "safe"})
        self.assertEqual(
            plugin.requests[0].settings,
            {
                "download.strategy": "auto",
                "torrent.client": "none",
                "quality": "original",
            },
        )

    async def test_dispatcher_passes_validated_values_and_secret_references(self) -> None:
        manifest = _manifest()
        plugin = SamplePlugin(manifest)
        entry = RuntimePlugin("sample_plugin", plugin, manifest)  # type: ignore[arg-type]
        settings_registry = SettingsRegistry()
        settings_registry.register_plugin("sample_plugin", manifest)
        dispatcher = OperationDispatcher(
            RuntimeStub(entry, settings_registry),  # type: ignore[arg-type]
            config=LoadedConfig(
                values={"plugins.sample_plugin.quality": "compressed"},
                secrets={"plugins.sample_plugin.session": "raw-secret"},
                sources=(),
            ),
        )

        result = await dispatcher.dispatch(
            "https://sample.test/g/1",
            request_id="request-1",
            confirmations=("live_network",),
        )

        self.assertEqual(result.result_kind, ResultKind.MESSAGE)
        request = plugin.requests[0]
        self.assertEqual(request.settings["quality"], "compressed")
        self.assertEqual(
            request.secret_refs,
            {"session": "secrets.plugins.sample_plugin.session"},
        )
        self.assertNotIn("raw-secret", repr(request))

    async def test_rejects_url_without_registered_hostname(self) -> None:
        dispatcher, plugin = _dispatcher()

        result = await dispatcher.dispatch("https://other.test/g/1", request_id="request-1")

        self.assertEqual(result.data.code, "unsupported_url")
        self.assertEqual(plugin.requests, [])

    async def test_rejects_operation_not_registered_by_manifest(self) -> None:
        dispatcher, plugin = _dispatcher()
        route = _route(plugin.manifest)
        object.__setattr__(route, "operation_id", "other_operation")
        plugin.route = route

        result = await dispatcher.dispatch("https://sample.test/g/1", request_id="request-1")

        self.assertEqual(result.data.code, "unknown_operation")
        self.assertEqual(plugin.requests, [])

    async def test_rejects_disabled_operation(self) -> None:
        dispatcher, plugin = _dispatcher(supported=False)

        result = await dispatcher.dispatch("https://sample.test/g/1", request_id="request-1")

        self.assertEqual(result.data.code, "unsupported_operation")
        self.assertEqual(plugin.requests, [])

    async def test_rejects_invalid_and_locked_parameters(self) -> None:
        cases = (
            ({"unknown": 1}, "Unknown parameters"),
            ({"limit": 0}, "must be at least"),
            ({"limit": True}, "must be of type integer"),
            ({"mode": "full"}, "cannot be changed"),
        )
        for params, message in cases:
            with self.subTest(params=params):
                dispatcher, plugin = _dispatcher()
                result = await dispatcher.dispatch(
                    "https://sample.test/g/1",
                    request_id="request-1",
                    params=params,
                    confirmations=("live_network",),
                )
                self.assertEqual(result.data.code, "invalid_params")
                self.assertIn(message, result.data.message)
                self.assertEqual(plugin.requests, [])

    async def test_rejects_non_object_parameters(self) -> None:
        dispatcher, plugin = _dispatcher()

        result = await dispatcher.dispatch(
            "https://sample.test/g/1",
            request_id="request-1",
            params=["limit"],  # type: ignore[arg-type]
            confirmations=("live_network",),
        )

        self.assertEqual(result.data.code, "invalid_params")
        self.assertEqual(plugin.requests, [])

        result = await dispatcher.dispatch(
            "https://sample.test/g/1",
            request_id="request-2",
            params={1: "limit"},  # type: ignore[dict-item]
            confirmations=("live_network",),
        )
        self.assertEqual(result.data.code, "invalid_params")

    async def test_requires_manifest_confirmation_before_plugin_invocation(self) -> None:
        dispatcher, plugin = _dispatcher()

        result = await dispatcher.dispatch("https://sample.test/g/1", request_id="request-1")

        self.assertEqual(result.data.code, "confirmation_required")
        self.assertEqual(result.data.details["required_confirmations"], ("live_network",))
        self.assertEqual(plugin.requests, [])

    async def test_rejects_route_schema_that_disagrees_with_manifest(self) -> None:
        dispatcher, plugin = _dispatcher()
        route = _route(plugin.manifest)
        object.__setattr__(route, "required_confirmations", ())
        plugin.route = route

        result = await dispatcher.dispatch("https://sample.test/g/1", request_id="request-1")

        self.assertEqual(result.data.code, "invalid_plugin_route")
        self.assertEqual(plugin.requests, [])

    async def test_rejects_non_core_route_and_result(self) -> None:
        dispatcher, plugin = _dispatcher()
        plugin.route = {"operation_id": "resolve_gallery"}
        route_result = await dispatcher.dispatch(
            "https://sample.test/g/1",
            request_id="request-1",
        )
        self.assertEqual(route_result.data.code, "invalid_plugin_route")

        plugin.route = _route(plugin.manifest)
        plugin.result = {"result_kind": "message"}
        result = await dispatcher.dispatch(
            "https://sample.test/g/1",
            request_id="request-2",
            confirmations=("live_network",),
        )
        self.assertEqual(result.data.code, "invalid_plugin_result")

        plugin.result = OperationResult(
            request_id="wrong-request",
            result_kind=ResultKind.MESSAGE,
            data=MessageData(message="ok"),
        )
        result = await dispatcher.dispatch(
            "https://sample.test/g/1",
            request_id="request-3",
            confirmations=("live_network",),
        )
        self.assertEqual(result.data.code, "invalid_plugin_result")

    async def test_converts_plugin_exception_to_structured_error(self) -> None:
        dispatcher, plugin = _dispatcher()

        async def fail(request: OperationRequest) -> OperationResult:
            raise RuntimeError("offline")

        plugin.execute = fail  # type: ignore[method-assign]
        result = await dispatcher.dispatch(
            "https://sample.test/g/1",
            request_id="request-1",
            confirmations=("live_network",),
        )

        self.assertEqual(result.data.code, "plugin_execution_failed")
        self.assertIn("offline", result.data.message)


if __name__ == "__main__":
    unittest.main()
