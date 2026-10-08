from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import Actor
from app.services import manual_publication
from fake_store import MemoryStore


TASK_ID = "11111111-1111-4111-8111-111111111111"
DOMAIN_ID = "22222222-2222-4222-8222-222222222222"
ACCOUNT_ID = "33333333-3333-4333-8333-333333333333"
NOTE_ID = "64abcdef0123456789abcdef"
OTHER_NOTE = "65abcdef0123456789abcdef"
URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
NOW = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
PUBLISHED = NOW - timedelta(hours=2)
ACTOR = Actor(user_id="operator-test", role="operator")


@pytest.fixture
def store(monkeypatch):
    db = MemoryStore()
    db.tables["domains"][0]["slug"] = "ai_content"
    db.tables["channel_accounts"] = [{
        "id": ACCOUNT_ID, "channel": "xiaohongshu", "is_active": True,
        "config_jsonb": {"onboarding": {"domain_slug": "ai_content"}},
    }]
    db.tables["pipeline_tasks"] = [{
        "id": TASK_ID, "domain_id": DOMAIN_ID, "account_id": ACCOUNT_ID,
        "channel": "xiaohongshu", "content_type": "post", "status": "approved",
        "stage": "approved", "created_by": "test", "created_at": "2026-10-06T00:00:00+00:00",
        "updated_at": "2026-10-07T00:00:00+00:00", "published_at": None,
        "payload_jsonb": {"channel_account_id": ACCOUNT_ID, "title": "Reviewed", "body": "Reviewed body"},
        "review_jsonb": {"approved_by": "reviewer", "approved_at": "2026-10-07T00:00:00+00:00"},
        "publish_jsonb": {}, "metrics_jsonb": {},
    }]
    monkeypatch.setattr(manual_publication, "_now", lambda: NOW)
    return db


def register(store, **kwargs):
    return manual_publication.register_manual_publication(store, **{
        "pipeline_task_id": TASK_ID, "account_id": ACCOUNT_ID, "domain_slug": "ai_content",
        "published_url": URL, "published_at": PUBLISHED, "confirmed": True, "actor": ACTOR,
        **kwargs,
    })


def reject(store, code, **kwargs):
    before = deepcopy(store.tables)
    with pytest.raises(HTTPException) as error:
        register(store, **kwargs)
    assert error.value.status_code == code
    assert store.tables == before


def task(store):
    return store.tables["pipeline_tasks"][0]


def intercept(store, hook):
    original = store.table

    def table(name):
        query = original(name)
        execute = query.execute
        query.execute = lambda: hook(name, query, execute)
        return query

    store.table = table


def test_approved_manual_publication_enters_feedback_pending(store):
    row = register(store)
    assert row["id"] == TASK_ID
    assert row["status"] == "published"
    assert row["stage"] == "feedback_pending"
    assert row["published_at"] == "2026-10-07T08:00:00+00:00"
    assert row["publish_jsonb"]["identity"]["feed_id"] == NOTE_ID
    assert row["publish_jsonb"]["identity"]["remote_post_id"] == NOTE_ID
    assert row["publish_jsonb"]["identity"]["published_url"] == URL
    provenance = row["publish_jsonb"]["manual_publication"]
    assert provenance["ownership"] == "user_confirmed"
    assert provenance["provider_verified"] is False
    assert provenance["confirmed_by"] == ACTOR.user_id
    assert provenance["account_id"] == ACCOUNT_ID
    assert row["publish_jsonb"]["feedback_state"] == "pending_metrics"
    assert row["payload_jsonb"] == task(store)["payload_jsonb"]
    assert store.tables["audit_logs"][0]["action"] == "pipeline.manual_publication_registered"


@pytest.mark.parametrize("path", ["explore", "discovery/item"])
@pytest.mark.parametrize("host", ["www.xiaohongshu.com", "xiaohongshu.com"])
def test_canonicalizes_supported_urls_and_removes_secrets(store, path, host):
    row = register(store, published_url=f"https://{host}/{path}/{NOTE_ID.upper()}?xsec_token=secret&source=share#private")
    assert row["publish_jsonb"]["identity"]["published_url"] == URL
    assert "secret" not in json.dumps(store.tables)
    assert "xsec_token" not in json.dumps(store.tables)


