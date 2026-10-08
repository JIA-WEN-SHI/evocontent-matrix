from cryptography.fernet import Fernet

from app.models import LeadEventPayload
from app.services import lead_service
from fake_store import MemoryStore


def test_real_lead_attribution_encrypts_contacts_and_preserves_metrics(monkeypatch):
    store = MemoryStore()
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(lead_service, "_build_fernet", lambda: cipher)
    store.tables["pipeline_tasks"] = [{
        "id": "task-1", "status": "published", "payload_jsonb": {"utm_code": "post", "form_id": "form"},
        "metrics_jsonb": {"likes": 42},
    }]
    result = lead_service.ingest_lead_event(store, LeadEventPayload(
        utm_code="post", form_id="form", channel="xiaohongshu", contact_fields={"phone": "12345"},
    ))
    assert result["meta_jsonb"]["pipeline_task_id"] == "task-1"
    assert result["contact_fields"]["phone"] != "12345"
    assert cipher.decrypt(result["contact_fields"]["phone"].encode()).decode() == "12345"
    assert store.tables["pipeline_tasks"][0]["metrics_jsonb"] == {"likes": 42, "leads_generated": 1}


def test_attribution_does_not_match_an_unpublished_task():
    store = MemoryStore()
    store.tables["pipeline_tasks"] = [{
        "id": "task-1", "status": "queued", "payload_jsonb": {"utm_code": "post", "form_id": "form"},
    }]
    assert lead_service._resolve_pipeline_task_id(store, "post", "form") is None
