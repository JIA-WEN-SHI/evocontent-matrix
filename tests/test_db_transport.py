import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import db
from app.config import Settings
from app.error_handling import install_error_handlers
from app.routers import system


def fake_settings(**overrides):
    return SimpleNamespace(
        **{
            "supabase_url": "https://example.supabase.co",
            "supabase_service_role_key": "test-secret",
            "supabase_postgrest_timeout_sec": 3,
            "supabase_http_proxy": "",
            "supabase_trust_env": False,
            **overrides,
        }
    )


def test_custom_transport_preserves_auth_timeout_and_closes(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=[{"id": "one"}])

    transport = httpx.MockTransport(respond)
    original_init = httpx.Client.__init__

    def init(self, **kwargs):
        assert kwargs["verify"] is True
        assert kwargs["trust_env"] is False
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    monkeypatch.setattr(db, "get_settings", fake_settings)
    dependency = db.get_supabase()
    client = next(dependency)
    assert client.table("domains").select("id").limit(1).execute().data == [{"id": "one"}]
    assert seen[0].headers["apikey"] == "test-secret"
    assert seen[0].headers["authorization"] == "Bearer test-secret"
    assert seen[0].extensions["timeout"]["read"] == 3
    dependency.close()
    assert client.postgrest.session.is_closed


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_failed_transport_is_redacted_and_never_replays_writes(method):
    attempts = []

    def fail(request):
        attempts.append(request)
        raise httpx.ReadError("TLS EOF https://user:password@example.test?apikey=secret")

    with db.DatabaseHttpClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(db.DatabaseUnavailableError) as caught:
            client.request(method, "https://example.supabase.co/rest/v1/domains")
    assert len(attempts) == 1
    assert caught.value.code == "database_connection_failed"
    assert caught.value.retryable is (method == "GET")
    assert "password" not in str(caught.value)
    assert "apikey" not in str(caught.value)


@pytest.mark.parametrize("wrapped", [False, True])
def test_write_errors_return_readable_503_even_when_wrapped(wrapped):
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/write")
    def write():
        try:
            raise db.DatabaseUnavailableError(httpx.ReadTimeout("secret"), method="POST")
        except db.DatabaseUnavailableError as exc:
            if wrapped:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            raise

    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.post("/write")
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "database_timeout"
    assert result.json()["error"]["retryable"] is False
    assert "secret" not in result.text


class FailingDatabase:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def table(self, _name):
        self.calls += 1
        return self

    def select(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        raise self.error


def test_readiness_does_not_misdiagnose_transport_failure_as_missing_schema(monkeypatch):
    client = FailingDatabase(httpx.ConnectError("TLS EOF secret"))

    async def agent_health(_self, *_args, **_kwargs):
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx.AsyncClient, "get", agent_health)
    result = asyncio.run(system.get_system_readiness(
        settings=SimpleNamespace(agent_service_url="http://agent"), client=client, _=None,
    ))
    assert result["status"] == "degraded"
    assert result["database"]["status"] == "unavailable"
    assert result["pipeline_mode"] == "unavailable"
    assert not result["core_ready"]
    assert client.calls == 1
    assert all(".sql" not in suggestion for suggestion in result["suggestions"])
    assert "secret" not in str(result)


def test_missing_table_stays_distinct_from_connection_failure():
    client = FailingDatabase(APIError({"code": "PGRST205", "message": "missing", "details": "", "hint": ""}))
    result = db.probe_database_table(client, "domains")
    assert result["status"] == "missing"
    assert result["code"] == "PGRST205"


def test_timeout_setting_rejects_nonpositive_values():
    with pytest.raises(ValueError):
        Settings(_env_file=None, SUPABASE_URL="https://example.supabase.co",
                 SUPABASE_SERVICE_ROLE_KEY="test", WEBHOOK_SHARED_SECRET="test",
                 PII_ENCRYPTION_KEY="test", SUPABASE_POSTGREST_TIMEOUT_SEC=0)


def test_certificate_failure_remains_fatal_without_insecure_fallback():
    attempts = []

    def fail(request):
        attempts.append(request)
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] secret")

    with db.DatabaseHttpClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(db.DatabaseUnavailableError) as caught:
            client.get("https://example.supabase.co")
    assert len(attempts) == 1
    assert caught.value.code == "database_tls_verification_failed"
    assert not caught.value.retryable
    assert "secret" not in str(caught.value)


