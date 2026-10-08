from pathlib import Path
import sys

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "services" / "api"))

from app.db import get_supabase
from app.main import app


class FailingAuditQuery:
    def select(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self):
        raise RuntimeError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")


class FailingAuditClient:
    def table(self, name: str):
        assert name == "audit_logs"
        return FailingAuditQuery()


def override_supabase():
    yield FailingAuditClient()


def test_audit_logs_returns_empty_items_when_data_source_is_temporarily_unavailable():
    app.dependency_overrides[get_supabase] = override_supabase
    try:
        client = TestClient(app)
        response = client.get(
            "/api/audit-logs?limit=100",
            headers={"x-user-id": "demo-reviewer", "x-user-role": "admin"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"items": []}
