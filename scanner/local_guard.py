"""Reject browser requests that did not originate from this scanner's machine.

CORS prevents reading cross-origin responses, not simple cross-origin writes or
DNS rebinding. This ASGI guard covers both HTTP and WebSocket requests.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_WILDCARDS = frozenset({"", "0.0.0.0", "::"})
_extra_hosts: set[str] = set()


def hostname(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else ""
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def allow_hosts(*names: str) -> None:
    """Allow the explicit LAN hostname/IP selected by --host."""
    for name in names:
        host = hostname(name)
        if host not in _WILDCARDS:
            _extra_hosts.add(host)


def allowed_hosts() -> set[str]:
    extra = os.environ.get("SCANNER_ALLOWED_HOSTS", "")
    return set(LOOPBACK_HOSTS) | _extra_hosts | {
        hostname(name) for name in extra.split(",") if hostname(name) not in _WILDCARDS
    }


def refusal(kind: str, method: str, headers: dict[str, str]) -> tuple[int, str] | None:
    host = headers.get("host")
    if host and hostname(host) not in allowed_hosts():
        return 400, "host not allowed"
    if kind == "websocket" or method in _UNSAFE_METHODS:
        origin = headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin.strip())
                valid = parsed.scheme in ("http", "https") and parsed.hostname in allowed_hosts()
            except ValueError:
                valid = False
            if not valid:
                return 403, "origin not allowed"
    if method == "POST":
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return 415, "POST needs Content-Type: application/json"
    return None


class LocalOnlyMiddleware:
    """Pure ASGI middleware also protects WebSocket handshakes."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                   for key, value in scope.get("headers") or []}
        blocked = refusal(kind, scope.get("method", "GET").upper(), headers)
        if blocked is None:
            await self.app(scope, receive, send)
            return
        status, reason = blocked
        if kind == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008, "reason": reason})
        else:
            await JSONResponse({"error": reason}, status_code=status)(scope, receive, send)
