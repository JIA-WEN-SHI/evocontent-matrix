from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.tools.collection import playwright_tools as collection
from app.orchestration import daily_ops
import httpx
import json


@pytest.fixture
def browser(monkeypatch):
    page = SimpleNamespace(url="https://example.test/previous-post")
    scope = Mock(side_effect=lambda *a, **k: nullcontext(page))
    monkeypatch.setattr(collection, "_get_channel_account_runtime", lambda *a: {"id": "a"})
    monkeypatch.setattr(collection, "upsert_account_runtime", lambda *a: None)
    monkeypatch.setattr(collection, "mark_account_runtime", lambda *a, **k: None)
    monkeypatch.setattr(collection, "account_execution_scope", lambda *a, **k: nullcontext())
    monkeypatch.setattr(collection, "playwright_page_scope", scope)
    monkeypatch.setattr(collection, "_has_xhs_login_gate", lambda *a: False)
    for name in ("_navigate_if_needed", "_restore_to_next_page", "_search_keyword_via_page",
                 "_open_url_in_current_page", "_expand_post_full_text"):
        monkeypatch.setattr(collection, name, lambda *a, **k: None)
    return page, scope


def test_detail_preserves_traditional_chinese_and_normalizes_whitespace(browser):
    page, _ = browser
    page.evaluate = lambda *a: {"title": "聽見需求", "body": "聽取\u00a0真實\n意見", "url": page.url}
    result = collection.collect_post_detail(object(), account_id="a", url=page.url)
    assert result["status"] == "ok"
    assert result["items"][0]["title"] == "聽見需求"
    assert result["items"][0]["body"] == "聽取 真實 意見"


@pytest.mark.parametrize("tool,params,error", [
    ("search_keyword", {"query": " \t"}, "missing_query"),
    ("collect_profile_posts", {"profile_hint": " "}, "missing_profile_hint"),
    ("collect_post_detail", {"url": " "}, "missing_url"),
])
def test_missing_target_cannot_collect_previous_page(browser, monkeypatch, tool, params, error):
    page, scope = browser
    page.evaluate = lambda *a: {"body": "previous unrelated post", "url": page.url}
    monkeypatch.setattr(collection, "_collect_note_cards", lambda *a, **k: [{"url": page.url}])
    result = collection.run_playwright_tool(object(), account_id="a", tool_name=tool, params=params)
    assert result["status"] == "error"
    assert result["error"] == error
    assert result["items"] == []
    scope.assert_not_called()


@pytest.mark.parametrize("snapshot", [None, {}, {"title": "Dashboard", "body": " \n", "metrics": []}])
def test_empty_dashboard_is_not_evidence(browser, monkeypatch, snapshot):
    monkeypatch.setattr(collection, "_collect_dashboard_snapshot", lambda *a: snapshot)
    result = collection.collect_creator_dashboard(object(), account_id="a")
    assert result["status"] == "error"
    assert result["error"] == "empty_dashboard_snapshot"
    assert result["collected_count"] == 0


@pytest.mark.parametrize("tool", ["collect_creator_dashboard", "collect_recent_published_posts"])
def test_creator_login_gate_cannot_be_collected_as_feedback(browser, monkeypatch, tool):
    monkeypatch.setattr(collection, "_has_xhs_login_gate", lambda *a: True)
    monkeypatch.setattr(collection, "_open_creator_note_manager", lambda *a: None)
    monkeypatch.setattr(collection, "_wait_for_results_loaded", lambda *a: True)
    snapshot = Mock(return_value={"title": "Login", "body": "扫码登录", "metrics": ["123456"]})
    cards = Mock(return_value=[{"title": "unrelated", "url": "https://example.test/post"}])
    monkeypatch.setattr(collection, "_collect_dashboard_snapshot", snapshot)
    monkeypatch.setattr(collection, "_collect_profile_note_cards", cards)
    result = collection.run_playwright_tool(object(), account_id="a", tool_name=tool)
    assert result["status"] == "error"
    assert result["error"] == "login_required"
    assert result["items"] == []
    snapshot.assert_not_called()
    cards.assert_not_called()


def test_valid_dashboard_preserves_observed_metrics(browser, monkeypatch):
    monkeypatch.setattr(collection, "_collect_dashboard_snapshot", lambda *a: {
        "title": "创作者后台", "body": "阅读 0", "metrics": ["阅读 0"], "url": "https://example.test/dashboard",
    })
    result = collection.collect_creator_dashboard(object(), account_id="a")
    assert result["status"] == "ok"
    assert result["items"][0]["metrics"] == ["阅读 0"]


def test_note_manager_navigation_exhaustion_is_reported(monkeypatch):
    page = Mock()
    page.get_by_text.return_value.first.click.side_effect = RuntimeError("selector missing")
    monkeypatch.setattr(collection, "_navigate_if_needed", Mock(side_effect=[
        None, RuntimeError("unreachable"), RuntimeError("unreachable"), RuntimeError("unreachable"),
    ]))
    with pytest.raises(RuntimeError, match="note_manager_unavailable"):
        collection._open_creator_note_manager(page)


