from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

import httpx
from postgrest.exceptions import APIError

from .config import get_settings


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


def probe_database_table(client: Client, table_name: str) -> dict:
    try:
        client.table(table_name).select("id").limit(1).execute()
        return {"status": "ready"}
    except DatabaseUnavailableError as exc:
        return {"status": "unavailable", "code": exc.code, "message": str(exc)}
    except httpx.TransportError as exc:
        error = DatabaseUnavailableError(exc)
        return {"status": "unavailable", "code": error.code, "message": str(error)}
    except APIError as exc:
        code = str(exc.code)
        if code in {"PGRST205", "42P01"}:
            return {"status": "missing", "code": code}
        return {
            "status": "unavailable",
            "code": code if code.isalnum() and len(code) <= 12 else "database_query_failed",
            "message": "Database query failed. Check project credentials, permissions and schema.",
        }
    except Exception:
        return {
            "status": "unavailable", "code": "database_query_failed",
            "message": "Database query failed; table availability could not be verified.",
        }


_TRANSIENT_DATA_TOKENS = (
    "server disconnected",
    "remoteprotocolerror",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "eof",
    "ssl",
    "tls",
    "network",
    "domain lookup unavailable",
    "name or service not known",
    "getaddrinfo",
)


def is_transient_data_error(exc: Exception | str | None) -> bool:
    if isinstance(exc, DatabaseUnavailableError):
        return True
    text = str(exc or "").lower()
    return any(token in text for token in _TRANSIENT_DATA_TOKENS)


def user_facing_data_message(
    exc: Exception | str | None = None,
    *,
    default: str = "后台数据暂时不可用，请稍后刷新。",
) -> str:
    if is_transient_data_error(exc):
        return "后台数据连接暂时异常，请稍后重试。"
    return default


def get_supabase():
    settings = get_settings()
    transport = create_database_http_client(settings)
    try:
        client: Client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
            options=SyncClientOptions(httpx_client=transport),
        )
        yield client
    finally:
        transport.close()
