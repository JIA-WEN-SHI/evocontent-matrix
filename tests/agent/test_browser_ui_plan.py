from unittest.mock import Mock
import pytest

from app.orchestration import daily_ops
from app.orchestration import pipeline_runner


def test_cover_prompt_matches_new_domain():
    helper = getattr(pipeline_runner, "_draft_image_prompt", None)
    assert helper is not None
    prompt = helper({"name": "AI 内容分享"}, "4步搭好AI工作流", "原创教程")
    assert "AI 内容分享" in prompt
    assert "日系" not in prompt


def strategy():
    return {"collection_plan": {"mode": "browser_ui", "steps": [
        {"tool": "browser_ui_search", "query": "AI 工作流", "limit": 3},
    ]}}


def test_local_cli_plan_never_falls_back_to_old_collectors(monkeypatch):
    plan = {'collection_plan': {'mode':'xhs_cli'}}
    for name in ('_execute_collection_plan_steps', '_collect_news_intel', 'get_mcp_readonly_status', '_collect_xhs_search_intel'):
        monkeypatch.setattr(daily_ops,name,Mock(side_effect=AssertionError('Unexpected fallback')))
    assert daily_ops._collect_hotspots({},plan) == []
    assert daily_ops._collect_viewpoints({},plan) == []
    handoff = daily_ops._browser_ui_handoff(plan,account_id='a',domain_slug='ai_content')
    assert handoff['status'] == 'awaiting_browser'
    assert handoff['reason'] == 'local_cli_requires_account_collect'


def test_browser_ui_collectors_never_fall_back_to_mcp_news_or_playwright(monkeypatch):
    sources = ["_execute_collection_plan_steps", "_collect_news_intel", "get_mcp_readonly_status", "_collect_xhs_search_intel"]
    for name in sources:
        monkeypatch.setattr(daily_ops, name, Mock(side_effect=AssertionError("Unexpected background collector")))
    assert daily_ops._collect_hotspots({}, strategy()) == []
    assert daily_ops._collect_viewpoints({}, strategy()) == []


def test_browser_ui_run_reports_handoff_instead_of_fake_success(monkeypatch):
    monkeypatch.setattr(daily_ops, "_load_domain", lambda *a: {"id": "d"})
    monkeypatch.setattr(daily_ops, "_load_channel_account_by_id", lambda *a: {"id": "a"})
    monkeypatch.setattr(daily_ops, "strategy_from_account", lambda *a: strategy())
    monkeypatch.setattr(daily_ops, "upsert_account_runtime", Mock(side_effect=AssertionError("Must not launch background browser")))
    result = daily_ops.run_account_collection(object(), "ai_content", account_id="a")
    assert result["status"] == "awaiting_browser"
    assert result["collected"] == result["inserted"] == 0
    assert "Chrome" in result["message"]


def test_ai_style_samples_do_not_load_legacy_local_fixtures(monkeypatch):
    monkeypatch.setattr(pipeline_runner, "_fetch_db_case_style_samples", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_runner, "_load_local_style_samples", Mock(side_effect=AssertionError("Legacy niche leaked")))
    assert pipeline_runner._collect_style_samples(object(), domain_id="ai", domain_slug="ai_content", account_id="a") == []


@pytest.mark.parametrize("flow", ["full", "viewpoint"])
def test_daily_browser_ui_flows_stop_before_drafting_or_autopublishing(monkeypatch, flow):
    monkeypatch.setattr(daily_ops, "_load_domain", lambda *a: {"id": "d"})
    monkeypatch.setattr(daily_ops, "_daily_channel_and_selector", lambda *a: ("xiaohongshu", ""))
    monkeypatch.setattr(daily_ops, "_pick_channel_account", lambda *a: {"id": "a"})
    monkeypatch.setattr(daily_ops, "strategy_from_account", lambda *a: {**strategy(), "account_id": "a"})
    for name in ("upsert_account_runtime", "_start_run_ledger", "_create_daily_pipeline_task", "_run_pipeline_lifecycle"):
        monkeypatch.setattr(daily_ops, name, Mock(side_effect=AssertionError("Must stop for browser assistance")))
    fn = daily_ops.run_daily_ops if flow == "full" else daily_ops.run_daily_viewpoint_ops
    result = fn(object(), "ai_content", **({"force": True} if flow == "viewpoint" else {}))
    assert result["status"] == "awaiting_browser"


def test_waiting_for_browser_is_not_retried_as_a_failure(monkeypatch):
    waiting = {"status": "awaiting_browser", "reason": "browser_ui_requires_assistance", "message": "Chrome"}
    run = Mock(return_value=waiting)
    monkeypatch.setattr(daily_ops, "run_daily_ops", run)
    monkeypatch.setattr(daily_ops, "_safe_insert_report", lambda *a, **kw: None)
    monkeypatch.setattr(daily_ops, "_safe_finish_report", Mock())
    result = daily_ops.run_daily_ops_with_retry(object(), "ai_content", max_attempts=3, retry_backoff_seconds=0)
    assert result["status"] == "awaiting_browser"
    assert run.call_count == 1

