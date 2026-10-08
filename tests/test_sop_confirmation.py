from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError

from app.models import Actor
from app.services import account_service


NOTE = "6ac3b3a200000000140395fc"
URL = "https://www.xiaohongshu.com/explore/" + NOTE


class Query:
    def __init__(self, client, table):
        self.client, self.name = client, table
        self.filters, self.payload, self.operation = [], None, "select"
        self.bound = None

    def select(self, *_):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def is_(self, key, value):
        assert value == "null"
        return self.eq(key, None)

    def limit(self, value):
        self.bound = value
        return self

    def update(self, payload):
        self.payload, self.operation = deepcopy(payload), "update"
        return self

    def insert(self, payload):
        self.payload, self.operation = deepcopy(payload), "insert"
        return self

    def execute(self):
        rows = self.client.rows.setdefault(self.name, [])
        selected = []
        for row in rows:
            matches = True
            for key, value in self.filters:
                parts = key.replace("->>", "->").split("->")
                actual = row
                for part in parts:
                    actual = actual.get(part) if isinstance(actual, dict) else None
                if "->>" in key and actual is not None:
                    actual = str(actual)
                if key == "config_jsonb" and isinstance(value, str):
                    value = json.loads(value)
                if actual != value:
                    matches = False
                    break
            if matches:
                selected.append(row)
        if self.bound is not None:
            selected = selected[:self.bound]
        if self.operation == "select":
            return SimpleNamespace(data=deepcopy(selected))
        self.client.writes.append((self.name, self.operation, deepcopy(self.payload)))
        failure = self.client.failure
        fail = bool(failure and failure(self.name, self.payload))
        if fail and not self.client.persist_on_failure:
            self.client.failure = None
            raise TimeoutError("uncertain write")
        if self.operation == "insert":
            if any(row.get("id") == self.payload.get("id") for row in rows) and self.payload.get("id"):
                raise RuntimeError("duplicate primary key")
            rows.append(deepcopy(self.payload))
            selected = [rows[-1]]
        else:
            for row in selected:
                row.update(deepcopy(self.payload))
        if fail:
            self.client.failure = None
            raise TimeoutError("persisted but response lost")
        return SimpleNamespace(data=deepcopy(selected))


class Database:
    def __init__(self):
        self.failure, self.persist_on_failure = None, False
        self.writes = []
        self.before = account_service._normalize_feedback_plan({"checkpoints_hours": [1, 3, 24]})
        self.after = {**deepcopy(self.before), "checkpoints_hours": [1, 3, 12, 24]}
        self.proposal = {
            "target": "feedback_plan", "before": self.before, "after": self.after,
            "before_version": 1, "reason": "Review actual observations",
        }
        content = {
            "decision": "ADJUST", "proposal": self.proposal,
            "evidence_refs": [{"source_ref": "task-1", "account_id": "account-1", "domain_id": "domain-1"}],
        }
        config = {"domain_slug": "ai_content", "feedback_plan": deepcopy(self.before)}
        account_service._append_sop_snapshot(config, reason="baseline", changed_fields=[], actor="operator")
        now = datetime.now(timezone.utc)
        published = (now - timedelta(days=1)).isoformat()
        observed = (now - timedelta(hours=1)).isoformat()
        self.rows = {
            "domains": [{"id": "domain-1", "slug": "ai_content"}],
            "channel_accounts": [{"id": "account-1", "account_name": "Test account", "config_jsonb": config,
                                  "updated_at": "2026-10-07T00:00:00+00:00"}],
            "memory_items": [{"id": "item-1", "domain_id": "domain-1", "account_id": "account-1",
                              "type": "strategy", "title": "Feedback adjustment", "content": json.dumps(content),
                              "status": "pending", "tags": ["feedback"], "updated_at": "2026-10-07T01:00:00+00:00"}],
            "audit_logs": [],
            "pipeline_tasks": [{"id": "task-1", "account_id": "account-1", "domain_id": "domain-1",
                                "status": "published", "channel": "xiaohongshu", "published_at": published,
                                "publish_jsonb": {
                                    "published_at": published,
                                    "identity": {"feed_id": NOTE, "remote_post_id": NOTE, "published_url": URL},
                                    "manual_publication": {"confirmed": True, "ownership": "user_confirmed",
                                                           "account_id": "account-1", "domain_slug": "ai_content"},
                                },
                                "metrics_jsonb": {"metrics_mode": "real", "post_metrics": {"likes": 0},
                                                  "post_metrics_synced_at": observed,
                                                  "observation": {"values": {"likes": 0}, "observed_at": observed,
                                                                  "provider": "manual", "source_ref": URL,
                                                                  "provenance": "creator_dashboard"}}}],
        }

    def table(self, name):
        return Query(self, name)

    @property
    def config(self):
        return self.rows["channel_accounts"][0]["config_jsonb"]

    @property
    def item(self):
        return self.rows["memory_items"][0]


@pytest.fixture
def service():
    def invoke(*args, **kwargs):
        path = "app.services.strategy_confirmation"
        try:
            module = importlib.import_module(path)
        except ModuleNotFoundError as exc:
            if exc.name != path:
                raise
            pytest.fail("independent strategy confirmation service is missing")
        return module.apply_confirmed_strategy(*args, **kwargs)
    return SimpleNamespace(apply_confirmed_strategy=invoke)


