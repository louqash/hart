"""Request authentication and CSRF checks (docs/security.md).

* hart sits behind a reverse proxy that authenticates people and passes
  their identity in a header — by default Tailscale Serve, which adds
  ``Tailscale-User-Login`` only for requests from user-owned devices
  (``HART_AUTH_HEADER`` names another header, e.g. for oauth2-proxy).
  A request without it is rejected.  ``HART_ALLOWED_USERS`` optionally
  limits which identities get in.
* ``/mcp`` also accepts the internal bearer token used by the server's own
  Claude subprocess.
* ``/healthz`` is open (no data).
* Cross-site requests: a foreign ``Origin`` is rejected everywhere, and
  state-changing ``/api`` calls need ``X-Requested-With: hart`` plus a JSON
  body, which a cross-site page can't send without a CORS preflight.
* ``HART_ENV=dev`` skips the identity check (only allowed on 127.0.0.1).
"""

from __future__ import annotations

import hmac
import json
from typing import Any
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

OPEN_PATHS = frozenset({"/healthz"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def _deny(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _same_origin(origin: str, headers: dict[str, str], public_host: str) -> bool:
    origin_host = urlsplit(origin).netloc.lower()
    allowed = {headers.get("host", "").lower(), headers.get("x-forwarded-host", "").lower(), public_host}
    allowed.discard("")
    return origin_host in allowed


class AuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        dev: bool,
        internal_token: str,
        public_host: str = "",
        identity_header: str = "Tailscale-User-Login",
        allowed_users: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.identity_header = identity_header.lower()
        self.allowed_users = frozenset(u.lower() for u in allowed_users)
        self.dev = dev
        self.internal_token = internal_token
        # Serve may forward requests with Host: localhost:8765, so the public
        # hostname is configured explicitly for the Origin check.
        self.public_host = public_host.lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        method: str = scope["method"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}

        if path in OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        origin = headers.get("origin")
        if origin and not _same_origin(origin, headers, self.public_host):
            await _deny(send, 403, "cross_origin", "Cross-origin requests are not allowed.")
            return

        login: str | None = headers.get(self.identity_header)
        internal = path == "/mcp" and hmac.compare_digest(
            headers.get("authorization", ""), f"Bearer {self.internal_token}"
        )
        if internal:
            login = "internal"
        elif self.dev:
            login = login or "dev"
        elif not login:
            await _deny(send, 403, "no_identity", "Sign-in required (no identity header from the proxy).")
            return
        elif self.allowed_users and login.lower() not in self.allowed_users:
            await _deny(send, 403, "not_allowed", f"{login} isn't allowed to use this hart instance.")
            return

        if path.startswith("/api/") and method not in SAFE_METHODS:
            content_type = headers.get("content-type", "")
            if headers.get("x-requested-with") != "hart" or not content_type.startswith("application/json"):
                await _deny(
                    send,
                    403,
                    "csrf",
                    "State-changing requests need 'X-Requested-With: hart' and a JSON body.",
                )
                return

        state: dict[str, Any] = scope.setdefault("state", {})
        state["login"] = login
        await self.app(scope, receive, send)
