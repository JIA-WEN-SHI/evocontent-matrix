from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

import httpx
import pytest

from app.execution import rpa
from app.agents.subagents import collector_agent
from app.orchestration.publish_feedback import _extract_real_metrics
from app.orchestration.review_history import compare_review_history


def db_with(rows):
    client = Mock()

    def table(name):
        query = Mock()
        for method in ("select", "eq", "is_", "order", "limit"):
            getattr(query, method).return_value = query
        query.execute.return_value = SimpleNamespace(data=rows.get(name, []))
        return query

    client.table.side_effect = table
    return client


@pytest.fixture
def rpa_context(monkeypatch):
    settings = SimpleNamespace(supabase_url="https://project.example.test", supabase_service_role_key="test-only-key")
    monkeypatch.setattr(rpa, "get_settings", lambda: settings)
    client = db_with({"domains": [{"id": "d"}], "rpa_task_templates": [
        {"task_type": "collect_case", "entity_type": "case", "octopus_flow_id": "actual-configured-flow"}]})
    transport = MagicMock()
    factory = Mock(return_value=transport)
    transport.__enter__.return_value = transport
    monkeypatch.setattr(rpa.httpx, "Client", factory)
    response = Mock(status_code=200)
    response.json.return_value = {"status": "ok", "run_id": "run-1", "octopus_run_id": "remote-1"}
    transport.post.return_value = response
    instruction = {"instruction_id": "stable-id", "task_type": "collect_case", "entity_type": "case", "params": {"keyword": "sample", "limit": 3}}
    return settings, client, transport, factory, response, instruction


def dispatch(context):
    return rpa.dispatch_collection(context[1], domain_slug="topic", account_id="a", instruction=context[5])


def test_rpa_uses_existing_edge_contract_once_with_timeout_and_ack_not_completion(rpa_context):
    result = dispatch(rpa_context)
    _, _, transport, factory, _, _ = rpa_context
    factory.assert_called_once_with(timeout=10.0, follow_redirects=False)
    transport.post.assert_called_once()
    args, kwargs = transport.post.call_args
    assert args == ("https://project.example.test/functions/v1/rpa-dispatch",)
    assert kwargs["json"] == {"instruction_id": "stable-id", "domain_slug": "topic", "account_id": "a",
                              "task_type": "collect_case", "entity_type": "case", "source_run_id": "stable-id",
                              "params": {"keyword": "sample", "limit": 3}}
    assert result["status"] == "dispatched" and result["collection_complete"] is False
    assert result["readiness"]["executor_verified"] is True
    assert "test-only-key" not in str(result)


@pytest.mark.parametrize("kind", ["configuration", "instruction", "template", "unsupported"])
def test_unconfigured_rpa_requires_manual_handoff_without_http(rpa_context, kind):
    settings, client, _, factory, _, instruction = rpa_context
    if kind == "configuration":
        settings.supabase_service_role_key = ""
    elif kind == "instruction":
        instruction["instruction_id"] = ""
    elif kind == "template":
        client.table.side_effect = db_with({"domains": [{"id": "d"}]}).table.side_effect
    else:
        instruction.update(task_type="publish", entity_type="asset")
    result = dispatch(rpa_context)
    assert result["status"] == "manual_required" and result["readiness"]["ready"] is False
    factory.assert_not_called()


@pytest.mark.parametrize("kind", ["timeout", "http_error", "missing_run_id", "invalid_json"])
def test_rpa_uncertain_results_are_not_retried_or_reported_success(rpa_context, kind):
    _, _, transport, _, response, _ = rpa_context
    if kind == "timeout":
        transport.post.side_effect = httpx.ReadTimeout("timed out")
    elif kind == "http_error":
        response.status_code = 502
    elif kind == "missing_run_id":
        response.json.return_value = {"status": "ok"}
    else:
        response.json.side_effect = ValueError("invalid JSON")
    result = dispatch(rpa_context)
    assert result["status"] == "manual_required" and result["dispatch_uncertain"] is True
    assert result["instruction_id"] == "stable-id"
    transport.post.assert_called_once()


@pytest.mark.parametrize("status", ["dispatched", "failed", "accepted"])
def test_existing_rpa_runs_are_never_redispatched_or_claimed_collected(rpa_context, status):
    rpa_context[4].json.return_value = {"status": "exists", "run": {"id": "run-1", "instruction_id": "stable-id", "status": status}}
    result = dispatch(rpa_context)
    assert result["status"] == "existing_run" and result["run_status"] == status
    assert result["collection_complete"] is False
    assert result["manual_handoff"]["required"] is (status == "failed")


def test_publish_handoff_contains_draft_but_never_account_credentials():
    result = rpa.publish_handoff({"id": "p", "channel": "xiaohongshu", "payload_jsonb": {"title": "draft", "body": "text"},
                                  "meta_jsonb": {"login_password": "secret", "cookies_json": "private"}})
    assert result["status"] == "failed" and result["manual_handoff"]["title"] == "draft"
    assert "secret" not in str(result) and "private" not in str(result)


