from functools import lru_cache

from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

import httpx

from app.config import get_settings


class DatabaseUnavailableError(RuntimeError):
    """Safe diagnostic that never embeds a URL, credentials, or database payload."""

    def __init__(self, cause: httpx.TransportError, *, method: str = "GET"):
        self.code = (
            "database_timeout"
            if isinstance(cause, httpx.TimeoutException)
            else "database_connection_failed"
        )
        self.retryable = method.upper() in {"GET", "HEAD"}
        message = "Database connection unavailable. Check the Supabase project URL, project status, DNS and proxy settings."
        if isinstance(cause, httpx.TimeoutException):
            message = "Database request timed out. Check connectivity and the configured timeout."
        if "certificate_verify_failed" in str(cause).lower():
            self.code = "database_tls_verification_failed"
            self.retryable = False
            message = "Database TLS certificate verification failed. Check the trusted CA configuration."
        if method.upper() not in {"GET", "HEAD"}:
            message += " Write success is unconfirmed; check the saved state before retrying."
        super().__init__(message)


class DatabaseHttpClient(httpx.Client):
    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        try:
            return super().send(request, **kwargs)
        except httpx.TransportError as exc:
            # Never replay a write: a dropped response may follow a committed transaction.
            raise DatabaseUnavailableError(exc, method=request.method) from exc


def database_error_from_exception(exc: BaseException) -> DatabaseUnavailableError | None:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, DatabaseUnavailableError):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def create_database_http_client(settings) -> DatabaseHttpClient:
    return DatabaseHttpClient(
        timeout=httpx.Timeout(settings.supabase_postgrest_timeout_sec),
        proxy=settings.supabase_http_proxy or None,
        trust_env=settings.supabase_trust_env,
        verify=True,
        http2=True,
    )


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    settings = get_settings()
    transport = create_database_http_client(settings)
    try:
        return create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
            options=SyncClientOptions(httpx_client=transport),
        )
    except Exception:
        transport.close()
        raise