def apply(service, db, **overrides):
    arguments = dict(item_id="item-1", domain_slug="ai_content", account_id="account-1",
                     target="feedback_plan", actor=Actor(user_id="operator", role="operator"))
    arguments.update(overrides)
    return service.apply_confirmed_strategy(db, **arguments)


def test_confirmation_persists_evidence_snapshot_before_activation(service):
    db = Database()
    result = apply(service, db)
    assert result["status"] == "ok"
    assert result["version"] == 2
    assert result["memory_status"] == "active"
    assert result["receipt"]["stage"] == "completed"
    assert db.config["feedback_plan"]["checkpoints_hours"] == [1, 3, 12, 24]
    snapshot = db.config["sop_snapshots"][-1]
    assert snapshot["context"]["source_item_id"] == "item-1"
    assert snapshot["context"]["evidence_refs"][0]["source_ref"] == "task-1"
    assert snapshot["actor"] == "operator"
    assert snapshot["changed_fields"] == ["feedback_plan"]
    activation_index = next(i for i, (table, _, payload) in enumerate(db.writes)
                            if table == "memory_items" and payload.get("status") == "active")
    assert any(table == "channel_accounts" and payload["config_jsonb"].get("sop_latest_version") == 2
               for table, _, payload in db.writes[:activation_index])
    assert any(table == "audit_logs" for table, _, _ in db.writes[:activation_index])


def test_duplicate_confirmation_reuses_receipt_and_version(service):
    db = Database()
    first = apply(service, db)
    writes = len(db.writes)
    second = apply(service, db)
    assert second["version"] == first["version"] == 2
    assert second["receipt"] == first["receipt"]
    assert len(db.writes) == writes
    assert len(db.rows["audit_logs"]) == 1


@pytest.mark.parametrize("field,value", [("status", "rejected"), ("status", "active"),
                                          ("account_id", "other-account"), ("account_id", None),
                                          ("domain_id", "other-domain")])
def test_pending_and_exact_account_domain_scope_required(service, field, value):
    db = Database()
    db.item[field] = value
    with pytest.raises(HTTPException):
        apply(service, db)
    assert db.writes == []


@pytest.mark.parametrize("decision", ["HOLD", "insufficient_evidence", "competitor_only"])
def test_non_executable_suggestion_rejected_even_with_injected_proposal(service, decision):
    db = Database()
    body = json.loads(db.item["content"])
    body["decision"] = decision
    db.item["content"] = json.dumps(body)
    with pytest.raises(HTTPException) as exc:
        apply(service, db, proposal=deepcopy(db.proposal))
    assert exc.value.status_code == 422
    assert db.writes == []


def test_injected_proposal_uses_persisted_item_and_links_evidence(service):
    db = Database()
    body = json.loads(db.item["content"])
    del body["proposal"]
    db.item["content"] = json.dumps(body)
    assert apply(service, db, proposal=deepcopy(db.proposal))["version"] == 2


def test_plain_hold_cannot_be_converted_to_generic_proposal(service):
    db = Database()
    db.item["content"] = "HOLD: insufficient own-account observations"
    with pytest.raises(HTTPException) as exc:
        apply(service, db, proposal=deepcopy(db.proposal))
    assert exc.value.status_code == 422
    assert db.writes == []


@pytest.mark.parametrize("mutation", ["before", "version", "no_change", "no_evidence", "foreign_evidence", "target", "role"])
def test_unsafe_or_stale_proposal_never_mutates_config(service, mutation):
    db = Database()
    body = json.loads(db.item["content"])
    arguments = {}
    if mutation == "before":
        body["proposal"]["before"]["checkpoints_hours"] = [24]
    elif mutation == "version":
        body["proposal"]["before_version"] = 0
    elif mutation == "no_change":
        body["proposal"]["after"] = deepcopy(body["proposal"]["before"])
    elif mutation == "no_evidence":
        body["evidence_refs"] = []
    elif mutation == "foreign_evidence":
        body["evidence_refs"][0]["account_id"] = "other-account"
    elif mutation == "target":
        arguments["target"] = "strategy_profile"
    else:
        arguments["actor"] = Actor(user_id="reviewer", role="reviewer")
    db.item["content"] = json.dumps(body)
    with pytest.raises(HTTPException):
        apply(service, db, **arguments)
    assert db.writes == []


@pytest.mark.parametrize("phase", ["prepared", "configured", "checkpointed", "audit", "active", "completed"])
@pytest.mark.parametrize("persisted", [False, True])
def test_failed_write_recovers_without_duplicate_application(service, phase, persisted):
    db = Database()
    def fail(table, payload):
        if phase == "audit":
            return table == "audit_logs"
        if phase == "active":
            return table == "memory_items" and payload.get("status") == "active"
        receipts = payload.get("config_jsonb", {}).get("strategy_confirmation_receipts", {})
        return any(receipt.get("stage") == phase for receipt in receipts.values())
    db.failure, db.persist_on_failure = fail, persisted
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 503
    if phase in {"prepared", "configured", "checkpointed", "audit"}:
        assert db.item["status"] == "pending"
    result = apply(service, db)
    assert result["version"] == 2
    assert result["receipt"]["stage"] == "completed"
    assert len(db.config["sop_snapshots"]) == 2
    assert len(db.rows["audit_logs"]) == 1