@pytest.mark.parametrize("url", [
    f"https://evil.test/explore/{NOTE_ID}", f"https://www.xiaohongshu.com.evil.test/explore/{NOTE_ID}",
    f"https://user:secret@www.xiaohongshu.com/explore/{NOTE_ID}",
    f"http://www.xiaohongshu.com/explore/{NOTE_ID}", f"https://www.xiaohongshu.com:444/explore/{NOTE_ID}",
    f"https://www.xiaohongshu.com/user/profile/{NOTE_ID}", "https://xhslink.com/a/share",
    "https://www.xiaohongshu.com/explore/123", "https://www.xiaohongshu.com/explore/zzabcdef0123456789abcdef",
    f"https://www.xiaohongshu.com/explore/{NOTE_ID}/extra", f"https://www.xiaohongshu.com/explore/%36{NOTE_ID[1:]}",
    f"https://www.xiaohongshu.com/explore/{NOTE_ID}\n", f"https://www.xiaohongshu.com\\evil.test/explore/{NOTE_ID}",
])
def test_rejects_invalid_publication_urls_without_writes(store, url):
    reject(store, 422, published_url=url)


@pytest.mark.parametrize("confirmed", [False, None, 1, "true"])
def test_requires_explicit_confirmation(store, confirmed):
    reject(store, 422, confirmed=confirmed)


@pytest.mark.parametrize("published_at", [datetime(2026, 10, 7, 8), NOW + timedelta(microseconds=1), "2026-10-07"])
def test_requires_timezone_and_nonfuture_time(store, published_at):
    reject(store, 422, published_at=published_at)


def test_normalizes_actual_time_and_accepts_now(store):
    row = register(store, published_at=NOW.astimezone(timezone(timedelta(hours=8))))
    assert row["published_at"] == "2026-10-07T10:00:00+00:00"


@pytest.mark.parametrize("role", ["viewer", "reviewer", "service", "unknown"])
def test_requires_operator_or_admin(store, role):
    reject(store, 403, actor=Actor(user_id="foreign", role=role))


def test_admin_can_register(store):
    assert register(store, actor=Actor(user_id="admin-test", role="admin"))["status"] == "published"


@pytest.mark.parametrize("changes", [
    {"account_id": "foreign"}, {"domain_id": "foreign"}, {"channel": "wechat_mp"},
    {"payload_jsonb": {"channel_account_id": "foreign"}}, {"deleted_at": "2026-10-06T00:00:00Z"},
])
def test_rejects_foreign_or_deleted_task(store, changes):
    task(store).update(changes)
    reject(store, 409)


def test_missing_task_is_not_found(store):
    reject(store, 404, pipeline_task_id="missing")


@pytest.mark.parametrize("changes", [
    {"is_active": False}, {"channel": "douyin"},
    {"config_jsonb": {"onboarding": {"domain_slug": "foreign"}}},
])
def test_rejects_unavailable_or_foreign_account(store, changes):
    store.tables["channel_accounts"][0].update(changes)
    reject(store, 409)


def test_legacy_payload_account_is_supported(store):
    task(store).pop("account_id")
    assert register(store)["status"] == "published"


@pytest.mark.parametrize("state", ["queued", "pending_review", "publish_failed", "publishing", "done"])
def test_only_approved_drafts_can_register_initial_publication(store, state):
    task(store)["status"] = state
    reject(store, 409)


def test_editing_approved_content_requires_review_again(store):
    from app.services.pipeline_service import update_pipeline_task

    update_pipeline_task(store, TASK_ID, ACTOR, "Changed", "Changed body")
    reject(store, 409)


def test_identical_registration_is_idempotent_and_keeps_feedback(store):
    register(store)
    task(store)["status"] = "done"
    task(store)["stage"] = "completed"
    task(store)["publish_jsonb"]["feedback_completed_hours"] = [1, 3, 24]
    task(store)["metrics_jsonb"] = {"post_metrics": {"likes": 0}}
    before = deepcopy(store.tables)
    row = register(store, published_url=f"https://xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=private",
                   published_at=PUBLISHED.astimezone(timezone(timedelta(hours=8))))
    assert row == task(store)
    assert store.tables == before


def test_conflicting_change_requires_reason(store):
    register(store)
    reject(store, 409, published_url=f"https://www.xiaohongshu.com/explore/{OTHER_NOTE}")
    reject(store, 409, published_at=PUBLISHED - timedelta(hours=1), correction_reason="  ")


def test_initial_registration_cannot_be_disguised_as_correction(store):
    reject(store, 409, correction_reason="Wrong note")


@pytest.mark.parametrize("legacy", [False, True])
def test_duplicate_note_in_same_account_rejected_even_with_reason(store, legacy):
    duplicate = deepcopy(task(store))
    duplicate.update(id="other-task", domain_id="another-domain", status="published", published_at=PUBLISHED.isoformat(),
                     publish_jsonb={"identity": {"published_url": f"https://xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=hidden"}})
    if legacy:
        duplicate.pop("account_id")
    store.tables["pipeline_tasks"].append(duplicate)
    reject(store, 409)


