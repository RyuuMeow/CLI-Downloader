from __future__ import annotations

import inspect
import math
from collections.abc import Mapping
from dataclasses import replace

from cli_downloader.config.loader import LoadedConfig
from cli_downloader.core.errors import ContractValidationError
from cli_downloader.core.operations import (
    ErrorData,
    FrozenJsonValue,
    OperationRequest,
    OperationResult,
    ResultKind,
    RouteMatch,
)
from cli_downloader.plugins.runtime import ApplicationRuntime, RuntimePlugin
from cli_downloader.sites.manifest import OperationSchema, ParameterSchema


class OperationDispatcher:
    def __init__(self, runtime: ApplicationRuntime, *, config: LoadedConfig | None = None) -> None:
        self._runtime = runtime
        self._config = config

    async def dispatch(
        self,
        url: str,
        *,
        request_id: str,
        params: Mapping[str, object] | None = None,
        profile: str | None = None,
        setting_refs: Mapping[str, str] | None = None,
        secret_refs: Mapping[str, str] | None = None,
        confirmations: tuple[str, ...] = (),
    ) -> OperationResult:
        entry = self._runtime.entry_for_url(url)
        if entry is None:
            return _error_result(request_id, "unsupported_url", "No plugin supports this URL")

        route = self._classify(entry, url, request_id)
        if isinstance(route, OperationResult):
            return route

        operation = self._validate_route(entry, route, request_id)
        if isinstance(operation, OperationResult):
            return operation

        if params is not None and not isinstance(params, Mapping):
            return _error_result(request_id, "invalid_params", "Parameters must be an object")
        if secret_refs is not None and not isinstance(secret_refs, Mapping):
            return _error_result(
                request_id, "invalid_params", "Secret references must be an object"
            )

        settings: Mapping[str, object] = {}
        registered_secret_refs: Mapping[str, str] = secret_refs or {}
        registry = getattr(self._runtime, "settings_registry", None)
        if registry is not None:
            context = registry.context(
                entry.plugin_id,
                values=self._config.values if self._config is not None else None,
                secrets=self._config.secrets if self._config is not None else None,
            )
            settings = context.settings
            registered_secret_refs = context.secret_refs

        validated_params = _validate_params(
            operation,
            params or {},
            registered_secret_refs,
        )
        if isinstance(validated_params, str):
            return _error_result(request_id, "invalid_params", validated_params)

        missing = tuple(
            confirmation
            for confirmation in route.required_confirmations
            if confirmation not in confirmations
        )
        if missing:
            return _error_result(
                request_id,
                "confirmation_required",
                f"Missing required confirmations: {', '.join(missing)}",
                details={"required_confirmations": list(missing)},
            )

        try:
            request = OperationRequest(
                request_id=request_id,
                plugin_id=entry.plugin_id,
                site_id=entry.site_id,
                operation_id=route.operation_id,
                source_url=url,
                route=route,
                params=validated_params,
                profile=profile,
                settings=settings,
                setting_refs=setting_refs or {},
                secret_refs=registered_secret_refs,
                confirmations=confirmations,
            )
        except ContractValidationError as error:
            return _error_result(
                request_id,
                "invalid_request",
                error.message,
                details={"path": error.path, "validation_code": error.code},
            )

        execute = getattr(entry.instance, "execute", None)
        if not callable(execute):
            return _error_result(
                request_id,
                "invalid_plugin_contract",
                "Plugin does not provide execute(request)",
            )
        try:
            pending_result = execute(request)
            if not inspect.isawaitable(pending_result):
                return _error_result(
                    request_id,
                    "invalid_plugin_contract",
                    "Plugin execute(request) must be asynchronous",
                )
            result = await pending_result
        except Exception as error:
            return _error_result(
                request_id,
                "plugin_execution_failed",
                f"Plugin operation failed: {error}",
            )

        if type(result) is not OperationResult:
            return _error_result(
                request_id,
                "invalid_plugin_result",
                "Plugin must return a core OperationResult",
            )
        try:
            page = replace(result.page) if result.page is not None else None
            result = replace(result, page=page)
        except Exception:
            return _error_result(
                request_id,
                "invalid_plugin_result",
                "Plugin returned an invalid core result or widget tree",
            )
        if result.request_id != request_id:
            return _error_result(
                request_id,
                "invalid_plugin_result",
                "Plugin result request_id does not match the request",
            )
        return result

    async def dispatch_plugin_operation(
        self,
        plugin_id: str,
        operation_id: str,
        *,
        request_id: str,
        params: Mapping[str, object] | None = None,
        profile: str | None = None,
        setting_refs: Mapping[str, str] | None = None,
        secret_refs: Mapping[str, str] | None = None,
        confirmations: tuple[str, ...] = (),
    ) -> OperationResult:
        entry = self._runtime.plugins_by_id.get(plugin_id)
        if entry is None:
            return _error_result(request_id, "unknown_plugin", "Plugin not found")

        operation = entry.manifest.operations.get(operation_id)
        if operation is None:
            if operation_id == "help":
                return self._generate_help_result(plugin_id, request_id)
            return _error_result(
                request_id,
                "unknown_operation",
                "Operation is not registered by the plugin manifest",
            )
        if not operation.supported:
            return _error_result(
                request_id,
                "unsupported_operation",
                f"Operation is disabled by the plugin manifest: {operation_id}",
            )

        # Create a synthetic route match
        route = RouteMatch(
            plugin_id=plugin_id,
            site_id=entry.site_id,
            rule_name="shell_command",
            operation_id=operation_id,
            normalized_url=f"plugin://{plugin_id}/{operation_id}",
            captures={},
            parameter_schema={
                name: schema.to_dict() for name, schema in sorted(operation.params.items())
            },
            required_confirmations=("live_network",)
            if operation.requires_live_network_confirmation
            else (),
        )

        # Then just defer back to dispatch with the fake URL
        # We can't because dispatch calls `entry_for_url` and `_classify`.
        # Instead, we do the rest of the dispatch logic here.
        if params is not None and not isinstance(params, Mapping):
            return _error_result(request_id, "invalid_params", "Parameters must be an object")
        if secret_refs is not None and not isinstance(secret_refs, Mapping):
            return _error_result(
                request_id, "invalid_params", "Secret references must be an object"
            )

        settings: Mapping[str, object] = {}
        registered_secret_refs: Mapping[str, str] = secret_refs or {}
        registry = getattr(self._runtime, "settings_registry", None)
        if registry is not None:
            context = registry.context(
                entry.plugin_id,
                values=self._config.values if self._config is not None else None,
                secrets=self._config.secrets if self._config is not None else None,
            )
            settings = context.settings
            registered_secret_refs = context.secret_refs

        validated_params = _validate_params(
            operation,
            params or {},
            registered_secret_refs,
        )
        if isinstance(validated_params, str):
            return _error_result(request_id, "invalid_params", validated_params)

        missing = tuple(
            confirmation
            for confirmation in route.required_confirmations
            if confirmation not in confirmations
        )
        if missing:
            return _error_result(
                request_id,
                "confirmation_required",
                f"Missing required confirmations: {', '.join(missing)}",
                details={"required_confirmations": list(missing)},
            )

        try:
            request = OperationRequest(
                request_id=request_id,
                plugin_id=entry.plugin_id,
                site_id=entry.site_id,
                operation_id=route.operation_id,
                source_url=route.normalized_url,
                route=route,
                params=validated_params,
                profile=profile,
                settings=settings,
                setting_refs=setting_refs or {},
                secret_refs=registered_secret_refs,
                confirmations=confirmations,
            )
        except ContractValidationError as error:
            return _error_result(
                request_id,
                "invalid_request",
                error.message,
                details={"path": error.path, "validation_code": error.code},
            )

        execute = getattr(entry.instance, "execute", None)
        if not callable(execute):
            return _error_result(
                request_id,
                "invalid_plugin_contract",
                "Plugin does not provide execute(request)",
            )
        try:
            pending_result = execute(request)
            if not inspect.isawaitable(pending_result):
                return _error_result(
                    request_id,
                    "invalid_plugin_contract",
                    "Plugin execute(request) must be asynchronous",
                )
            result = await pending_result
        except Exception as error:
            return _error_result(
                request_id,
                "plugin_execution_failed",
                f"Plugin operation failed: {error}",
            )

        if type(result) is not OperationResult:
            return _error_result(
                request_id,
                "invalid_plugin_result",
                "Plugin must return a core OperationResult",
            )
        try:
            page = replace(result.page) if result.page is not None else None
            result = replace(result, page=page)
        except Exception:
            return _error_result(
                request_id,
                "invalid_plugin_result",
                "Plugin returned an invalid core result or widget tree",
            )
        if result.request_id != request_id:
            return _error_result(
                request_id,
                "invalid_plugin_result",
                "Plugin result request_id does not match the request",
            )
        return result

    def _classify(
        self,
        entry: RuntimePlugin,
        url: str,
        request_id: str,
    ) -> RouteMatch | OperationResult:
        classify = getattr(entry.instance, "classify_url", None)
        if not callable(classify):
            return _error_result(
                request_id,
                "invalid_plugin_contract",
                "Plugin does not provide classify_url(url)",
            )
        try:
            route = classify(url)
        except Exception as error:
            return _error_result(
                request_id,
                "plugin_classification_failed",
                f"Plugin URL classification failed: {error}",
            )
        if route is None:
            return _error_result(request_id, "unsupported_url", "Plugin does not support this URL")
        if type(route) is not RouteMatch:
            return _error_result(
                request_id,
                "invalid_plugin_route",
                "Plugin must return a core RouteMatch",
            )
        return route

    @staticmethod
    def _validate_route(
        entry: RuntimePlugin,
        route: RouteMatch,
        request_id: str,
    ) -> OperationSchema | OperationResult:
        if route.plugin_id != entry.plugin_id or route.site_id != entry.site_id:
            return _error_result(
                request_id,
                "invalid_plugin_route",
                "Route plugin_id and site_id must match the selected plugin",
            )

        rule = next(
            (item for item in entry.manifest.url_rules if item.name == route.rule_name),
            None,
        )
        operation = entry.manifest.operations.get(route.operation_id)
        if rule is None or rule.operation != route.operation_id or operation is None:
            return _error_result(
                request_id,
                "unknown_operation",
                "Route operation is not registered by the plugin manifest",
            )
        if not operation.supported:
            return _error_result(
                request_id,
                "unsupported_operation",
                f"Operation is disabled by the plugin manifest: {route.operation_id}",
            )

        expected_schema = {
            name: schema.to_dict() for name, schema in sorted(operation.params.items())
        }
        expected_confirmations = (
            ("live_network",) if operation.requires_live_network_confirmation else ()
        )
        route_schema = route.to_dict()["parameter_schema"]
        if (
            route_schema != expected_schema
            or route.required_confirmations != expected_confirmations
        ):
            return _error_result(
                request_id,
                "invalid_plugin_route",
                "Route schema and confirmations must match the plugin manifest",
            )
        return operation

    def _generate_help_result(self, plugin_id: str, request_id: str) -> OperationResult:
        from cli_downloader.core.operations import ListPageData, ListItemData, PageInfo
        from cli_downloader.core.widgets import Page, ListView, ListItem, Title

        entry = self._runtime.plugins_by_id.get(plugin_id)
        if not entry:
            return _error_result(request_id, "unknown_plugin", "Plugin not found")

        data_items = []
        widget_items = []
        for op_id, op in sorted(entry.manifest.operations.items()):
            if not op.supported:
                continue
            params_str = " ".join(f"{k}=..." for k in op.params.keys())
            summary = params_str if params_str else "(No parameters)"
            data_items.append(
                ListItemData(
                    item_id=op_id,
                    title=op_id,
                    summary=summary,
                    source_url=f"https://cli-downloader.local/plugin/{plugin_id}/{op_id}",
                    resolved=True,
                )
            )
            widget_items.append(
                ListItem(
                    widget_id=f"item_{op_id}",
                    title=op_id,
                    text=summary,
                )
            )

        return OperationResult(
            request_id=request_id,
            result_kind=ResultKind.LIST_PAGE,
            data=ListPageData(
                title=f"Plugin Operations: {plugin_id}",
                items=tuple(data_items),
                page_info=PageInfo(
                    current_page=1, total_pages=1, page_size=max(1, len(data_items))
                ),
            ),
            page=Page(
                header=(Title(text=f"Plugin Operations: {plugin_id}"),),
                content=(ListView(items=tuple(widget_items)),),
            ),
        )


