from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import get_supabase
from app.main import app
from app.services.pipeline_service import _repair_mojibake_text


TASK_ID = "11111111-1111-4111-8111-111111111111"
HEADERS = {"x-user-id": "test-reviewer", "x-user-role": "admin"}


class TaskStore:
    def __init__(self, status="pending_review"):
        self.task = {
            "id": TASK_ID, "domain_id": "22222222-2222-4222-8222-222222222222",
            "channel": "xiaohongshu", "content_type": "post", "status": status,
            "stage": "review", "created_by": "test", "created_at": "2026-09-18T00:00:00Z",
            "updated_at": "2026-09-18T00:00:00Z",
            "payload_jsonb": {"title": "Original", "body": "Original body", "tags": ["tag"], "full_body": "Original body"},
        }
        self.audits = []
        self.conflict = False

    def table(self, name):
        return Query(self, name)


class Query:
    def __init__(self, store, table):
        self.store, self.table_name = store, table
        self.filters, self.changes, self.inserted = {}, None, None

    def select(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def update(self, changes):
        self.changes = changes
        return self

    def insert(self, row):
        self.inserted = row
        return self

    def execute(self):
        if self.table_name == "audit_logs":
            self.store.audits.append(self.inserted)
            return SimpleNamespace(data=[self.inserted])
        assert self.table_name == "pipeline_tasks"
        if self.changes is not None and self.store.conflict:
            self.store.task["status"] = "publishing"
        if any(self.store.task.get(key) != value for key, value in self.filters.items()):
            return SimpleNamespace(data=[])
        if self.changes is not None:
            self.store.task.update(deepcopy(self.changes))
        return SimpleNamespace(data=[deepcopy(self.store.task)])


@pytest.fixture
def store():
    db = TaskStore()
    app.dependency_overrides[get_supabase] = lambda: db
    yield db
    app.dependency_overrides.clear()


@pytest.mark.parametrize("status", ["approved", "publish_failed", "publishing", "published"])
def test_run_does_not_dispatch_publish_requests(store, status, monkeypatch):
    from unittest.mock import Mock
    from app.routers import pipeline
    store.task["status"] = status
    outbound = Mock(side_effect=AssertionError("Publishing must not reach the agent"))
    monkeypatch.setattr(pipeline.httpx, "AsyncClient", outbound)
    response = TestClient(app).post(f"/api/pipeline/tasks/{TASK_ID}/run", headers=HEADERS)
    assert response.status_code == 403
    assert store.task["status"] == status
    outbound.assert_not_called()


def test_save_draft_persists_content_without_approving_or_losing_metadata(store):
    response = TestClient(app).patch(f"/api/pipeline/tasks/{TASK_ID}", headers=HEADERS,
                                     json={"title": "New title", "body": "中文正文？正常问题?"})
    assert response.status_code == 200
    row = response.json()
    assert row["status"] == "pending_review"
    assert row["payload_jsonb"]["body"] == "中文正文？正常问题?"
    assert row["payload_jsonb"]["full_body"] == row["payload_jsonb"]["body"]
    assert row["payload_jsonb"]["tags"] == ["tag"]
    assert store.audits[-1]["action"] == "pipeline.task_edited"


def test_edit_invalidates_generated_analysis_but_preserves_source_records(store):
    store.task["payload_jsonb"]["analysis_jsonb"] = {
        "quality_gate": {"final": {"score": 100}},
        "evidence_map": {"sections": [{"preview": "Original body"}]},
        "reference_items": [{"source_url": "https://example.com/source"}],
    }
    store.task["payload_jsonb"]["image_prompt"] = "Original body cover"
    response = TestClient(app).patch(f"/api/pipeline/tasks/{TASK_ID}", headers=HEADERS,
                                     json={"title": "Changed", "body": "New body"})
    payload = response.json()["payload_jsonb"]
    assert "quality_gate" not in payload["analysis_jsonb"]
    assert "evidence_map" not in payload["analysis_jsonb"]
    assert payload["analysis_jsonb"]["reference_items"]
    assert payload["analysis_jsonb"]["review_required_after_edit"] is True
    assert "image_prompt" not in payload


@pytest.mark.parametrize("status", ["publishing", "published", "done", "drafting"])
def test_save_draft_does_not_modify_running_or_published_tasks(store, status):
    store.task["status"] = status
    response = TestClient(app).patch(f"/api/pipeline/tasks/{TASK_ID}", headers=HEADERS,
                                     json={"title": "New", "body": "Body"})
    assert response.status_code == 409
    assert store.task["payload_jsonb"]["title"] == "Original"


def test_concurrent_status_change_prevents_draft_overwrite(store):
    store.conflict = True
    response = TestClient(app).patch(f"/api/pipeline/tasks/{TASK_ID}", headers=HEADERS,
                                     json={"title": "New", "body": "Body"})
    assert response.status_code == 409
    assert store.task["payload_jsonb"]["title"] == "Original"


def test_editing_an_approved_task_requires_review_again(store):
    store.task["status"] = "approved"
    response = TestClient(app).patch(f"/api/pipeline/tasks/{TASK_ID}", headers=HEADERS,
                                     json={"title": "Changed after review", "body": "Body"})
    assert response.status_code == 200
    assert response.json()["status"] == "pending_review"


def test_approving_edited_body_preserves_the_displayed_full_body(store):
    response = TestClient(app).post(f"/api/pipeline/tasks/{TASK_ID}/approve", headers=HEADERS,
                                    json={"title": "Reviewed", "body": "Edited and reviewed body"})
    assert response.status_code == 200
    row = response.json()
    assert row["status"] == "approved"
    assert row["payload_jsonb"]["body"] == "Edited and reviewed body"
    assert row["payload_jsonb"]["full_body"] == "Edited and reviewed body"
    assert not row.get("published_at")


@pytest.mark.parametrize("body", ["", "   "])
def test_empty_draft_is_rejected(store, body):
    response = TestClient(app).patch(f"/api/pipeline/tasks/{TASK_ID}", headers=HEADERS,
                                     json={"title": "Title", "body": body})
    assert response.status_code == 422


def test_unicode_question_mark_is_not_lossily_reencoded():
    assert _repair_mojibake_text("正常中文, how are you?") == "正常中文, how are you?"


def test_non_reviewer_cannot_edit_draft(store):
    response = TestClient(app).patch(f"/api/pipeline/tasks/{TASK_ID}",
                                     headers={"x-user-id": "test", "x-user-role": "viewer"},
                                     json={"title": "Title", "body": "Body"})
    assert response.status_code == 403