def test_duplicate_note_in_other_account_does_not_block_or_change_it(store):
    foreign = deepcopy(task(store))
    foreign.update(id="foreign-task", account_id="foreign", status="published",
                   payload_jsonb={"channel_account_id": "foreign"}, publish_jsonb={"identity": {"feed_id": NOTE_ID}})
    store.tables["pipeline_tasks"].append(foreign)
    assert register(store)["status"] == "published"
    assert store.tables["pipeline_tasks"][1] == foreign


def add_feedback(store):
    task(store)["status"] = "done"
    task(store)["metrics_jsonb"] = {"post_metrics": {"likes": 7}, "observations": [{"values": {"likes": 7}, "source_ref": NOTE_ID}]}
    task(store)["publish_jsonb"].update(feedback_completed_hours=[1, 3, 24], feedback_analysis={"score": 5},
                                       last_feedback_at=NOW.isoformat(), next_feedback_at=None)
    store.tables["task_metrics"] = [{"id": "snapshot", "pipeline_task_id": TASK_ID,
        "metric_window": "24h", "metrics_jsonb": {"likes": 7}, "score": 5, "captured_at": NOW.isoformat()},
        {"id": "foreign-snapshot", "pipeline_task_id": "foreign-task", "metrics_jsonb": {"likes": 8}, "score": 6}]


@pytest.mark.parametrize("changes", [
    {"published_url": f"https://www.xiaohongshu.com/explore/{OTHER_NOTE}"},
    {"published_at": PUBLISHED - timedelta(hours=1)},
])
def test_reasoned_correction_archives_and_invalidates_feedback(store, changes):
    register(store)
    add_feedback(store)
    previous = deepcopy(task(store))
    foreign = deepcopy(store.tables["task_metrics"][1])
    row = register(store, correction_reason="Corrected publication evidence", **changes)
    assert row["status"] == "published"
    assert row["stage"] == "feedback_pending"
    assert row["metrics_jsonb"] == {}
    assert row["publish_jsonb"]["feedback_completed_hours"] == []
    assert "feedback_analysis" not in row["publish_jsonb"]
    history = row["publish_jsonb"]["publication_history"][-1]
    assert history["reason"] == "Corrected publication evidence"
    assert history["previous"]["identity"]["feed_id"] == NOTE_ID
    assert history["previous"]["published_at"] == previous["published_at"]
    assert history["previous"]["metrics_jsonb"] == previous["metrics_jsonb"]
    snapshot = store.tables["task_metrics"][0]
    assert snapshot["metrics_jsonb"]["publication_invalidated"]["reason"] == "Corrected publication evidence"
    assert "likes" not in snapshot["metrics_jsonb"]
    assert snapshot["metrics_jsonb"]["publication_invalidated"]["previous_metrics"] == {"likes": 7}
    assert store.tables["task_metrics"][1] == foreign
    assert store.tables["audit_logs"][-1]["action"] == "pipeline.manual_publication_corrected"
    before = deepcopy(store.tables)
    assert register(store, correction_reason="Corrected publication evidence", **changes) == row
    assert store.tables == before


def test_conditional_update_does_not_overwrite_concurrent_edit(store):
    def hook(name, query, execute):
        if name == "pipeline_tasks" and query.action == "update":
            task(store).update(status="pending_review", updated_at=NOW.isoformat(), payload_jsonb={"body": "Changed"})
        return execute()

    intercept(store, hook)
    with pytest.raises(HTTPException) as error:
        register(store)
    assert error.value.status_code == 409
    assert task(store)["status"] == "pending_review"
    assert task(store)["published_at"] is None
    assert not store.tables.get("audit_logs")


@pytest.mark.parametrize("result", ["empty", "timeout"])
def test_unknown_write_outcome_rereads_identical_saved_registration(store, result):
    def hook(name, query, execute):
        rows = execute()
        if name == "pipeline_tasks" and query.action == "update":
            if result == "timeout":
                raise TimeoutError("response lost")
            return SimpleNamespace(data=[])
        return rows

    intercept(store, hook)
    assert register(store)["status"] == "published"
    before = deepcopy(store.tables)
    register(store)
    assert store.tables == before


def test_unconfirmed_write_reports_uncertainty_without_replay(store):
    attempts = []

    def hook(name, query, execute):
        if name == "pipeline_tasks" and query.action == "update":
            attempts.append(query.payload)
            raise TimeoutError("request outcome unknown")
        return execute()

    intercept(store, hook)
    with pytest.raises(HTTPException) as error:
        register(store)
    assert error.value.status_code == 503
    assert len(attempts) == 1
    assert task(store)["status"] == "approved"


