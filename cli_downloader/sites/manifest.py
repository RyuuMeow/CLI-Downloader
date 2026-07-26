from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from cli_downloader.core.errors import PluginValidationError
from cli_downloader.core.output import COMMON_OUTPUT_FIELDS, validate_folder_template


class InputKind(StrEnum):
    SINGLE = "single"
    PAGE = "page"
    BATCH = "batch"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ParameterSchema:
    type: str
    default: Any | None = None
    required: bool = False
    min: int | float | None = None
    max: int | float | None = None
    enum: tuple[str, ...] = ()
    editable: bool = True
    visible: bool = True
    requires_secret: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "required": self.required,
            "editable": self.editable,
            "visible": self.visible,
        }
        if self.default is not None:
            payload["default"] = self.default
        if self.min is not None:
            payload["min"] = self.min
        if self.max is not None:
            payload["max"] = self.max
        if self.enum:
            payload["enum"] = list(self.enum)
        if self.requires_secret is not None:
            payload["requires_secret"] = self.requires_secret
        if self.description is not None:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True)
class OperationSchema:
    name: str
    supported: bool = True
    requires_live_network_confirmation: bool = False
    params: dict[str, ParameterSchema] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "supported": self.supported,
            "requires_live_network_confirmation": self.requires_live_network_confirmation,
            "params": {key: value.to_dict() for key, value in sorted(self.params.items())},
        }


@dataclass(frozen=True)
class SettingSchema:
    type: str
    default: Any | None = None
    required: bool = False
    min: int | float | None = None
    max: int | float | None = None
    enum: tuple[str, ...] = ()
    editable: bool = True
    visible: bool = True
    secret: bool = False
    description: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "required": self.required,
            "editable": self.editable,
            "visible": self.visible,
            "secret": self.secret,
        }
        if self.default is not None:
            payload["default"] = self.default
        if self.min is not None:
            payload["min"] = self.min
        if self.max is not None:
            payload["max"] = self.max
        if self.enum:
            payload["enum"] = list(self.enum)
        if self.description is not None:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True)
class OutputSchema:
    fields: tuple[str, ...] = ()
    default_folder_template: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"fields": list(self.fields)}
        if self.default_folder_template is not None:
            payload["default_folder_template"] = self.default_folder_template
        return payload


@dataclass(frozen=True)
class NetworkPolicySchema:
    max_concurrency: int = 2
    min_interval_seconds: float = 0.0
    retryable_status_codes: tuple[int, ...] = (429,)
    max_attempts: int = 5
    max_backoff_seconds: float = 300.0

    def to_dict(self) -> dict[str, object]:
        return {
            "max_concurrency": self.max_concurrency,
            "min_interval_seconds": self.min_interval_seconds,
            "retryable_status_codes": list(self.retryable_status_codes),
            "max_attempts": self.max_attempts,
            "max_backoff_seconds": self.max_backoff_seconds,
        }


@dataclass(frozen=True)
class BrowserAuthSchema:
    login_url: str
    cookie_domains: tuple[str, ...]
    required_cookies: tuple[str, ...] = ()
    include_local_storage: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "login_url": self.login_url,
            "cookie_domains": list(self.cookie_domains),
            "required_cookies": list(self.required_cookies),
            "include_local_storage": self.include_local_storage,
        }


@dataclass(frozen=True)
class UrlRule:
    name: str
    pattern: str
    input_kind: InputKind
    operation: str
    description: str | None = None

    def match(self, url: str) -> dict[str, str] | None:
        match = re.match(self.pattern, url.strip(), re.IGNORECASE)
        if match is None:
            return None
        return {key: value for key, value in match.groupdict().items() if value is not None}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "pattern": self.pattern,
            "input_kind": self.input_kind.value,
            "operation": self.operation,
        }
        if self.description is not None:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True)
class ManifestRoute:
    site_id: str
    display_name: str
    rule_name: str
    input_kind: InputKind
    operation: str
    operation_schema: OperationSchema | None
    captures: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        required_confirmations: list[str] = []
        operation_supported = False
        params: dict[str, object] = {}
        if self.operation_schema is not None:
            operation_supported = self.operation_schema.supported
            params = {
                key: value.to_dict() for key, value in sorted(self.operation_schema.params.items())
            }
            if self.operation_schema.requires_live_network_confirmation:
                required_confirmations.append("live_network")

        return {
            "site_id": self.site_id,
            "display_name": self.display_name,
            "rule_name": self.rule_name,
            "input_kind": self.input_kind.value,
            "operation": self.operation,
            "operation_supported": operation_supported,
            "captures": dict(sorted(self.captures.items())),
            "required_confirmations": required_confirmations,
            "params": params,
        }