def test_explicit_proxy_is_passed_without_changing_process_environment(monkeypatch):
    options = {}

    def capture(**kwargs):
        options.update(kwargs)
        return object()

    monkeypatch.setattr(db, "DatabaseHttpClient", capture)
    db.create_database_http_client(fake_settings(supabase_http_proxy="http://127.0.0.1:8080"))
    assert options["proxy"] == "http://127.0.0.1:8080"
    assert options["trust_env"] is False
    assert options["verify"] is True


def test_client_construction_failure_closes_owned_transport(monkeypatch):
    transport = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    def fail(*_args, **_kwargs):
        raise RuntimeError("configuration failed")

    monkeypatch.setattr(db, "get_settings", fake_settings)
    monkeypatch.setattr(db, "create_database_http_client", lambda _: transport)
    monkeypatch.setattr(db, "create_client", fail)
    with pytest.raises(RuntimeError, match="configuration failed"):
        next(db.get_supabase())
    assert transport.is_closed


def test_auth_failure_is_not_reported_as_missing_schema():
    client = FailingDatabase(APIError({"code": "42501", "message": "secret", "details": "", "hint": ""}))
    result = db.probe_database_table(client, "domains")
    assert result["status"] == "unavailable"
    assert result["code"] == "42501"
    assert "secret" not in str(result)


def test_empty_successful_read_is_a_ready_table():
    class EmptyDatabase(FailingDatabase):
        def execute(self):
            return SimpleNamespace(data=[])

    assert db.probe_database_table(EmptyDatabase(None), "domains") == {"status": "ready"}


def test_feature_checklist_does_not_recommend_migrations_during_outage(monkeypatch):
    async def agent_health(_self, *_args, **_kwargs):
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx.AsyncClient, "get", agent_health)
    result = asyncio.run(system.get_feature_checklist(
        settings=SimpleNamespace(agent_service_url="http://agent"),
        client=FailingDatabase(httpx.ConnectError("TLS EOF")), _=None,
    ))
    assert result["status"] == "degraded"
    assert result["summary"]["ok"] == 0
    assert result["summary"]["blocked"] == result["summary"]["total"]
    assert ".sql" not in str(result)


def test_readiness_stops_when_successful_queries_exhaust_time_budget(monkeypatch):
    class EmptyDatabase(FailingDatabase):
        def execute(self):
            return SimpleNamespace(data=[])

    times = iter([0, 0, 11])
    monkeypatch.setattr(system, "monotonic", lambda: next(times))
    client = EmptyDatabase(None)
    results = system._probe_tables(client)
    assert client.calls == 1
    assert results["domains"]["status"] == "ready"
    assert results["strategy_versions"]["code"] == "database_readiness_timeout"
    assert results["pipeline_tasks"]["status"] == "not_checked"


def test_readiness_bounds_each_io_phase_and_restores_client_timeout(monkeypatch):
    timeouts = []

    def respond(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json=[])

    with db.DatabaseHttpClient(transport=httpx.MockTransport(respond), timeout=8) as transport:
        monkeypatch.setattr(db, "create_database_http_client", lambda _: transport)
        monkeypatch.setattr(db, "get_settings", fake_settings)
        dependency = db.get_supabase()
        client = next(dependency)
        try:
            assert all(result["status"] == "ready" for result in system._probe_tables(client).values())
            assert timeouts
            assert all(sum(phases.values()) <= 10 for phases in timeouts)
            assert transport.timeout.read == 8
        finally:
            dependency.close()


def test_agent_health_has_an_overall_deadline(monkeypatch):
    async def stalled_agent(_self, *_args, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(httpx.AsyncClient, "get", stalled_agent)
    monkeypatch.setattr(system, "AGENT_HEALTH_TIMEOUT_SEC", 0.01)
    result = asyncio.run(system.get_system_readiness(
        settings=SimpleNamespace(agent_service_url="http://agent"),
        client=FailingDatabase(httpx.ConnectError("TLS EOF")), _=None,
    ))
    assert result["agent_health"] == {"reachable": False, "status": "down"}
    assert result["status"] == "degraded"
