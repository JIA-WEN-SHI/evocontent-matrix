import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.db import get_supabase
from app.main import app
from fake_store import MemoryStore


HEADERS = {"x-user-id": "qa-admin", "x-user-role": "admin"}
UUID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def api():
    store = MemoryStore()
    app.dependency_overrides[get_supabase] = lambda: store
    with TestClient(app) as client:
        yield client, store
    app.dependency_overrides.clear()


def request(client, method, path, payload=None):
    response = client.request(method, path, headers=HEADERS, json=payload)
    assert response.status_code < 300, response.text
    return response.json()


def test_account_create_update_and_disable(api):
    client, _ = api
    row = request(client, "POST", "/api/accounts", {"account_name": "Test account", "login_mode": "storage_state"})
    account_id = row["id"]
    assert row["is_active"] is True
    row = request(client, "PATCH", f"/api/accounts/{account_id}", {"account_name": "Renamed", "is_active": False})
    assert row["account_name"] == "Renamed" and not row["is_active"]
    rows = request(client, "GET", "/api/accounts")["items"]
    assert rows[0]["id"] == account_id


@pytest.mark.parametrize("resource,payload,field,value", [
    ("cases", {"title": "Case", "content": "Evidence"}, "title", "Updated case"),
    ("assets", {"content": "Asset"}, "is_verified", True),
    ("user-needs", {"original_text": "Question"}, "real_problem", "Problem"),
    ("topics", {"title": "Topic"}, "status", "drafted"),
    ("reviews", {"summary_text": "Review"}, "improvement", "Improvement"),
    ("tags", {"name": "Tag"}, "name", "Renamed tag"),
])
def test_knowledge_create_edit_list_and_soft_delete(api, resource, payload, field, value):
    client, _ = api
    path = f"/api/kb/{resource}"
    row = request(client, "POST", path, payload)
    row = request(client, "PATCH", f"{path}/{row['id']}", {field: value})
    assert row[field] == value
    assert len(request(client, "GET", path)["items"]) == 1
    request(client, "PATCH", f"{path}/{row['id']}", {"deleted": True})
    assert request(client, "GET", path)["items"] == []


def test_pipeline_uses_explicit_account_when_multiple_accounts_are_active(api):
    client, _ = api
    first = request(client, "POST", "/api/accounts", {"account_name": "First"})
    second = request(client, "POST", "/api/accounts", {"account_name": "Second"})
    task = request(client, "POST", "/api/pipeline/tasks", {
        "domain_slug": "japan_immigration", "account_id": second["id"],
        "payload_jsonb": {"title": "Draft", "body": "Text"},
    })
    assert task["account_id"] == second["id"] != first["id"]
    assert task["payload_jsonb"]["channel_account_id"] == second["id"]


def test_import_tags_recommend_and_review_complete_knowledge_cycle(api):
    client, store = api
    payload = {"entity_type": "case", "source_run_id": "qa-import", "items": [{
        "title": "日本签证申请步骤", "content": "先核对条件，再准备材料，最后提交申请。",
        "url": "https://example.com/qa-evidence", "source_ref": "qa-case-1", "tags": ["签证"],
    }]}
    result = request(client, "POST", "/api/kb/import/octopus", payload)
    assert result["success_count"] == 1 and result["failed_count"] == 0
    assert result["auto_assets_created_count"] == 4
    case_id = result["inserted_ids"][0]
    assert sum(row["name"] == "签证" for row in store.tables["tags"]) == 1
    assert any(row["entity_id"] == case_id for row in store.tables["entity_tags"])
    repeated = request(client, "POST", "/api/kb/import/octopus", payload)
    assert repeated["inserted_ids"] == [case_id]
    assert len(store.tables["cases"]) == 1
    topics = request(client, "POST", "/api/kb/topics/recommend", {"case_ids": [case_id], "limit": 2})
    assert topics["created_count"] > 0
    topic = topics["topics"][0]
    review = request(client, "POST", "/api/kb/reviews", {
        "topic_id": topic["id"], "summary_text": "测试复盘", "improvement": "保留材料清单",
    })
    assert review["topic_id"] == topic["id"]
    assert store.tables["topic_case_links"][0]["case_id"] == case_id


MUTATIONS = [
    (method, re.sub(r"\{[^}]+\}", UUID, route.path))
    for route in app.routes if isinstance(route, APIRoute)
    for method in sorted(route.methods & {"POST", "PUT", "PATCH", "DELETE"})
    if not route.path.startswith("/api/webhooks/")
]


@pytest.mark.parametrize("method,path", MUTATIONS)
def test_mutations_reject_unauthenticated_requests_before_writing(api, method, path):
    client, store = api
    response = client.request(method, path, json={})
    assert response.status_code == 401, (method, path, response.text)
    assert not any(rows for table, rows in store.tables.items() if table != "domains")


@pytest.mark.parametrize("path", ["leads", "kb-octopus"])
def test_webhooks_reject_invalid_signature(api, path):
    client, _ = api
    response = client.post(f"/api/webhooks/{path}", headers={"x-signature": "invalid"}, json={})
    assert response.status_code == 401
