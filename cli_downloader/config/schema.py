from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from cli_downloader.core.browser_auth import browser_session_cookies, browser_session_has_cookies
from cli_downloader.core.errors import ConfigValidationError
from cli_downloader.core.output import validate_folder_template
from cli_downloader.sites.manifest import BrowserAuthSchema, SettingSchema, SiteManifest


Validator = Callable[[Any], bool]


@dataclass(frozen=True)
class ConfigField:
    key: str
    default: Any
    description: str
    type: str = "string"
    editable: bool = True
    visible: bool = True
    secret: bool = False
    enum: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    validator: Validator | None = None
    exposed_to_plugins: bool = False

    def validate(self, value: Any) -> None:
        if self.validator and not self.validator(value):
            raise ConfigValidationError(f"invalid config value for {self.key!r}: {value!r}")


@dataclass(frozen=True)
class ConfigSchema:
    fields: tuple[ConfigField, ...]

    def defaults(self) -> dict[str, Any]:
        return {field.key: field.default for field in self.fields}

    def validate(self, values: dict[str, Any]) -> None:
        known = {field.key: field for field in self.fields}
        for key, value in values.items():
            if key in known:
                known[key].validate(value)


@dataclass(frozen=True)
class PluginSettingsContext:
    settings: dict[str, Any]
    secret_refs: dict[str, str]