@pytest.fixture
def cli_daily(monkeypatch):
    settings = SimpleNamespace(browser_bridge_api_url="http://api.test", internal_service_token="test-token",
                               daily_viewpoint_enabled=True, daily_run_max_attempts=3,
                               daily_retry_backoff_seconds=0)
    account = {"id": "d8cc2285-dba3-405b-ac1b-505993d12cec", "channel": "xiaohongshu",
               "config_jsonb": {"collection_plan": {"mode": "xhs_cli"}}}
    monkeypatch.setattr(daily_ops, "get_settings", lambda: settings)
    monkeypatch.setattr(daily_ops, "_load_domain", lambda *a: {"id": "domain"})
    monkeypatch.setattr(daily_ops, "_daily_channel_and_selector", lambda *a: ("xiaohongshu", ""))
    monkeypatch.setattr(daily_ops, "_pick_channel_account", lambda *a: account)
    for name in ("_collect_hotspots", "_collect_viewpoints", "_start_run_ledger",
                 "_run_pipeline_lifecycle", "upsert_account_runtime"):
        monkeypatch.setattr(daily_ops, name, Mock(side_effect=AssertionError("Legacy flow must not execute")))
    monkeypatch.setattr(daily_ops, "_safe_insert_report", lambda *a, **kw: None)
    monkeypatch.setattr(daily_ops, "_safe_finish_report", lambda *a, **kw: None)
    return account


def cli_transport(monkeypatch, handler):
    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client_type(**kw, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("flow", ["full", "viewpoint"])
def test_cli_daily_delegates_scoped_flow_without_legacy_collector(cli_daily, monkeypatch, flow):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "result": {
            "status": "pending_review", "pipeline_task_id": "draft", "evidence_refs": ["case"]}})
    cli_transport(monkeypatch, handle)
    fn = daily_ops.run_daily_ops if flow == "full" else daily_ops.run_daily_viewpoint_ops
    result = fn(object(), "ai_content", run_key="one-run", triggered_by="scheduler:daily",
                preferred_account_id=cli_daily["id"])
    assert result["status"] == "pending_review"
    assert result["pipeline_task_id"] == "draft"
    assert result["evidence_refs"] == ["case"]
    assert result["execution_owner"] == "api"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"http://api.test/api/accounts/{cli_daily['id']}/loop/run-daily"
    assert json.loads(request.content) == {"domain_slug": "ai_content", "flow": flow, "run_key": "one-run"}
    assert request.headers["x-user-role"] == "operator"
    assert request.headers["x-user-id"] == "scheduler:daily"
    assert request.headers["x-internal-token"] == "test-token"


@pytest.mark.parametrize("state", ["pending_review", "approved", "blocked", "awaiting_status", "failed"])
def test_cli_retry_wrapper_returns_actual_state_once_with_same_key(cli_daily, monkeypatch, state):
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "result": {"status": state, "reason": "actual-reason"}})
    cli_transport(monkeypatch, handle)
    result = daily_ops.run_daily_ops_with_retry(object(), "ai_content", run_key="stable",
                                               max_attempts=3, retry_backoff_seconds=0)
    assert result["status"] == state
    assert result["reason"] == "actual-reason"
    assert requests == [{"domain_slug": "ai_content", "flow": "full", "run_key": "stable"}]


def test_cli_daily_timeout_is_unknown_and_not_replayed(cli_daily, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout("secret must not leak", request=request)
    cli_transport(monkeypatch, handle)
    result = daily_ops.run_daily_ops_with_retry(object(), "ai_content", run_key="stable", max_attempts=3)
    assert result["status"] == "awaiting_status"
    assert result["reason"] == "cli_loop_api_unavailable"
    assert "secret" not in str(result)
    assert len(requests) == 1


@pytest.mark.parametrize("body", [[], {"status": "ok"}, {"status": "ok", "result": {"title": "not a status"}}])
def test_cli_daily_missing_actual_status_cannot_claim_success(cli_daily, monkeypatch, body):
    cli_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    result = daily_ops.run_daily_ops(object(), "ai_content", run_key="stable")
    assert result["status"] == "awaiting_status"
    assert result["reason"] == "cli_loop_invalid_response"


@pytest.mark.parametrize("flow", ["full", "viewpoint"])
def test_cli_http_daily_does_not_hold_serial_executor_while_api_calls_back(cli_daily, monkeypatch, flow):
    from app import main
    monkeypatch.setattr(main, "get_supabase", lambda: object())
    monkeypatch.setattr(main, "_run_playwright_serial", Mock(side_effect=AssertionError("Recursive lock")))
    def handle(request):
        # Simulate the API calling the draft endpoint while the daily request waits.
        return httpx.Response(200, json={"status": "ok", "result": {
            "status": main.run_pipeline_task("draft")["result"]["status"]}})
    monkeypatch.setattr(main, "run_pipeline_task_service", lambda *a: {"status": "pending_review"})
    cli_transport(monkeypatch, handle)
    fn = main.run_daily_domain_ops if flow == "full" else main.run_daily_domain_viewpoint_ops
    result = fn("ai_content", run_key="stable", preferred_account_id=cli_daily["id"])
    assert result["status"] == "pending_review"


def test_non_cli_daily_http_entry_keeps_serial_browser_dispatch(monkeypatch):
    from app import main
    client = object()
    monkeypatch.setattr(main, "get_supabase", lambda: client)
    monkeypatch.setattr(daily_ops, "_load_domain", lambda *a: {"id": "domain"})
    monkeypatch.setattr(daily_ops, "_daily_channel_and_selector", lambda *a: ("xiaohongshu", ""))
    monkeypatch.setattr(daily_ops, "_pick_channel_account", lambda *a: {"id": "a"})
    serial = Mock(return_value={"status": "awaiting_browser"})
    monkeypatch.setattr(main, "_run_playwright_serial", serial)
    result = main.run_daily_domain_ops("ai_content", preferred_account_id="a")
    assert result["status"] == "awaiting_browser"
    assert serial.call_count == 1
    assert serial.call_args.args == (main.run_daily_ops_with_retry, client)
