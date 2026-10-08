from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.db import database_error_from_exception

_RETRYABLE_HINTS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "server disconnected",
    "remoteprotocolerror",
)


def _trace_id() -> str:
    return f"tr_{uuid4().hex[:16]}"


def _is_retryable(message: str, status_code: int) -> bool:
    if status_code in {429, 502, 503, 504}:
        return True
    lowered = str(message or "").lower()
    return any(token in lowered for token in _RETRYABLE_HINTS)


def _envelope(*, code: str, message: str, retryable: bool, trace_id: str) -> dict:
    return {
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
            "trace_id": trace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }


def install_error_handlers(app) -> None:
    def database_response(exc: Exception):
        error = database_error_from_exception(exc)
        if error is None:
            return None
        return JSONResponse(
            status_code=503,
            content=_envelope(
                code=error.code, message=str(error),
                retryable=error.retryable, trace_id=_trace_id(),
            ),
        )

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(_: Request, exc: HTTPException):
        response = database_response(exc)
        if response is not None:
            return response
        trace_id = _trace_id()
        status_code = int(getattr(exc, "status_code", 500) or 500)
        message = str(getattr(exc, "detail", "") or "Request failed")
        retryable = _is_retryable(message, status_code)
        code = f"http_{status_code}"
        return JSONResponse(
            status_code=status_code,
            content=_envelope(code=code, message=message, retryable=retryable, trace_id=trace_id),
        )

    @app.exception_handler(Exception)
    async def _handle_exception(_: Request, exc: Exception):
        response = database_response(exc)
        if response is not None:
            return response
        trace_id = _trace_id()
        message = str(exc or "Internal server error")
        retryable = _is_retryable(message, 500)
        return JSONResponse(
            status_code=500,
            content=_envelope(code="internal_error", message=message, retryable=retryable, trace_id=trace_id),
        )
