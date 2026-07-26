from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from time import time
from urllib.parse import urlsplit


def serialize_browser_session(
    state: Mapping[str, object],
    *,
    allowed_domains: Sequence[str],
    include_local_storage: bool = False,
) -> str:
    cookies = state.get("cookies", ())
    if not isinstance(cookies, Sequence) or isinstance(cookies, (str, bytes)):
        raise ValueError("browser session cookies must be a list")
    filtered = [
        _normalized_cookie(cookie)
        for cookie in cookies
        if isinstance(cookie, Mapping)
        and _domain_is_allowed(str(cookie.get("domain", "")), allowed_domains)
    ]
    payload: dict[str, object] = {"cookies": filtered}
    if include_local_storage:
        origins = state.get("origins", ())
        if not isinstance(origins, Sequence) or isinstance(origins, (str, bytes)):
            raise ValueError("browser session origins must be a list")
        payload["origins"] = [
            dict(origin)
            for origin in origins
            if isinstance(origin, Mapping)
            and _url_host_is_allowed(str(origin.get("origin", "")), allowed_domains)
        ]
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def browser_session_cookies(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, str):
        raise ValueError("browser session must be a JSON string")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("browser session is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("browser session must be an object")
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        raise ValueError("browser session cookies must be a list")
    return tuple(_normalized_cookie(cookie) for cookie in cookies)


def browser_session_has_cookies(value: object, required_names: Sequence[str]) -> bool:
    cookies = browser_session_cookies(value)
    active_names = {
        str(cookie["name"])
        for cookie in cookies
        if float(cookie.get("expires", -1)) <= 0 or float(cookie["expires"]) > time()
    }
    return bool(active_names) and set(required_names).issubset(active_names)


def cookie_header_for_url(value: object, url: str) -> str | None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    now = time()
    matches: list[dict[str, object]] = []
    for cookie in browser_session_cookies(value):
        domain = str(cookie["domain"]).lower()
        cookie_path = str(cookie["path"])
        expires = float(cookie.get("expires", -1))
        if not _cookie_matches_host(domain, hostname):
            continue
        if not _cookie_path_matches(cookie_path, path) or (
            cookie.get("secure") and parsed.scheme != "https"
        ):
            continue
        if expires > 0 and expires <= now:
            continue
        matches.append(cookie)
    if not matches:
        return None
    matches.sort(key=lambda cookie: len(str(cookie["path"])), reverse=True)
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in matches)


def _normalized_cookie(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("browser session cookie must be an object")
    required = ("name", "value", "domain", "path")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ValueError("browser session cookie is missing a required string field")
    cookie: dict[str, object] = {key: value[key] for key in required}
    expires = value.get("expires", -1)
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        raise ValueError("browser session cookie expires must be a number")
    cookie["expires"] = expires
    for key in ("httpOnly", "secure"):
        flag = value.get(key, False)
        if not isinstance(flag, bool):
            raise ValueError(f"browser session cookie {key} must be a boolean")
        cookie[key] = flag
    same_site = value.get("sameSite", "Lax")
    if same_site not in {"Strict", "Lax", "None"}:
        raise ValueError("browser session cookie sameSite is invalid")
    cookie["sameSite"] = same_site
    partition_key = value.get("partitionKey")
    if isinstance(partition_key, str):
        cookie["partitionKey"] = partition_key
    return cookie


def _domain_is_allowed(domain: str, allowed_domains: Sequence[str]) -> bool:
    hostname = domain.lstrip(".").lower()
    return any(
        hostname == allowed.lstrip(".").lower()
        or hostname.endswith(f".{allowed.lstrip('.').lower()}")
        for allowed in allowed_domains
    )


def _url_host_is_allowed(url: str, allowed_domains: Sequence[str]) -> bool:
    return _domain_is_allowed(urlsplit(url).hostname or "", allowed_domains)


def _cookie_matches_host(domain: str, hostname: str) -> bool:
    normalized = domain.lstrip(".")
    return hostname == normalized or (
        domain.startswith(".") and hostname.endswith(f".{normalized}")
    )


def _cookie_path_matches(cookie_path: str, request_path: str) -> bool:
    return (
        request_path == cookie_path
        or request_path.startswith(cookie_path.rstrip("/") + "/")
        or (cookie_path.endswith("/") and request_path.startswith(cookie_path))
    )