def test_retry_does_not_overwrite_newer_configuration(service):
    db = Database()
    db.failure = lambda table, payload: table == "channel_accounts" and any(
        receipt.get("stage") == "checkpointed"
        for receipt in payload["config_jsonb"].get("strategy_confirmation_receipts", {}).values())
    with pytest.raises(HTTPException):
        apply(service, db)
    db.config["feedback_plan"]["checkpoints_hours"] = [24, 48]
    account_service._append_sop_snapshot(db.config, reason="newer edit", changed_fields=["feedback_plan"], actor="other")
    before = deepcopy(db.config)
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 409
    assert db.config == before
    assert db.item["status"] == "pending"


def test_existing_audited_rollback_recovers_prior_feedback_version(service):
    db = Database()
    apply(service, db)
    result = account_service.rollback_account_sop_snapshot(
        db, account_id="account-1", domain_slug="ai_content", version=1,
        actor=Actor(user_id="operator", role="operator"), reason="restore prior feedback")
    assert result["active_version"] == 3
    assert db.config["feedback_plan"]["checkpoints_hours"] == [1, 3, 24]
    replay = apply(service, db)
    assert replay["version"] == 2
    assert db.config["feedback_plan"]["checkpoints_hours"] == [1, 3, 24]


@pytest.mark.parametrize("target,after", [
    ("collection_plan", {"mode": "xhs_cli", "steps": [{"tool": "xhs_cli_search", "query": "AI", "limit": 9}]}),
    ("publish_preferences", {"next_publish_slot_local": "20:30", "min_action_gap_seconds": 2}),
])
def test_other_allowlisted_targets_have_recoverable_snapshot(service, target, after):
    db = Database()
    body = json.loads(db.item["content"])
    body["proposal"] = {"target": target, "before": {}, "after": after}
    db.item["content"] = json.dumps(body)
    result = apply(service, db, target=target)
    assert result["version"] == 2
    assert db.config[target] == result["receipt"]["after"]
    assert db.config["sop_snapshots"][-1][target] == db.config[target]


@pytest.mark.parametrize("mutation", ["foreign_task", "missing_task", "no_observations", "competitor_url", "hold_flag"])
def test_unverified_or_non_executable_evidence_is_rejected(service, mutation):
    db = Database()
    body = json.loads(db.item["content"])
    if mutation == "foreign_task":
        db.rows["pipeline_tasks"][0]["account_id"] = "other-account"
    elif mutation == "missing_task":
        db.rows["pipeline_tasks"] = []
    elif mutation == "no_observations":
        db.rows["pipeline_tasks"][0]["metrics_jsonb"] = {}
    elif mutation == "competitor_url":
        body["evidence_refs"] = ["https://www.xiaohongshu.com/explore/competitor"]
    else:
        body["executable"] = False
        body["decision"] = "HOLD"
        body["proposal"]["decision"] = "ADJUST"
    db.item["content"] = json.dumps(body)
    with pytest.raises(HTTPException):
        apply(service, db)
    assert db.writes == []


def test_completed_receipt_cannot_hide_missing_persisted_audit(service):
    db = Database()
    apply(service, db)
    db.rows["audit_logs"] = []
    with pytest.raises(HTTPException):
        apply(service, db)


def test_source_edit_during_configuration_write_cannot_activate(service):
    db = Database()
    def edit(table, payload):
        receipts = payload.get("config_jsonb", {}).get("strategy_confirmation_receipts", {})
        if table == "channel_accounts" and any(r["stage"] == "configured" for r in receipts.values()):
            db.item["content"] = "HOLD: suggestion withdrawn"
        return False
    db.failure = edit
    with pytest.raises(HTTPException):
        apply(service, db)
    assert db.item["status"] == "pending"


def test_conditional_config_write_does_not_overwrite_concurrent_edit(service):
    db = Database()
    original_table = db.table
    edited = False
    def table(name):
        nonlocal edited
        query = original_table(name)
        execute = query.execute
        def concurrent_execute():
            nonlocal edited
            if name == "channel_accounts" and query.operation == "update" and not edited:
                db.config["operator_note"] = "keep concurrent edit"
                db.rows[name][0]["updated_at"] = datetime.now(timezone.utc).isoformat()
                edited = True
            return execute()
        query.execute = concurrent_execute
        return query
    db.table = table
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 409
    assert db.config["operator_note"] == "keep concurrent edit"
    assert db.config["feedback_plan"]["checkpoints_hours"] == [1, 3, 24]
    assert db.item["status"] == "pending"


def test_injected_proposal_cannot_replace_explicit_pending_item_change(service):
    db = Database()
    injected = deepcopy(db.proposal)
    injected["after"]["checkpoints_hours"] = [48]
    with pytest.raises(HTTPException) as exc:
        apply(service, db, proposal=injected)
    assert exc.value.status_code == 409
    assert db.writes == []


def test_generic_collection_proposal_preserves_existing_schedule_and_gate(service):
    db = Database()
    before = account_service._normalize_collection_plan({
        "mode": "xhs_cli", "steps": [{"tool": "xhs_cli_search", "limit": 8}],
        "daily_schedule": {"posts_per_day": 2, "time_slots": ["08:15"]},
        "loop_gate": {"min_case_per_day": 7, "min_asset_per_day": 6},
    })
    db.config["collection_plan"] = deepcopy(before)
    body = json.loads(db.item["content"])
    body["proposal"] = {"target": "collection_plan", "before": before,
                        "after": {"mode": "xhs_cli", "steps": [{"tool": "xhs_cli_search", "limit": 10}]}}
    db.item["content"] = json.dumps(body)
    apply(service, db, target="collection_plan")
    assert db.config["collection_plan"]["daily_schedule"]["time_slots"] == ["08:15"]
    assert db.config["collection_plan"]["loop_gate"] == {"min_case_per_day": 7, "min_asset_per_day": 6}
    assert db.config["collection_plan"]["steps"][0]["limit"] == 10


