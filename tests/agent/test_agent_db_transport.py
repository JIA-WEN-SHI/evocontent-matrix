from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db
from app.error_handling import install_error_handlers


def test_agent_uses_bounded_authenticated_transport(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    original_init = httpx.Client.__init__

    def init(self, **kwargs):
        assert kwargs["verify"] is True
        assert kwargs["trust_env"] is False
        kwargs["transport"] = httpx.MockTransport(respond)
        original_init(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(
        supabase_url="https://example.supabase.co", supabase_service_role_key="test-key",
        supabase_postgrest_timeout_sec=4, supabase_http_proxy="", supabase_trust_env=False,
    ))
    db.get_supabase.cache_clear()
    client = None
    try:
        client = db.get_supabase()
        assert db.get_supabase() is client
        assert client.table("domains").select("id").execute().data == []
        assert requests[0].extensions["timeout"]["read"] == 4
        assert requests[0].headers["apikey"] == "test-key"
    finally:
        if client is not None:
            client.postgrest.session.close()
        db.get_supabase.cache_clear()


def test_agent_failed_write_is_redacted_and_not_retried():
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ReadTimeout("private-token")

    with db.DatabaseHttpClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(db.DatabaseUnavailableError) as caught:
            client.post("https://example.supabase.co/rest/v1/domains", json={"id": "one"})
    assert len(requests) == 1
    assert caught.value.code == "database_timeout"
    assert caught.value.retryable is False
    assert "private-token" not in str(caught.value)


def test_agent_http_error_preserves_database_diagnosis():
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/write")
    def write():
        try:
            raise db.DatabaseUnavailableError(httpx.ReadError("secret"), method="POST")
        except db.DatabaseUnavailableError as exc:
            raise HTTPException(500, detail=str(exc)) from exc

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/write")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_connection_failed"
    assert response.json()["error"]["retryable"] is False
    assert "secret" not in response.text