def _validate_params(
    operation: OperationSchema,
    supplied: Mapping[str, object],
    secret_refs: Mapping[str, str],
) -> dict[str, object] | str:
    if any(not isinstance(name, str) for name in supplied):
        return "Parameter names must be strings"
    unknown = sorted(set(supplied) - set(operation.params))
    if unknown:
        return f"Unknown parameters: {', '.join(unknown)}"

    values: dict[str, object] = {}
    for name, schema in operation.params.items():
        provided = name in supplied
        if provided:
            value = supplied[name]
        elif schema.default is not None:
            value = schema.default
        elif schema.required:
            return f"Missing required parameter: {name}"
        else:
            continue

        if provided and not schema.editable and value != schema.default:
            return f"Parameter cannot be changed: {name}"
        error = _validate_parameter_value(name, value, schema)
        if error is not None:
            return error
        if schema.requires_secret is not None and schema.requires_secret not in secret_refs:
            return f"Parameter {name} requires secret reference: {schema.requires_secret}"
        values[name] = value
    return values


def _validate_parameter_value(
    name: str,
    value: object,
    schema: ParameterSchema,
) -> str | None:
    valid = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "string_list": lambda item: (
            isinstance(item, (list, tuple)) and all(isinstance(entry, str) for entry in item)
        ),
    }
    validator = valid.get(schema.type)
    if validator is None:
        return f"Parameter {name} uses unsupported schema type: {schema.type}"
    if not validator(value):
        return f"Parameter {name} must be of type {schema.type}"
    if schema.type == "number" and not math.isfinite(value):
        return f"Parameter {name} must be a finite number"
    if schema.enum and value not in schema.enum:
        return f"Parameter {name} must be one of: {', '.join(schema.enum)}"
    if schema.min is not None and isinstance(value, (int, float)) and value < schema.min:
        return f"Parameter {name} must be at least {schema.min}"
    if schema.max is not None and isinstance(value, (int, float)) and value > schema.max:
        return f"Parameter {name} must be at most {schema.max}"
    return None


def _error_result(
    request_id: str,
    code: str,
    message: str,
    *,
    details: Mapping[str, FrozenJsonValue] | None = None,
) -> OperationResult:
    return OperationResult(
        request_id=request_id,
        result_kind=ResultKind.ERROR,
        data=ErrorData(code=code, message=message, details=details or {}),
    )
