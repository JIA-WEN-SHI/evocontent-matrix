import pytest
from fastapi import HTTPException

from app.models import Actor
from app.services.pipeline_service import approve_pipeline_task, reject_pipeline_task
from fake_store import MemoryStore


def task_store(status):
    store = MemoryStore()
    store.tables["pipeline_tasks"] = [{
        "id": "task-1", "status": status, "channel": "xiaohongshu", "payload_jsonb": {"title": "Draft", "body": "Content"},
    }]
    return store


def test_approval_changes_real_task_status_and_records_audit():
    store = task_store("pending_review")
    result = approve_pipeline_task(store, "task-1", Actor(user_id="qa", role="reviewer"))
    assert result["status"] == "approved"
    assert result["review_jsonb"]["approved_by"] == "qa"
    assert store.tables["audit_logs"][0]["action"] == "pipeline.task_approved"


def test_rejection_records_reason_without_publishing():
    store = task_store("pending_review")
    result = reject_pipeline_task(store, "task-1", "Please rewrite", Actor(user_id="qa", role="reviewer"))
    assert result["status"] == "review_rejected"
    assert result["review_jsonb"]["rejection_reason"] == "Please rewrite"


@pytest.mark.parametrize("state", ["queued", "drafting", "approved", "publishing", "published", "done"])
def test_invalid_approval_transition_is_rejected_by_production_service(state):
    store = task_store(state)
    with pytest.raises(HTTPException) as error:
        approve_pipeline_task(store, "task-1", Actor(user_id="qa", role="reviewer"))
    assert error.value.status_code == 409
    assert store.tables["pipeline_tasks"][0]["status"] == state