@dataclass(frozen=True)
class SiteManifest:
    site_id: str
    display_name: str
    domains: tuple[str, ...]
    url_rules: tuple[UrlRule, ...]
    operations: dict[str, OperationSchema] = field(default_factory=dict)
    settings: dict[str, SettingSchema] = field(default_factory=dict)
    secrets: dict[str, SettingSchema] = field(default_factory=dict)
    output: OutputSchema = field(default_factory=OutputSchema)
    network: NetworkPolicySchema | None = None
    browser_auth: BrowserAuthSchema | None = None
    version: str = "1"

    def route_url(self, url: str) -> ManifestRoute | None:
        for rule in self.url_rules:
            captures = rule.match(url)
            if captures is None:
                continue
            return ManifestRoute(
                site_id=self.site_id,
                display_name=self.display_name,
                rule_name=rule.name,
                input_kind=rule.input_kind,
                operation=rule.operation,
                operation_schema=self.operations.get(rule.operation),
                captures=captures,
            )
        return None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "site_id": self.site_id,
            "display_name": self.display_name,
            "version": self.version,
            "domains": list(self.domains),
            "url_rules": [rule.to_dict() for rule in self.url_rules],
            "operations": {key: value.to_dict() for key, value in sorted(self.operations.items())},
            "settings": {key: value.to_dict() for key, value in sorted(self.settings.items())},
            "secrets": {key: value.to_dict() for key, value in sorted(self.secrets.items())},
            "output": self.output.to_dict(),
        }
        if self.network is not None:
            payload["network"] = self.network.to_dict()
        if self.browser_auth is not None:
            payload["browser_auth"] = self.browser_auth.to_dict()
        return payload


class SiteManifestProvider(Protocol):
    def get_manifest(self) -> SiteManifest: ...


def site_manifest_from_json_file(path: str | Path) -> SiteManifest:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginValidationError(
            f"Cannot read site manifest JSON in {manifest_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PluginValidationError(
            f"Invalid site manifest JSON in {manifest_path}: {exc.msg}"
        ) from exc
    return site_manifest_from_dict(payload, source=str(manifest_path))


def site_manifest_from_dict(
    payload: Mapping[str, Any],
    *,
    source: str = "site manifest",
) -> SiteManifest:
    data = _require_mapping(payload, source)
    operations = _operation_map_from_dict(
        _optional_mapping(data, "operations", source),
        f"{source}.operations",
    )
    url_rules = tuple(
        _url_rule_from_dict(item, f"{source}.url_rules[{index}]")
        for index, item in enumerate(_require_list(data, "url_rules", source))
    )
    for rule in url_rules:
        if rule.operation not in operations:
            raise PluginValidationError(
                f"{source}.url_rules references unknown operation: {rule.operation}"
            )

    domains = tuple(_require_string_list(data, "domains", source, allow_empty=False))
    browser_auth = (
        _browser_auth_from_dict(data["browser_auth"], domains, f"{source}.browser_auth")
        if "browser_auth" in data
        else None
    )
    return SiteManifest(
        site_id=_require_identifier(data, "site_id", source),
        display_name=_require_non_empty_string(data, "display_name", source),
        version=_optional_non_empty_string(data, "version", source, default="1"),
        domains=domains,
        url_rules=url_rules,
        operations=operations,
        settings=_setting_map_from_dict(
            _optional_mapping(data, "settings", source),
            f"{source}.settings",
            secret=False,
        ),
        secrets=_setting_map_from_dict(
            _optional_mapping(data, "secrets", source),
            f"{source}.secrets",
            secret=True,
        ),
        output=_output_from_dict(
            _optional_mapping(data, "output", source),
            f"{source}.output",
        ),
        network=(
            _network_from_dict(data["network"], f"{source}.network") if "network" in data else None
        ),
        browser_auth=browser_auth,
    )