def test_corrupt_receipt_stage_cannot_activate_without_snapshot_or_audit(service):
    db = Database()
    db.failure = lambda table, payload: table == "channel_accounts" and any(
        receipt.get("stage") == "configured"
        for receipt in payload["config_jsonb"].get("strategy_confirmation_receipts", {}).values())
    with pytest.raises(HTTPException):
        apply(service, db)
    db.config["strategy_confirmation_receipts"]["item-1"]["stage"] = "invalid"
    with pytest.raises(HTTPException):
        apply(service, db)
    assert db.item["status"] == "pending"
    assert len(db.config["sop_snapshots"]) == 1


def test_unconfirmed_checkpoint_response_never_activates(service):
    db = Database()
    original_table = db.table
    def table(name):
        query = original_table(name)
        execute = query.execute
        def lost_write():
            receipts = (query.payload or {}).get("config_jsonb", {}).get("strategy_confirmation_receipts", {})
            if name == "channel_accounts" and any(r["stage"] == "checkpointed" for r in receipts.values()):
                return SimpleNamespace(data=deepcopy(db.rows[name]))
            return execute()
        query.execute = lost_write
        return query
    db.table = table
    with pytest.raises(HTTPException):
        apply(service, db)
    assert db.item["status"] == "pending"
    assert len(db.config["sop_snapshots"]) == 1
    assert db.rows["audit_logs"] == []


def test_explicit_false_hold_flags_do_not_block_executable_suggestion(service):
    db = Database()
    body = json.loads(db.item["content"])
    body.update(hold=False, insufficient_evidence=False, competitor_only=False)
    db.item["content"] = json.dumps(body)
    assert apply(service, db)["status"] == "ok"


def test_chinese_hold_item_cannot_be_promoted_by_generic_proposal(service):
    db = Database()
    db.item["content"] = "\u8bc1\u636e\u4e0d\u8db3"
    with pytest.raises(HTTPException) as exc:
        apply(service, db, proposal=db.proposal)
    assert exc.value.status_code == 422
    assert db.writes == []


@pytest.mark.parametrize("mutation", [
    "unpublished", "approved", "missing_publication_time", "naive_publication_time", "future_publication_time",
    "publication_time_mismatch", "missing_identity", "identity_note_mismatch", "identity_url_mismatch",
    "missing_manual_registration", "unconfirmed_registration", "foreign_registration", "foreign_registration_domain",
    "missing_mode", "synthetic_mode", "synthetic_post_metrics", "missing_observation_time", "naive_observation_time",
    "future_observation_time", "observation_before_publication", "missing_provider", "synthetic_provider",
    "missing_provenance", "empty_provenance", "synthetic_provenance", "missing_source", "wrong_source", "invalid_source",
    "invalidated_observation", "invalidated_metrics", "publication_invalidated", "empty_values", "null_values",
    "boolean_values", "nonfinite_values", "negative_values", "conflicting_post_metrics", "conflicting_sync_time",
])
def test_only_actual_registered_publication_observations_can_confirm(service, mutation):
    db = Database()
    task = db.rows["pipeline_tasks"][0]
    publish = task["publish_jsonb"]
    metrics = task["metrics_jsonb"]
    observation = metrics["observation"]
    now = datetime.now(timezone.utc)
    if mutation in {"unpublished", "approved"}:
        task["status"] = "pending_review" if mutation == "unpublished" else "approved"
    elif mutation == "missing_publication_time":
        task.pop("published_at")
    elif mutation == "naive_publication_time":
        task["published_at"] = "2026-01-01T00:00:00"
    elif mutation == "future_publication_time":
        task["published_at"] = publish["published_at"] = (now + timedelta(days=1)).isoformat()
    elif mutation == "publication_time_mismatch":
        publish["published_at"] = (now - timedelta(days=2)).isoformat()
    elif mutation == "missing_identity":
        publish.pop("identity")
    elif mutation == "identity_note_mismatch":
        publish["identity"]["remote_post_id"] = "6ac3b3a200000000140395fd"
    elif mutation == "identity_url_mismatch":
        publish["identity"]["published_url"] = "https://www.xiaohongshu.com/explore/6ac3b3a200000000140395fd"
    elif mutation == "missing_manual_registration":
        publish.pop("manual_publication")
    elif mutation == "unconfirmed_registration":
        publish["manual_publication"]["confirmed"] = False
    elif mutation == "foreign_registration":
        publish["manual_publication"]["account_id"] = "another-account"
    elif mutation == "foreign_registration_domain":
        publish["manual_publication"]["domain_slug"] = "another-domain"
    elif mutation == "missing_mode":
        metrics.pop("metrics_mode")
    elif mutation == "synthetic_mode":
        metrics["metrics_mode"] = "synthetic_preview_only"
    elif mutation == "synthetic_post_metrics":
        metrics["metrics_mode"] = "synthetic"
        metrics.pop("observation")
    elif mutation == "missing_observation_time":
        observation.pop("observed_at")
    elif mutation == "naive_observation_time":
        observation["observed_at"] = "2026-01-01T01:00:00"
    elif mutation == "future_observation_time":
        observation["observed_at"] = (now + timedelta(days=1)).isoformat()
    elif mutation == "observation_before_publication":
        observation["observed_at"] = (now - timedelta(days=2)).isoformat()
    elif mutation == "missing_provider":
        observation.pop("provider")
    elif mutation == "synthetic_provider":
        observation["provider"] = "synthetic_preview"
    elif mutation == "missing_provenance":
        observation.pop("provenance")
    elif mutation == "empty_provenance":
        observation["provenance"] = {}
    elif mutation == "synthetic_provenance":
        observation["provenance"] = {"method": "synthetic_preview"}
    elif mutation == "missing_source":
        observation.pop("source_ref")
    elif mutation == "wrong_source":
        observation["source_ref"] = "https://www.xiaohongshu.com/explore/6ac3b3a200000000140395fd"
    elif mutation == "invalid_source":
        observation["source_ref"] = "https://example.com/explore/" + NOTE
    elif mutation == "invalidated_observation":
        observation["invalidated"] = True
    elif mutation == "invalidated_metrics":
        metrics["invalidated"] = True
    elif mutation == "publication_invalidated":
        metrics["publication_invalidated"] = {"revision": "changed-publication"}
    elif mutation == "empty_values":
        observation["values"] = {}
    elif mutation == "null_values":
        observation["values"] = {"likes": None}
    elif mutation == "boolean_values":
        observation["values"] = {"likes": False}
    elif mutation == "nonfinite_values":
        observation["values"] = {"likes": float("inf")}
    elif mutation == "negative_values":
        observation["values"] = {"likes": -1}
    elif mutation == "conflicting_post_metrics":
        metrics["post_metrics"] = {"likes": 300}
    elif mutation == "conflicting_sync_time":
        metrics["post_metrics_synced_at"] = (now - timedelta(hours=2)).isoformat()
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 422
    assert db.writes == []
    assert db.item["status"] == "pending"