def test_audit_failure_is_recoverable_without_republication(store):
    failures = [True]

    def hook(name, query, execute):
        if name == "audit_logs" and query.action == "insert" and failures:
            failures.pop()
            raise TimeoutError("audit unavailable")
        return execute()

    intercept(store, hook)
    with pytest.raises(HTTPException) as error:
        register(store)
    assert error.value.status_code == 503
    assert task(store)["status"] == "published"
    saved = deepcopy(task(store))
    assert register(store) == saved
    assert len(store.tables["audit_logs"]) == 1


def test_correction_snapshot_failure_is_recoverable(store):
    register(store)
    add_feedback(store)
    failures = [True]

    def hook(name, query, execute):
        if name == "task_metrics" and query.action == "update" and failures:
            failures.pop()
            raise TimeoutError("snapshot unavailable")
        return execute()

    intercept(store, hook)
    kwargs = {"published_url": f"https://www.xiaohongshu.com/explore/{OTHER_NOTE}", "correction_reason": "Wrong note"}
    with pytest.raises(HTTPException) as error:
        register(store, **kwargs)
    assert error.value.status_code == 503
    saved = deepcopy(task(store))
    assert register(store, **kwargs) == saved
    assert "publication_invalidated" in store.tables["task_metrics"][0]["metrics_jsonb"]
    assert len(store.tables["audit_logs"]) == 2


def test_later_correction_finishes_prior_interrupted_audit(store):
    failures = [True]

    def hook(name, query, execute):
        if name == "audit_logs" and query.action == "insert" and failures:
            failures.pop()
            raise TimeoutError("audit unavailable")
        return execute()

    intercept(store, hook)
    with pytest.raises(HTTPException):
        register(store)
    register(store, published_url=f"https://www.xiaohongshu.com/explore/{OTHER_NOTE}", correction_reason="Wrong note")
    assert [row["action"] for row in store.tables["audit_logs"]] == [
        "pipeline.manual_publication_registered", "pipeline.manual_publication_corrected",
    ]


def test_correction_scrubs_existing_history_secrets(store):
    register(store)
    task(store)["publish_jsonb"]["publication_history"] = [{
        "previous": {"identity": {"xsec_token": "old-secret", "published_url": URL + "?xsec_token=old-secret"}},
    }]
    register(store, published_url=f"https://www.xiaohongshu.com/explore/{OTHER_NOTE}", correction_reason="Wrong note")
    assert "old-secret" not in json.dumps(task(store)["publish_jsonb"])


def test_publication_history_does_not_recursively_duplicate_prior_audit_payload(store):
    register(store)
    for offset in range(1, 4):
        register(store, published_at=PUBLISHED - timedelta(minutes=offset), correction_reason="Corrected clock")
    history = task(store)["publish_jsonb"]["publication_history"]
    assert len(history) == 3
    for entry in history:
        previous_receipt = entry["previous"]["publish_jsonb"]["manual_publication"]
        assert "audit" not in previous_receipt
        assert "snapshot_invalidations" not in previous_receipt


def test_timestamp_conflict_without_status_change_preserves_new_feedback(store):
    register(store)

    def hook(name, query, execute):
        if name == "pipeline_tasks" and query.action == "update":
            task(store)["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
            task(store)["metrics_jsonb"] = {"post_metrics": {"likes": 9}}
        return execute()

    intercept(store, hook)
    with pytest.raises(HTTPException) as error:
        register(store, published_at=PUBLISHED - timedelta(minutes=1), correction_reason="Wrong clock")
    assert error.value.status_code == 409
    assert task(store)["metrics_jsonb"] == {"post_metrics": {"likes": 9}}
    assert task(store)["published_at"] == PUBLISHED.isoformat()


def test_duplicate_scan_fails_closed_at_bound(store):
    for index in range(1000):
        other = deepcopy(task(store))
        other["id"] = f"task-{index}"
        store.tables["pipeline_tasks"].append(other)
    reject(store, 409)


def test_duplicate_target_blocks_reasoned_correction(store):
    register(store)
    other = deepcopy(task(store))
    other.update(id="other-task", publish_jsonb={"identity": {"feed_id": OTHER_NOTE}})
    store.tables["pipeline_tasks"].append(other)
    reject(store, 409, published_url=f"https://www.xiaohongshu.com/explore/{OTHER_NOTE}", correction_reason="Wrong note")


def test_audit_response_loss_does_not_duplicate_audit(store):
    def hook(name, query, execute):
        result = execute()
        if name == "audit_logs" and query.action == "insert":
            raise TimeoutError("response lost")
        return result

    intercept(store, hook)
    register(store)
    register(store)
    assert len(store.tables["audit_logs"]) == 1


def test_no_publish_tool_or_network_invoked(store, monkeypatch):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("External publication or network access prohibited")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert register(store)["status"] == "published"