class SettingsRegistry:
    def __init__(self, global_schema: ConfigSchema | None = None) -> None:
        self._global_schema = global_schema or default_schema()
        self._plugin_fields: dict[str, tuple[ConfigField, ...]] = {}
        self._plugin_secret_fields: dict[str, tuple[ConfigField, ...]] = {}
        self._plugin_secrets: dict[str, dict[str, SettingSchema]] = {}
        self._browser_auth_fields: dict[str, BrowserAuthSchema] = {}

    @property
    def schema(self) -> ConfigSchema:
        fields = [*self._global_schema.fields]
        for plugin_fields in self._plugin_fields.values():
            fields.extend(plugin_fields)
        return ConfigSchema(tuple(fields))

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        return tuple(self._plugin_fields)

    @property
    def settings_fields(self) -> tuple[ConfigField, ...]:
        fields = [*self._global_schema.fields]
        for plugin_id in self._plugin_fields:
            fields.extend(self._plugin_fields[plugin_id])
            fields.extend(self._plugin_secret_fields[plugin_id])
        return tuple(fields)

    def field(self, key: str) -> ConfigField:
        for field in self.settings_fields:
            if field.key == key:
                return field
        raise ConfigValidationError(f"unknown setting: {key}")

    def browser_auth(self, key: str) -> BrowserAuthSchema:
        try:
            return self._browser_auth_fields[key]
        except KeyError as error:
            raise ConfigValidationError(f"setting is not browser authentication: {key}") from error

    def register_plugin(self, plugin_id: str, manifest: SiteManifest) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", plugin_id):
            raise ConfigValidationError(f"invalid plugin setting namespace: {plugin_id!r}")
        if plugin_id in self._plugin_fields:
            raise ConfigValidationError(f"plugin settings already registered: {plugin_id}")
        prefix = f"plugins.{plugin_id}."
        fields = tuple(
            ConfigField(
                key=f"{prefix}{key}",
                default=setting.default,
                description=setting.description or key,
                type=setting.type,
                editable=setting.editable,
                visible=setting.visible,
                enum=setting.enum,
                validator=_setting_validator(setting),
            )
            for key, setting in manifest.settings.items()
        )
        if manifest.output.fields or manifest.output.default_folder_template is not None:
            if "download.folder_template" in manifest.settings:
                raise ConfigValidationError(
                    "plugin output template must be declared through manifest.output"
                )
            fields += (
                ConfigField(
                    key=f"{prefix}download.folder_template",
                    default=None,
                    description="Override this Plugin's Gallery folder template.",
                    validator=lambda value: (
                        value is None or validate_folder_template(value, manifest.output.fields)
                    ),
                ),
            )
        if manifest.network is not None:
            network = manifest.network
            fields += (
                ConfigField(
                    key=f"{prefix}network.max_concurrency",
                    default=network.max_concurrency,
                    description="Maximum concurrent requests to this site.",
                    type="integer",
                    validator=_positive_int,
                ),
                ConfigField(
                    key=f"{prefix}network.min_interval_seconds",
                    default=network.min_interval_seconds,
                    description="Minimum delay between requests to this site.",
                    type="number",
                    validator=_non_negative_number,
                ),
                ConfigField(
                    key=f"{prefix}network.max_attempts",
                    default=network.max_attempts,
                    description="Maximum attempts for retryable site requests.",
                    type="integer",
                    validator=_positive_int,
                ),
                ConfigField(
                    key=f"{prefix}network.max_backoff_seconds",
                    default=network.max_backoff_seconds,
                    description="Maximum fallback cooldown after a rejected request.",
                    type="number",
                    validator=_positive_number,
                ),
            )
        secret_schemas = dict(manifest.secrets)
        if manifest.browser_auth is not None:
            auth_key = "auth.session"
            if auth_key in secret_schemas:
                raise ConfigValidationError("browser authentication owns secret auth.session")
            secret_schemas[auth_key] = SettingSchema(
                type="browser_auth",
                secret=True,
                description="Connect or disconnect this website account.",
            )
            self._browser_auth_fields[f"{prefix}{auth_key}"] = manifest.browser_auth
        secret_fields = tuple(
            ConfigField(
                key=f"{prefix}{key}",
                default=None,
                description=setting.description or key,
                type=setting.type,
                editable=setting.editable,
                visible=setting.visible,
                secret=True,
                enum=setting.enum,
                validator=_setting_validator(setting),
            )
            for key, setting in secret_schemas.items()
        )
        for field in fields:
            if field.default is not None:
                field.validate(field.default)
        self._plugin_fields[plugin_id] = fields
        self._plugin_secret_fields[plugin_id] = secret_fields
        self._plugin_secrets[plugin_id] = secret_schemas

    def validate(self, values: dict[str, Any], secrets: dict[str, Any]) -> None:
        self.schema.validate(values)
        for plugin_id, schemas in self._plugin_secrets.items():
            prefix = f"plugins.{plugin_id}."
            for key, setting in schemas.items():
                value = secrets.get(f"{prefix}{key}")
                if value is None and not setting.required:
                    continue
                if not _setting_validator(setting)(value):
                    raise ConfigValidationError(f"invalid secret value for {prefix + key!r}")
        for key, auth in self._browser_auth_fields.items():
            value = secrets.get(key)
            if value is None:
                continue
            try:
                cookies = browser_session_cookies(value)
            except ValueError as error:
                raise ConfigValidationError(
                    f"invalid browser session for {key}: {error}"
                ) from error
            if any(
                not _auth_cookie_domain_allowed(str(cookie["domain"]), auth.cookie_domains)
                for cookie in cookies
            ):
                raise ConfigValidationError(f"browser session contains a disallowed domain: {key}")
            if not browser_session_has_cookies(value, auth.required_cookies):
                raise ConfigValidationError(f"browser session is expired or incomplete: {key}")

    def context(
        self,
        plugin_id: str,
        *,
        values: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> PluginSettingsContext:
        if plugin_id not in self._plugin_fields:
            raise ConfigValidationError(f"plugin settings are not registered: {plugin_id}")
        values = {**self.schema.defaults(), **(values or {})}
        secrets = secrets or {}
        self.validate(values, secrets)
        settings = {
            field.key: values[field.key]
            for field in self._global_schema.fields
            if field.exposed_to_plugins
        }
        prefix = f"plugins.{plugin_id}."
        settings.update(
            (field.key.removeprefix(prefix), values[field.key])
            for field in self._plugin_fields[plugin_id]
            if values[field.key] is not None
        )
        secret_refs = {
            key: f"secrets.{prefix}{key}"
            for key in self._plugin_secrets[plugin_id]
            if secrets.get(f"{prefix}{key}") is not None
        }
        return PluginSettingsContext(dict(settings), secret_refs)


def default_schema() -> ConfigSchema:
    return ConfigSchema(
        fields=(
            ConfigField(
                key="download.default_dir",
                default="downloads",
                description="Default output directory relative to the data root.",
                type="string",
                validator=lambda value: isinstance(value, str) and bool(value.strip()),
            ),
            ConfigField(
                key="interface.language",
                default="en-US",
                description="Language used for the application interface.",
                type="string",
                enum=("system", "en-US", "zh-TW", "zh-CN"),
                validator=lambda value: value in {"system", "en-US", "zh-TW", "zh-CN"},
            ),
            ConfigField(
                key="download.strategy",
                default="auto",
                description="Default download strategy: auto, direct, torrent, or api.",
                type="string",
                enum=("auto", "direct", "torrent", "api"),
                validator=lambda value: value in {"auto", "direct", "torrent", "api"},
                exposed_to_plugins=True,
            ),
            ConfigField(
                key="download.folder_template",
                default="{site_id}/{gallery_id}-{title}",
                description="Relative folder template for downloaded galleries.",
                type="string",
                validator=lambda value: isinstance(value, str) and bool(value.strip()),
            ),
            ConfigField(
                key="download.max_concurrent_galleries",
                default=2,
                description="Maximum number of galleries downloaded at the same time.",
                type="integer",
                validator=lambda value: (
                    isinstance(value, int) and not isinstance(value, bool) and value > 0
                ),
            ),
            ConfigField(
                key="scheduler.policy",
                default="round_robin",
                description="Item scheduling policy: round_robin or fifo.",
                type="string",
                enum=("round_robin", "fifo"),
                validator=lambda value: value in {"round_robin", "fifo"},
            ),
            ConfigField(
                key="scheduler.max_concurrent_downloads",
                default=2,
                description="Maximum active Item downloads across all sites.",
                type="integer",
                validator=_positive_int,
            ),
            ConfigField(
                key="scheduler.max_concurrent_downloads_per_site",
                default=2,
                description="Fallback maximum active Item downloads for one site.",
                type="integer",
                validator=_positive_int,
            ),
            ConfigField(
                key="torrent.client",
                default="none",
                description="Torrent client: none or qbittorrent.",
                type="string",
                enum=("none", "qbittorrent"),
                validator=lambda value: value in {"none", "qbittorrent"},
                exposed_to_plugins=True,
            ),
            ConfigField(
                key="torrent.poll_interval_seconds",
                default=10,
                description="Seconds between torrent progress polls.",
                type="integer",
                validator=_positive_int,
            ),
            ConfigField(
                key="torrent.auto_delete_torrent_file",
                default=True,
                description="Delete .torrent file after adding to client.",
                type="boolean",
                validator=lambda value: isinstance(value, bool),
            ),
            ConfigField(
                key="torrent.qbittorrent.url",
                default="http://localhost:8080",
                description="qBittorrent Web API base URL.",
                type="string",
                validator=lambda value: isinstance(value, str) and bool(value.strip()),
            ),
            ConfigField(
                key="torrent.qbittorrent.username",
                default="admin",
                description="qBittorrent Web UI username.",
                type="string",
                validator=lambda value: isinstance(value, str),
            ),
            ConfigField(
                key="torrent.qbittorrent.password",
                default=None,
                description="qBittorrent Web UI password (stored in secrets.toml).",
                type="string",
                secret=True,
                validator=lambda value: value is None or isinstance(value, str),
            ),
            ConfigField(
                key="network.max_concurrent_requests",
                default=2,
                description="Fallback maximum concurrent requests per site.",
                type="integer",
                validator=_positive_int,
            ),
            ConfigField(
                key="network.min_request_interval_seconds",
                default=0.5,
                description="Fallback minimum delay between requests to a site.",
                type="number",
                validator=_non_negative_number,
            ),
            ConfigField(
                key="network.max_attempts",
                default=5,
                description="Fallback maximum attempts for retryable requests.",
                type="integer",
                validator=_positive_int,
            ),
            ConfigField(
                key="tasks.auto_delete_on_complete",
                default=False,
                description="Automatically delete task records when a download completes successfully.",
                type="boolean",
                enum=("true", "false"),
                validator=lambda value: isinstance(value, bool),
            ),
            ConfigField(
                key="network.max_backoff_seconds",
                default=300.0,
                description="Fallback maximum cooldown when Retry-After is absent.",
                type="number",
                validator=_positive_number,
            ),
            ConfigField(
                key="profile.default",
                default="default",
                description="Default user profile name.",
                type="string",
                validator=lambda value: isinstance(value, str) and bool(value.strip()),
            ),
        )
    )


def _setting_validator(schema: SettingSchema) -> Validator:
    def validate(value: Any) -> bool:
        if value is None:
            return not schema.required
        valid_type = {
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "boolean": lambda item: isinstance(item, bool),
            "browser_auth": lambda item: isinstance(item, str),
        }.get(schema.type)
        if valid_type is None or not valid_type(value):
            return False
        if schema.enum and value not in schema.enum:
            return False
        if schema.min is not None and (not isinstance(value, (int, float)) or value < schema.min):
            return False
        if schema.max is not None and (not isinstance(value, (int, float)) or value > schema.max):
            return False
        return True

    return validate


def _auth_cookie_domain_allowed(domain: str, allowed_domains: tuple[str, ...]) -> bool:
    hostname = domain.lstrip(".").lower()
    return any(
        hostname == allowed.lstrip(".").lower()
        or hostname.endswith(f".{allowed.lstrip('.').lower()}")
        for allowed in allowed_domains
    )


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _positive_number(value: Any) -> bool:
    return _non_negative_number(value) and value > 0
