"""ASGI middleware: request correlation, access logging and optional API-key auth."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app import constants
from app.config import Settings
from app.core.errors import AuthError
from app.core.logging import get_logger, request_id_var

Handler = Callable[[Request], Awaitable[Response]]

logger = get_logger(__name__)

#: Paths that never require authentication, so probes keep working.
PUBLIC_PATHS = frozenset(
    {"/health", "/healthy", "/health/ready", "/ready", "/docs", "/redoc", "/openapi.json"}
)


async def request_context_middleware(request: Request, call_next: Handler) -> Response:
    """Assign a request id, time the request and log its outcome."""
    request_id = request.headers.get(constants.REQUEST_ID_HEADER) or uuid.uuid4().hex
    token = request_id_var.set(request_id)
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request.failed",
            method=request.method,
            path=request.url.path,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        raise
    finally:
        request_id_var.reset(token)

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers[constants.REQUEST_ID_HEADER] = request_id
    logger.info(
        "request.completed",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


def build_auth_middleware(settings: Settings) -> Callable[[Request, Handler], Awaitable[Response]]:
    """Build the API-key middleware for the given settings.

    When no keys are configured the middleware is a pass-through, so local and
    trusted-network deployments need no credentials at all.
    """

    async def auth_middleware(request: Request, call_next: Handler) -> Response:
        if not settings.auth_enabled or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        presented = request.headers.get("X-API-Key")
        if presented is None or presented not in settings.api_keys:
            error = AuthError(
                "Missing or invalid API key",
                detail="Supply a valid key via the X-API-Key header.",
                instance=request.url.path,
            )
            return JSONResponse(
                status_code=error.status,
                content=error.to_problem(),
                media_type="application/problem+json",
            )
        return await call_next(request)

    return auth_middleware