def test_collector_connects_failed_collection_to_explicit_rpa_instruction(monkeypatch):
    monkeypatch.setattr(collector_agent, "run_account_collection", lambda *a, **k: {"status": "failed", "reason": "browser_failed"})
    dispatch_mock = Mock(return_value={"status": "dispatched", "run_id": "r", "collection_complete": False})
    monkeypatch.setattr(collector_agent, "dispatch_collection", dispatch_mock)
    result = collector_agent.run_collector_agent(Mock(), domain_slug="topic", account_id="a", triggered_by="test", trace_id="t",
                                                payload={"rpa_instruction": {"instruction_id": "i"}})
    assert result["status"] == "retry_later" and result["retryable"] is False
    assert result["result"]["primary_result"]["reason"] == "browser_failed"
    assert result["evidence_refs"] == []
    dispatch_mock.assert_called_once()


def task(index, *, collects=10, checkpoint=24, version="v1"):
    return {"id": f"task-{index}", "domain_id": "d", "account_id": "a", "channel": "xiaohongshu", "status": "done",
            "published_at": f"2026-09-{index:02d}T00:00:00Z",
            "publish_jsonb": {"feedback_completed_hours": [checkpoint]},
            "payload_jsonb": {"analysis_jsonb": {"active_prompt_versions": {"draft_writer": {"version": version}}}},
            "metrics_jsonb": {"post_metrics": {"views": 100, "collects": collects, "likes": 20}, "post_metrics_synced_at": "2026-09-18T00:00:00Z"}}


def compare(current, rows):
    return compare_review_history(db_with({"pipeline_tasks": rows}), current,
                                  current["metrics_jsonb"]["post_metrics"], _extract_real_metrics)


def test_history_compares_observed_rates_and_links_all_sources():
    current = task(10, collects=5)
    rows = [task(1), task(2, collects=20), task(3, collects=30)]
    before = deepcopy(rows)
    result = compare(current, rows)
    assert result["status"] == "compared" and rows == before
    saved = next(item for item in result["comparisons"] if item["metric"] == "collects")
    assert saved["current_rate"] == .05 and saved["historical_median_rate"] == .2
    assert result["adjust_actions"] and result["keep_actions"]
    assert {ref["source_ref"] for ref in result["evidence_refs"]} == {"task-1", "task-2", "task-3"}
    assert result["causal_claim"] is False


@pytest.mark.parametrize("mismatch", ["account", "domain", "channel", "checkpoint", "future", "synthetic", "zero_views"])
def test_history_excludes_noncomparable_or_unobserved_samples(mismatch):
    current = task(10)
    rows = [task(1), task(2), task(3)]
    row = rows[-1]
    if mismatch in {"account", "domain"}:
        row[f"{mismatch}_id"] = "other"
    elif mismatch == "channel":
        row["channel"] = "other"
    elif mismatch == "checkpoint":
        row["publish_jsonb"]["feedback_completed_hours"] = [1]
    elif mismatch == "future":
        row["published_at"] = "2027-01-01T00:00:00Z"
    elif mismatch == "synthetic":
        row["metrics_jsonb"]["metrics_mode"] = "synthetic_preview_only"
    else:
        row["metrics_jsonb"]["post_metrics"]["views"] = 0
    result = compare(current, rows)
    assert result["status"] == "insufficient_history"
    assert result["keep_actions"] == result["adjust_actions"] == result["discard_actions"] == []


def test_retirement_is_only_a_confirmable_candidate_for_repeated_identified_version():
    current = task(10, collects=1, version="v2")
    rows = [task(1), task(2), task(3), task(5, collects=2, version="v2"), task(6, collects=3, version="v2")]
    result = compare(current, rows)
    candidate = result["discard_actions"][0]
    assert candidate["prompt_version"] == "v2"
    assert candidate["confirmation_required"] is True and candidate["auto_apply"] is False
    assert candidate["causal_claim"] is False and len(candidate["history_task_ids"]) == 5
    current["payload_jsonb"] = {}
    assert compare(current, rows)["discard_actions"] == []


def test_single_low_sample_never_proposes_retirement():
    result = compare(task(10, collects=0, version="v2"), [task(1), task(2), task(3)])
    assert result["adjust_actions"] and result["discard_actions"] == []


def test_unavailable_history_is_explicit_and_readonly():
    client = Mock()
    client.table.side_effect = RuntimeError("unavailable")
    current = task(10)
    result = compare_review_history(client, current, current["metrics_jsonb"]["post_metrics"], _extract_real_metrics)
    assert result["status"] == "history_unavailable" and result["discard_actions"] == []
