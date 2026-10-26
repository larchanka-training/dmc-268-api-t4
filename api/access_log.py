"""The API's access log: one JSON line per request (docs/observability.md § Logs).

It replaces uvicorn's access log, which configure_logging silences. That log
printed the full path, and on the OAuth callback the path carries the
authorization code and state; this one logs the route template
(``/auth/{provider}/callback``), never the raw path or the query string. Every
line logged while a request is handled carries its ``request_id``, which the
response echoes as ``X-Request-ID``.
"""

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response

from adapters.observability.logging import bind_context

REQUEST_ID_HEADER = "X-Request-ID"
#: A caller's id is kept only when it is short and plain, so it cannot carry
#: content or break a log line; anything else gets a fresh one.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
#: No route matched (a 404), or the CSRF check answered before routing.
UNMATCHED_ROUTE = "<unmatched>"
#: The Docker healthcheck hits it every 10 s: logged at debug, so not in production.
_HEALTH_ROUTE = "/health"

Next = Callable[[Request], Awaitable[Response]]

logger = logging.getLogger("api.access")


def install_access_log(app: FastAPI) -> None:
    """Add the access-log middleware. Call it after every other middleware, so
    it is the outermost one and also logs the requests they answer themselves."""

    @app.middleware("http")
    async def access_log(request: Request, call_next: Next) -> Response:
        request_id = _request_id(request.headers.get(REQUEST_ID_HEADER))
        started = time.monotonic()
        status = 500  # what the client gets when the app raises
        with bind_context(request_id=request_id):
            try:
                response = await call_next(request)
                status = response.status_code
                response.headers[REQUEST_ID_HEADER] = request_id
                return response
            finally:
                _log_request(request, status, started)


def _request_id(header: str | None) -> str:
    if header is not None and _VALID_REQUEST_ID.fullmatch(header):
        return header
    return uuid.uuid4().hex


def _log_request(request: Request, status: int, started: float) -> None:
    # The router sets the matched route on the scope; its path is the template.
    route = getattr(request.scope.get("route"), "path", None) or UNMATCHED_ROUTE
    logger.log(
        logging.DEBUG if route == _HEALTH_ROUTE else logging.INFO,
        "http.request",
        extra={
            "http.method": request.method,
            "http.route": route,
            "http.status": status,
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
    )