def _browser_auth_from_dict(
    payload: Any,
    site_domains: tuple[str, ...],
    source: str,
) -> BrowserAuthSchema:
    data = _require_mapping(payload, source)
    login_url = _require_non_empty_string(data, "login_url", source)
    parsed = urlsplit(login_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PluginValidationError(f"{source}.login_url must be an absolute HTTPS URL")
    cookie_domains = tuple(_require_string_list(data, "cookie_domains", source, allow_empty=False))
    if not all(_domain_belongs_to_site(domain, site_domains) for domain in cookie_domains):
        raise PluginValidationError(f"{source}.cookie_domains must belong to the Plugin site")
    if not _domain_belongs_to_site(parsed.hostname, cookie_domains):
        raise PluginValidationError(f"{source}.login_url must use an allowed cookie domain")
    required_cookies = tuple(_optional_string_list(data, "required_cookies", source))
    if len(set(required_cookies)) != len(required_cookies):
        raise PluginValidationError(f"{source}.required_cookies must not contain duplicates")
    return BrowserAuthSchema(
        login_url=login_url,
        cookie_domains=cookie_domains,
        required_cookies=required_cookies,
        include_local_storage=_optional_bool(
            data,
            "include_local_storage",
            source,
            default=False,
        ),
    )


def _domain_belongs_to_site(domain: str, site_domains: tuple[str, ...]) -> bool:
    hostname = domain.lstrip(".").lower()
    return any(
        hostname == site_domain.lstrip(".").lower()
        or hostname.endswith(f".{site_domain.lstrip('.').lower()}")
        for site_domain in site_domains
    )


def _network_from_dict(payload: Any, source: str) -> NetworkPolicySchema:
    data = _require_mapping(payload, source)
    max_concurrency = _optional_positive_int(data, "max_concurrency", source, default=2)
    min_interval = _optional_non_negative_number(data, "min_interval_seconds", source, default=0.0)
    max_attempts = _optional_positive_int(data, "max_attempts", source, default=5)
    max_backoff = _optional_positive_number(data, "max_backoff_seconds", source, default=300.0)
    statuses = tuple(_optional_int_list(data, "retryable_status_codes", source, default=(429,)))
    if not statuses or any(status < 100 or status > 599 for status in statuses):
        raise PluginValidationError(
            f"{source}.retryable_status_codes must contain HTTP status codes"
        )
    return NetworkPolicySchema(
        max_concurrency=max_concurrency,
        min_interval_seconds=min_interval,
        retryable_status_codes=statuses,
        max_attempts=max_attempts,
        max_backoff_seconds=max_backoff,
    )


def _output_from_dict(payload: Mapping[str, Any], source: str) -> OutputSchema:
    fields = tuple(_optional_string_list(payload, "fields", source))
    if len(set(fields)) != len(fields):
        raise PluginValidationError(f"{source}.fields must not contain duplicates")
    for name in fields:
        _require_identifier_key(name, f"{source}.fields")
        if name in COMMON_OUTPUT_FIELDS:
            raise PluginValidationError(f"{source}.fields cannot redeclare core field: {name}")
    template = _optional_string(payload, "default_folder_template", source)
    if template is not None:
        try:
            validate_folder_template(template, fields)
        except Exception as error:
            raise PluginValidationError(f"{source}.default_folder_template: {error}") from error
    return OutputSchema(fields=fields, default_folder_template=template)


def _operation_map_from_dict(
    payload: Mapping[str, Any],
    source: str,
) -> dict[str, OperationSchema]:
    return {
        _require_identifier_key(name, source): _operation_from_dict(value, f"{source}.{name}")
        for name, value in payload.items()
    }


def _operation_from_dict(payload: Any, source: str) -> OperationSchema:
    data = _require_mapping(payload, source)
    params = _optional_mapping(data, "params", source)
    return OperationSchema(
        name=_optional_non_empty_string(data, "name", source, default=source.rsplit(".", 1)[-1]),
        supported=_optional_bool(data, "supported", source, default=True),
        requires_live_network_confirmation=_optional_bool(
            data,
            "requires_live_network_confirmation",
            source,
            default=False,
        ),
        params={
            _require_identifier_key(name, f"{source}.params"): _parameter_from_dict(
                value,
                f"{source}.params.{name}",
            )
            for name, value in params.items()
        },
    )


def _parameter_from_dict(payload: Any, source: str) -> ParameterSchema:
    data = _require_mapping(payload, source)
    return ParameterSchema(
        type=_require_non_empty_string(data, "type", source),
        default=data.get("default"),
        required=_optional_bool(data, "required", source, default=False),
        min=_optional_number(data, "min", source),
        max=_optional_number(data, "max", source),
        enum=tuple(_optional_string_list(data, "enum", source)),
        editable=_optional_bool(data, "editable", source, default=True),
        visible=_optional_bool(data, "visible", source, default=True),
        requires_secret=_optional_string(data, "requires_secret", source),
        description=_optional_string(data, "description", source),
    )


def _setting_map_from_dict(
    payload: Mapping[str, Any],
    source: str,
    *,
    secret: bool,
) -> dict[str, SettingSchema]:
    return {
        _require_dotted_key(name, source): _setting_from_dict(
            value,
            f"{source}.{name}",
            secret=secret,
        )
        for name, value in payload.items()
    }


def _setting_from_dict(payload: Any, source: str, *, secret: bool) -> SettingSchema:
    data = _require_mapping(payload, source)
    return SettingSchema(
        type=_require_non_empty_string(data, "type", source),
        default=data.get("default"),
        required=_optional_bool(data, "required", source, default=False),
        min=_optional_number(data, "min", source),
        max=_optional_number(data, "max", source),
        enum=tuple(_optional_string_list(data, "enum", source)),
        editable=_optional_bool(data, "editable", source, default=True),
        visible=_optional_bool(data, "visible", source, default=True),
        secret=_optional_bool(data, "secret", source, default=secret),
        description=_optional_string(data, "description", source),
    )


def _url_rule_from_dict(payload: Any, source: str) -> UrlRule:
    data = _require_mapping(payload, source)
    pattern = _require_non_empty_string(data, "pattern", source)
    try:
        re.compile(pattern)
    except re.error as exc:
        raise PluginValidationError(f"{source}.pattern is not a valid regex") from exc

    input_kind_value = _require_non_empty_string(data, "input_kind", source)
    try:
        input_kind = InputKind(input_kind_value)
    except ValueError as exc:
        raise PluginValidationError(
            f"{source}.input_kind must be one of: {', '.join(kind.value for kind in InputKind)}"
        ) from exc

    return UrlRule(
        name=_require_identifier(data, "name", source),
        pattern=pattern,
        input_kind=input_kind,
        operation=_require_identifier(data, "operation", source),
        description=_optional_string(data, "description", source),
    )


def _require_mapping(payload: Any, source: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise PluginValidationError(f"{source} must be an object")
    return payload


def _optional_mapping(
    data: Mapping[str, Any],
    key: str,
    source: str,
) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise PluginValidationError(f"{source}.{key} must be an object")
    return value


def _require_list(data: Mapping[str, Any], key: str, source: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise PluginValidationError(f"{source}.{key} must be a list")
    return value


def _require_string_list(
    data: Mapping[str, Any],
    key: str,
    source: str,
    *,
    allow_empty: bool,
) -> list[str]:
    values = _require_list(data, key, source)
    if not allow_empty and not values:
        raise PluginValidationError(f"{source}.{key} must not be empty")
    for index, value in enumerate(values):
        if not isinstance(value, str) or value.strip() == "":
            raise PluginValidationError(f"{source}.{key}[{index}] must be a string")
    return values


def _optional_string_list(
    data: Mapping[str, Any],
    key: str,
    source: str,
) -> list[str]:
    if key not in data:
        return []
    value = data[key]
    if not isinstance(value, list):
        raise PluginValidationError(f"{source}.{key} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, str) or item.strip() == "":
            raise PluginValidationError(f"{source}.{key}[{index}] must be a string")
    return value


def _require_non_empty_string(
    data: Mapping[str, Any],
    key: str,
    source: str,
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise PluginValidationError(f"{source}.{key} must be a non-empty string")
    return value


def _optional_non_empty_string(
    data: Mapping[str, Any],
    key: str,
    source: str,
    *,
    default: str,
) -> str:
    if key not in data:
        return default
    return _require_non_empty_string(data, key, source)


def _optional_string(
    data: Mapping[str, Any],
    key: str,
    source: str,
) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str) or value.strip() == "":
        raise PluginValidationError(f"{source}.{key} must be a string")
    return value


def _optional_bool(
    data: Mapping[str, Any],
    key: str,
    source: str,
    *,
    default: bool,
) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise PluginValidationError(f"{source}.{key} must be a boolean")
    return value


def _optional_positive_int(data: Mapping[str, Any], key: str, source: str, *, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PluginValidationError(f"{source}.{key} must be a positive integer")
    return value


def _optional_non_negative_number(
    data: Mapping[str, Any], key: str, source: str, *, default: float
) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise PluginValidationError(f"{source}.{key} must be a non-negative number")
    return float(value)


def _optional_positive_number(
    data: Mapping[str, Any], key: str, source: str, *, default: float
) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PluginValidationError(f"{source}.{key} must be a positive number")
    return float(value)


def _optional_int_list(
    data: Mapping[str, Any],
    key: str,
    source: str,
    *,
    default: tuple[int, ...],
) -> list[int]:
    value = data.get(key, list(default))
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise PluginValidationError(f"{source}.{key} must be a list of integers")
    return value


def _optional_number(
    data: Mapping[str, Any],
    key: str,
    source: str,
) -> int | float | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PluginValidationError(f"{source}.{key} must be a number")
    return value


def _require_identifier(data: Mapping[str, Any], key: str, source: str) -> str:
    return _require_identifier_key(_require_non_empty_string(data, key, source), f"{source}.{key}")


def _require_identifier_key(value: str, source: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        raise PluginValidationError(
            f"{source} must use lowercase letters, numbers, and underscores"
        )
    return value


def _require_dotted_key(value: str, source: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", value):
        raise PluginValidationError(f"{source} keys must use lowercase dotted identifiers")
    return value