@pytest.mark.parametrize("mutation", ["timestamp", "provider", "source_ref", "observation_id"])
def test_evidence_reference_must_match_saved_observation(service, mutation):
    db = Database()
    body = json.loads(db.item["content"])
    ref = body["evidence_refs"][0]
    if mutation == "timestamp":
        ref["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    elif mutation == "provider":
        ref["provider"] = "xiaohongshu_cli"
    elif mutation == "source_ref":
        ref.update(task_id="task-1", source_ref="https://www.xiaohongshu.com/explore/6ac3b3a200000000140395fd")
    else:
        ref["observation_id"] = "another-observation"
        db.rows["pipeline_tasks"][0]["metrics_jsonb"]["observation_id"] = "actual-observation"
    db.item["content"] = json.dumps(body)
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 422
    assert db.writes == []


@pytest.mark.parametrize("provider,provenance", [
    ("manual", "creator_dashboard"), ("xiaohongshu_cli", "public_note_detail"),
    ("xhs_mcp_readonly", {"tool_name": "get_feed_detail"}),
])
def test_supported_actual_providers_and_old_provenance_shapes_remain_usable(service, provider, provenance):
    db = Database()
    metrics = db.rows["pipeline_tasks"][0]["metrics_jsonb"]
    metrics["observation"].update(provider=provider, provenance=provenance, invalidated=False)
    metrics["invalidated"] = False
    result = apply(service, db)
    assert result["status"] == "ok"
    assert db.config["sop_latest_version"] == 2


def test_older_actual_post_metrics_require_explicit_source_time_and_provenance(service):
    db = Database()
    metrics = db.rows["pipeline_tasks"][0]["metrics_jsonb"]
    observation = metrics.pop("observation")
    metrics.update(mcp_source="xhs_mcp_readonly", mcp_tool="get_feed_detail", source_ref=URL)
    assert apply(service, db)["status"] == "ok"
    assert metrics["post_metrics_synced_at"] == observation["observed_at"]


@pytest.mark.parametrize("field", ["mcp_tool", "source_ref", "post_metrics_synced_at"])
def test_legacy_post_metrics_without_actual_provenance_are_rejected(service, field):
    db = Database()
    metrics = db.rows["pipeline_tasks"][0]["metrics_jsonb"]
    metrics.pop("observation")
    metrics.update(mcp_source="xhs_mcp_readonly", mcp_tool="get_feed_detail", source_ref=URL)
    metrics.pop(field)
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 422
    assert db.writes == []


def test_recovering_receipt_revalidates_evidence_before_activation(service):
    db = Database()
    db.failure = lambda table, payload: table == "channel_accounts" and any(
        receipt.get("stage") == "checkpointed"
        for receipt in payload["config_jsonb"].get("strategy_confirmation_receipts", {}).values())
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 503
    db.rows["pipeline_tasks"][0]["metrics_jsonb"]["observation"]["invalidated"] = True
    before = deepcopy(db.config)
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 422
    assert db.config == before
    assert db.item["status"] == "pending"


def test_evidence_invalidated_during_confirmation_cannot_activate(service):
    db = Database()
    def invalidate(table, payload):
        receipts = payload.get("config_jsonb", {}).get("strategy_confirmation_receipts", {})
        if table == "channel_accounts" and any(r["stage"] == "configured" for r in receipts.values()):
            db.rows["pipeline_tasks"][0]["metrics_jsonb"]["publication_invalidated"] = {"revision": "correction"}
        return False
    db.failure = invalidate
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 422
    assert db.item["status"] == "pending"


@pytest.mark.parametrize("source_text", ["x" * 33787, "\u4e2d" * 1500], ids=["large_ascii", "encoded_unicode"])
def test_large_config_confirmation_uses_bounded_data_api_filter(service, source_text):
    db = Database()
    db.config["archived_source_text"] = source_text
    for _ in range(15):
        account_service._append_sop_snapshot(db.config, reason="old checkpoint", changed_fields=[], actor="operator")
    body = json.loads(db.item["content"])
    body["proposal"]["before_version"] = 16
    db.item["content"] = json.dumps(body)
    original_table = db.table
    filter_lengths = []
    def table(name):
        query = original_table(name)
        execute = query.execute
        def data_api_execute():
            if name == "channel_accounts" and query.operation == "update":
                encoded = urlencode([(key, "eq." + str(value)) for key, value in query.filters])
                filter_lengths.append(len(encoded))
                if len(encoded) > 3000:
                    raise APIError({"code": "400", "message": "JSON could not be generated",
                                    "details": "URL filter exceeds configured Data API limit", "hint": None})
                assert any(key == "updated_at" for key, _ in query.filters)
                assert any(key == "config_jsonb->>sop_latest_version" for key, _ in query.filters)
                assert all(key != "config_jsonb" for key, _ in query.filters)
            return execute()
        query.execute = data_api_execute
        return query
    db.table = table
    result = apply(service, db)
    assert result["version"] == 17
    assert result["receipt"]["stage"] == "completed"
    assert filter_lengths and max(filter_lengths) <= 3000
    assert db.config["archived_source_text"] == source_text


@pytest.mark.parametrize("timestamp", [None, "", "invalid", "2026-01-01T00:00:00"])
def test_large_config_requires_usable_concurrency_timestamp(service, timestamp):
    db = Database()
    db.config["archived_source_text"] = "x" * 33787
    db.rows["channel_accounts"][0]["updated_at"] = timestamp
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 409
    assert db.writes == []
    assert db.item["status"] == "pending"


def test_large_config_cas_preserves_concurrent_timestamped_edit(service):
    db = Database()
    db.config["archived_source_text"] = "x" * 33787
    original_table = db.table
    edited = False
    def table(name):
        nonlocal edited
        query = original_table(name)
        execute = query.execute
        def concurrent_execute():
            nonlocal edited
            if name == "channel_accounts" and query.operation == "update" and not edited:
                db.config["operator_note"] = "keep concurrent edit"
                db.rows[name][0]["updated_at"] = datetime.now(timezone.utc).isoformat()
                edited = True
            return execute()
        query.execute = concurrent_execute
        return query
    db.table = table
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 409
    assert db.config["operator_note"] == "keep concurrent edit"
    assert db.config["feedback_plan"]["checkpoints_hours"] == [1, 3, 24]
    assert db.item["status"] == "pending"


@pytest.mark.parametrize("container,key,value", [
    ("observation", "account_id", "another-account"), ("observation", "domain_id", "another-domain"),
    ("observation", "domain_slug", "another-domain"), ("metrics", "account_id", "another-account"),
])
def test_declared_observation_scope_must_match_own_task(service, container, key, value):
    db = Database()
    metrics = db.rows["pipeline_tasks"][0]["metrics_jsonb"]
    target = metrics["observation"] if container == "observation" else metrics
    target[key] = value
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 422
    assert db.writes == []


def test_valid_zero_observation_can_include_unknown_null_counters(service):
    db = Database()
    metrics = db.rows["pipeline_tasks"][0]["metrics_jsonb"]
    metrics["observation"]["values"] = {"likes": 0, "views": None}
    assert apply(service, db)["status"] == "ok"


def test_compact_config_without_timestamp_uses_small_json_guard(service):
    db = Database()
    db.config.clear()
    db.config.update(domain_slug="ai_content", feedback_plan={"checkpoints_hours": [1, 3, 24]},
                     sop_latest_version=1, sop_snapshots=[{"version": 1}])
    db.rows["channel_accounts"][0]["updated_at"] = None
    original_table = db.table
    filters = []
    def table(name):
        query = original_table(name)
        execute = query.execute
        def capture():
            if name == "channel_accounts" and query.operation == "update":
                filters.append(deepcopy(query.filters))
            return execute()
        query.execute = capture
        return query
    db.table = table
    assert apply(service, db)["version"] == 2
    assert any(key == "config_jsonb" for key, _ in filters[0])
    assert len(urlencode([(key, "eq." + str(value)) for key, value in filters[0]])) < 3000


def test_large_config_without_version_field_uses_null_version_guard(service):
    db = Database()
    db.config["archived_source_text"] = "x" * 33787
    db.config.pop("sop_latest_version")
    assert apply(service, db)["version"] == 2


def test_large_confirmation_through_real_postgrest_uses_short_request_urls(service):
    import httpx
    from postgrest import SyncPostgrestClient

    db = Database()
    db.config["archived_source_text"] = "x" * 33787
    for _ in range(15):
        account_service._append_sop_snapshot(db.config, reason="old checkpoint", changed_fields=[], actor="operator")
    body = json.loads(db.item["content"])
    body["proposal"]["before_version"] = 16
    db.item["content"] = json.dumps(body)
    request_lengths = []
    account_updates = []

    def respond(request):
        request_lengths.append(len(str(request.url)))
        assert request_lengths[-1] <= 3000
        table = request.url.path.rsplit("/", 1)[-1]
        query = db.table(table)
        if request.method == "PATCH":
            query.update(json.loads(request.content))
            if table == "channel_accounts":
                params = request.url.params
                assert "config_jsonb" not in params
                assert params["updated_at"].startswith("eq.")
                assert params["config_jsonb->>sop_latest_version"].startswith("eq.")
                account_updates.append(request)
        elif request.method == "POST":
            query.insert(json.loads(request.content))
        else:
            assert request.method == "GET"
        for key, value in request.url.params.multi_items():
            if key == "select":
                continue
            if key == "limit":
                query.limit(int(value))
            elif value == "is.null":
                query.is_(key, "null")
            else:
                assert value.startswith("eq.")
                query.eq(key, value[3:])
        return httpx.Response(200, json=query.execute().data)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = SyncPostgrestClient("https://data-api.example.test/rest/v1", http_client=http)
        result = apply(service, client)
    assert result["version"] == 17
    assert result["receipt"]["stage"] == "completed"
    assert len(account_updates) == 5
    assert max(request_lengths) <= 3000
    assert db.config["archived_source_text"] == "x" * 33787


def snapshot_evidence(db, selector="nested_id"):
    task = db.rows["pipeline_tasks"][0]
    metrics = task["metrics_jsonb"]
    observation = deepcopy(metrics["observation"])
    snapshot = {"id": "observation-old", "pipeline_task_id": task["id"], "metric_window": "observed",
                "captured_at": observation["observed_at"],
                "metrics_jsonb": {"metrics_mode": "real", "post_metrics": deepcopy(observation["values"]),
                                  "observation": observation}}
    db.rows["task_metrics"] = [snapshot]
    metrics["observation_id"] = snapshot["id"]
    ref = {"task_id": task["id"], "source_type": "pipeline_tasks.metrics_jsonb", "source_ref": URL,
           "timestamp": observation["observed_at"], "provider": observation["provider"],
           "account_id": "account-1", "domain_id": "domain-1"}
    if selector == "nested_id":
        ref["observation"] = {**deepcopy(observation), "observation_id": snapshot["id"]}
    elif selector == "id":
        ref["observation_id"] = snapshot["id"]
    elif selector == "row_id":
        ref.update(source_type="task_metrics", id=snapshot["id"])
    elif selector == "bare":
        ref = {"source_ref": task["id"]}
    body = json.loads(db.item["content"])
    body["evidence_refs"] = [ref]
    db.item["content"] = json.dumps(body)
    return snapshot


def newer_observation(db):
    task = db.rows["pipeline_tasks"][0]
    newer = deepcopy(db.rows["task_metrics"][0])
    newer["id"] = "observation-new"
    observed = (datetime.fromisoformat(newer["captured_at"]) + timedelta(minutes=30)).isoformat()
    newer["captured_at"] = observed
    newer["metrics_jsonb"]["post_metrics"] = {"likes": 12}
    newer["metrics_jsonb"]["observation"].update(values={"likes": 12}, observed_at=observed)
    db.rows["task_metrics"].append(newer)
    task["metrics_jsonb"] = {**deepcopy(newer["metrics_jsonb"]), "post_metrics_synced_at": observed,
                             "observation_id": newer["id"]}


@pytest.mark.parametrize("selector", ["id", "nested_id", "row_id", "timestamp", "bare"])
def test_completed_retry_uses_referenced_immutable_snapshot(service, selector):
    db = Database()
    original = snapshot_evidence(db, selector)
    first = apply(service, db)
    newer_observation(db)
    writes = deepcopy(db.writes)
    retry = apply(service, db)
    assert retry["receipt"] == first["receipt"]
    assert retry["version"] == 2
    assert retry["receipt"]["evidence_refs"][0]["observation_id"] == original["id"]
    assert db.writes == writes


@pytest.mark.parametrize("stage", ["prepared", "configured", "checkpointed", "audited"])
@pytest.mark.parametrize("selector", ["id", "nested_id", "timestamp"])
def test_interrupted_retry_uses_referenced_immutable_snapshot(service, stage, selector):
    db = Database()
    original = snapshot_evidence(db, selector)
    db.failure = lambda table, payload: table == "channel_accounts" and any(
        receipt.get("stage") == stage
        for receipt in payload["config_jsonb"].get("strategy_confirmation_receipts", {}).values())
    db.persist_on_failure = True
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 503
    assert db.config["strategy_confirmation_receipts"]["item-1"]["stage"] == stage
    newer_observation(db)
    result = apply(service, db)
    assert result["receipt"]["stage"] == "completed"
    assert result["receipt"]["evidence_refs"][0]["observation_id"] == original["id"]
    assert result["version"] == 2
    assert len(db.config["sop_snapshots"]) == 2
    assert len(db.rows["audit_logs"]) == 1
    assert db.item["status"] == "active"


@pytest.mark.parametrize("selector", ["id", "nested_id", "timestamp"])
def test_pending_confirmation_uses_old_snapshot_not_latest_metrics(service, selector):
    db = Database()
    original = snapshot_evidence(db, selector)
    newer_observation(db)
    result = apply(service, db)
    assert result["receipt"]["evidence_refs"][0]["observation_id"] == original["id"]
    assert result["sop_latest"]["context"]["evidence_refs"] == result["receipt"]["evidence_refs"]


@pytest.mark.parametrize("state", ["completed", "interrupted"])
@pytest.mark.parametrize("correction", ["snapshot_marker", "pending_invalidation", "identity", "publication_time"])
def test_snapshot_retry_still_rejects_publication_correction(service, state, correction):
    db = Database()
    snapshot = snapshot_evidence(db)
    if state == "interrupted":
        db.failure = lambda table, payload: table == "audit_logs"
        with pytest.raises(HTTPException) as exc:
            apply(service, db)
        assert exc.value.status_code == 503
    else:
        apply(service, db)
    newer_observation(db)
    task = db.rows["pipeline_tasks"][0]
    if correction == "snapshot_marker":
        snapshot["metrics_jsonb"]["publication_invalidated"] = {"revision": "corrected"}
    elif correction == "pending_invalidation":
        task["publish_jsonb"]["manual_publication"]["snapshot_invalidations"] = [
            {"id": snapshot["id"], "metrics_jsonb": deepcopy(snapshot["metrics_jsonb"])}]
    elif correction == "identity":
        corrected = "6ac3b3a200000000140395fd"
        task["publish_jsonb"]["identity"] = {"feed_id": corrected, "remote_post_id": corrected,
                                               "published_url": URL.replace(NOTE, corrected)}
    else:
        published = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        task["published_at"] = task["publish_jsonb"]["published_at"] = published
    writes = deepcopy(db.writes)
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 422
    assert db.writes == writes


@pytest.mark.parametrize("mutation", ["foreign_task", "missing_row", "missing_provenance", "synthetic",
                                      "future", "capture_mismatch", "nested_mismatch", "ambiguous_time"])
def test_snapshot_reference_requires_scoped_actual_immutable_row(service, mutation):
    db = Database()
    snapshot = snapshot_evidence(db, "timestamp" if mutation == "ambiguous_time" else "nested_id")
    if mutation == "foreign_task":
        snapshot["pipeline_task_id"] = "other-task"
    elif mutation == "missing_row":
        db.rows["task_metrics"] = []
    elif mutation == "missing_provenance":
        snapshot["metrics_jsonb"]["observation"].pop("provenance")
    elif mutation == "synthetic":
        snapshot["metrics_jsonb"]["metrics_mode"] = "synthetic"
    elif mutation == "future":
        snapshot["captured_at"] = snapshot["metrics_jsonb"]["observation"]["observed_at"] = (
            datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    elif mutation == "capture_mismatch":
        snapshot["captured_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    elif mutation == "nested_mismatch":
        body = json.loads(db.item["content"])
        body["evidence_refs"][0]["observation"]["source_ref"] = URL.replace(NOTE, "6ac3b3a200000000140395fd")
        db.item["content"] = json.dumps(body)
    else:
        duplicate = {**deepcopy(snapshot), "id": "ambiguous-observation"}
        db.rows["task_metrics"].append(duplicate)
    with pytest.raises(HTTPException) as exc:
        apply(service, db)
    assert exc.value.status_code == 422
    assert db.writes == []


def test_legacy_actual_feedback_reference_may_have_null_nested_observation_id(service):
    db = Database()
    observation = deepcopy(db.rows["pipeline_tasks"][0]["metrics_jsonb"]["observation"])
    body = json.loads(db.item["content"])
    body["evidence_refs"] = [{"task_id": "task-1", "source_ref": URL,
                              "timestamp": observation["observed_at"],
                              "observation": {**observation, "observation_id": None}}]
    db.item["content"] = json.dumps(body)
    assert apply(service, db)["status"] == "ok"


@pytest.mark.parametrize("selector", ["nested_id", "timestamp"])
def test_pre_fix_receipt_resolves_immutable_evidence_without_top_level_id(service, selector):
    db = Database()
    snapshot_evidence(db, selector)
    first = apply(service, db)
    receipt = db.config["strategy_confirmation_receipts"]["item-1"]
    receipt["evidence_refs"] = json.loads(db.item["content"])["evidence_refs"]
    newer_observation(db)
    writes = deepcopy(db.writes)
    result = apply(service, db)
    assert result["receipt"] == receipt
    assert result["version"] == first["version"]
    assert db.writes == writes


def test_multiple_refs_for_same_task_resolve_their_own_snapshots(service):
    db = Database()
    old = snapshot_evidence(db)
    newer_observation(db)
    body = json.loads(db.item["content"])
    newer = db.rows["task_metrics"][-1]
    body["evidence_refs"].append({"task_id": "task-1", "source_ref": URL,
                                  "observation_id": newer["id"], "timestamp": newer["captured_at"]})
    db.item["content"] = json.dumps(body)
    result = apply(service, db)
    assert [ref["observation_id"] for ref in result["receipt"]["evidence_refs"]] == [old["id"], newer["id"]]


def test_scoped_snapshot_queries_are_bounded_and_do_not_use_full_json_filters(service):
    db = Database()
    snapshot_evidence(db, "timestamp")
    newer_observation(db)
    original_table = db.table
    reads = []
    def table(name):
        query = original_table(name)
        execute = query.execute
        def bounded():
            if name == "task_metrics":
                assert query.operation == "select"
                assert query.bound == 2
                assert ("pipeline_task_id", "task-1") in query.filters
                assert any(key in {"id", "captured_at"} for key, _ in query.filters)
                assert all("jsonb" not in key for key, _ in query.filters)
                reads.append(deepcopy(query.filters))
            return execute()
        query.execute = bounded
        return query
    db.table = table
    assert apply(service, db)["status"] == "ok"
    assert reads
