from __future__ import annotations

import json
import locale as system_locale
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from importlib import resources
from typing import Any, Iterator


DEFAULT_LOCALE = "en-US"
SYSTEM_LOCALE = "system"
SUPPORTED_LOCALES = ("en-US", "zh-TW", "zh-CN")
LANGUAGE_OPTIONS = (SYSTEM_LOCALE, *SUPPORTED_LOCALES)

_ACTIVE_TRANSLATOR: ContextVar["TranslationService | None"] = ContextVar(
    "cli_downloader_active_translator",
    default=None,
)
_STATUS_TRANSLATION_KEYS = {
    "pending": "status.waiting",
    "pending_resolution": "status.waiting",
    "resolving": "status.preparing",
    "running": "status.in_progress",
    "queued": "status.queued",
    "downloading": "status.downloading",
    "completed": "status.completed",
    "partially_completed": "status.partial_success",
    "failed": "status.failed",
    "resolution_failed": "status.failed",
    "package_rejected": "status.failed",
    "duplicate_skipped": "status.already_downloaded",
    "cancelled": "status.cancelled",
    "package_ready": "status.ready",
}


@dataclass(frozen=True)
class TranslationCatalog:
    locale: str
    values: dict[str, str]

    def get(self, key: str) -> str | None:
        return self.values.get(key)


class TranslationService:
    """Resolve stable UI keys through packaged locale catalogs."""

    def __init__(self, language: str = DEFAULT_LOCALE) -> None:
        self.requested_language = _normalize_locale(language) or DEFAULT_LOCALE
        self.locale = resolve_locale(self.requested_language)
        self._catalogs = {
            locale_name: _load_catalog(locale_name) for locale_name in SUPPORTED_LOCALES
        }

    def translate(self, key: str, **values: Any) -> str:
        text = self._catalogs[self.locale].get(key)
        if text is None and self.locale != DEFAULT_LOCALE:
            text = self._catalogs[DEFAULT_LOCALE].get(key)
        if text is None:
            text = key
        return text.format(**values) if values else text

    __call__ = translate

    @property
    def available_locales(self) -> tuple[str, ...]:
        return SUPPORTED_LOCALES


def current_translator() -> TranslationService:
    """Return the translator active for the current CLI invocation."""
    translator = _ACTIVE_TRANSLATOR.get()
    return translator if translator is not None else TranslationService()


@contextmanager
def translation_context(translator: TranslationService) -> Iterator[None]:
    token: Token[TranslationService | None] = _ACTIVE_TRANSLATOR.set(translator)
    try:
        yield
    finally:
        _ACTIVE_TRANSLATOR.reset(token)


def translate_status(value: str, translator: TranslationService | None = None) -> str:
    service = translator or current_translator()
    key = _STATUS_TRANSLATION_KEYS.get(value)
    return service(key) if key is not None else value


def resolve_locale(language: str | None, *, system_language: str | None = None) -> str:
    requested = _normalize_locale(language)
    if not requested or requested == SYSTEM_LOCALE:
        requested = _normalize_locale(system_language) or detect_system_locale()
    for candidate in _locale_candidates(requested):
        if candidate in SUPPORTED_LOCALES:
            return candidate
    return DEFAULT_LOCALE


def detect_system_locale() -> str:
    candidates = [
        os.environ.get("LC_ALL"),
        os.environ.get("LANG"),
        system_locale.getlocale()[0],
    ]
    for candidate in candidates:
        normalized = _normalize_locale(candidate)
        if normalized:
            return normalized
    return DEFAULT_LOCALE


def validate_catalogs() -> tuple[str, ...]:
    """Return keys missing from at least one packaged catalog."""
    catalogs = [_load_catalog(locale_name) for locale_name in SUPPORTED_LOCALES]
    required = set(catalogs[0].values)
    missing: list[str] = []
    for catalog in catalogs[1:]:
        missing.extend(
            f"{catalog.locale}:{key}" for key in sorted(required - set(catalog.values))
        )
    return tuple(missing)


def _load_catalog(locale_name: str) -> TranslationCatalog:
    package = resources.files("cli_downloader.cli.locales")
    payload = json.loads(package.joinpath(f"{locale_name}.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise ValueError(f"invalid translation catalog: {locale_name}")
    return TranslationCatalog(locale_name, dict(payload))


def _locale_candidates(language: str | None) -> tuple[str, ...]:
    normalized = _normalize_locale(language)
    if not normalized:
        return ()
    base = normalized.split("-", 1)[0]
    return (normalized,) if base == normalized else (normalized, base)


def _normalize_locale(language: str | None) -> str | None:
    if not language:
        return None
    value = language.strip().replace("_", "-")
    if not value:
        return None
    if value.lower() == SYSTEM_LOCALE:
        return SYSTEM_LOCALE
    parts = value.split("-")
    return "-".join((parts[0].lower(), *(part.upper() for part in parts[1:])))
