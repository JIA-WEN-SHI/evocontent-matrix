from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.orchestration import daily_ops, pipeline_runner
from app.tools.publishing import publisher
from app.tools.integrations import xhs_mcp_readonly as mcp
from app.governance.execution_control import is_action_allowed


def test_approved_task_is_preserved_without_publishing(monkeypatch):
    task = {"id": "task", "domain_id": "domain", "status": "approved", "payload_jsonb": {"title": "Draft", "body": "Body"}}
    monkeypatch.setattr(pipeline_runner, "_get_pipeline_task", lambda *a: task)
    monkeypatch.setattr(pipeline_runner, "_get_domain", lambda *a: {"id": "domain"})
    publish = Mock(return_value={"status": "ok", "mode": "publish"})
    monkeypatch.setattr(pipeline_runner, "_publish_pipeline_task", publish)
    result = pipeline_runner.run_pipeline_task(Mock(), "task")
    assert result["status"] == "blocked"
    assert result["reason"] == "delegated_publishing_disabled"
    assert task["status"] == "approved"
    publish.assert_not_called()


@pytest.mark.parametrize("status", ["queued", "intel_ready", "review_rejected"])
def test_draft_generation_remains_available(monkeypatch, status):
    monkeypatch.setattr(pipeline_runner, "_get_pipeline_task", lambda *a: {"id": "task", "domain_id": "domain", "status": status})
    monkeypatch.setattr(pipeline_runner, "_get_domain", lambda *a: {"id": "domain"})
    monkeypatch.setattr(pipeline_runner, "_draft_pipeline_task", lambda *a: {"status": "ok", "mode": "draft"})
    assert pipeline_runner.run_pipeline_task(Mock(), "task") == {"status": "ok", "mode": "draft"}


@pytest.mark.parametrize("method", ["publish_with_playwright", "publish_with_mcp", "publish_with_fallback"])
def test_all_publish_adapters_stop_before_external_side_effects(monkeypatch, method):
    monkeypatch.setattr(publisher, "get_settings", lambda: SimpleNamespace(playwright_dry_run=False, playwright_publish_retries=1, xhs_mcp_write_enabled=True))
    network = Mock(return_value={"status": "success"})
    monkeypatch.setattr(publisher, "_publish_once", network)
    monkeypatch.setattr(publisher, "publish_content_via_mcp", network)
    monkeypatch.setattr(publisher, "resolve_publish_route", lambda *a: {"order": ["playwright", "mcp", "rpa"]})
    result = getattr(publisher, method)({"id": "task", "channel": "xiaohongshu", "payload_jsonb": {"title": "Draft", "body": "Body"}, "meta_jsonb": {"title": "Draft", "body": "Body", "images": ["image.png"]}})
    assert result["status"] == "blocked"
    assert result["error"] == "delegated_publishing_disabled"
    assert result["attempt"] == 0
    network.assert_not_called()


def test_old_auto_publish_settings_cannot_enable_daily_publishing(monkeypatch):
    monkeypatch.setattr(daily_ops, "get_settings", lambda: SimpleNamespace(publish_guard_mode="auto", allow_auto_publish=True, daily_auto_approve_publish=True))
    monkeypatch.setattr(daily_ops, "_automation_policy", lambda *a: {"auto_approve_publish": True})
    assert daily_ops._auto_publish_enabled({}) is False


@pytest.mark.parametrize("tool", ["publish_content", "publish_with_video", "post.publish", "post_comment_to_feed", "reply_comment_in_feed"])
@pytest.mark.parametrize("allow_write", [True, False])
def test_mcp_custom_tools_cannot_bypass_publish_removal(monkeypatch, tool, allow_write):
    monkeypatch.setattr(mcp, "get_settings", lambda: SimpleNamespace(xhs_mcp_base_url="http://invalid", xhs_mcp_timeout_sec=3))
    monkeypatch.setattr(mcp, "get_mcp_write_status", lambda: {"enabled": True, "allowed_tools": [tool]})
    monkeypatch.setattr(mcp, "get_mcp_readonly_status", lambda: {"enabled": True})
    initialize = Mock(return_value=("", "test_network_disabled"))
    monkeypatch.setattr(mcp, "_ensure_mcp_session", initialize)
    monkeypatch.setattr(mcp, "_post_json", Mock(return_value={}))
    result = mcp._call_mcp_tool(tool, {}, allow_write=allow_write)
    assert result["status"] == "blocked"
    assert result["error"] == "delegated_publishing_disabled"
    initialize.assert_not_called()


@pytest.mark.parametrize("action", ["approve_and_publish_pending"])
def test_assistant_cannot_restore_removed_publish_actions(action):
    assert not is_action_allowed(policies={"default_action": "allow", "audiences": {"assistant": {"allow": [action]}}}, audience="assistant", action_type=action)


def test_batch_draft_creation_remains_available():
    assert is_action_allowed(policies={"default_action": "allow"}, audience="assistant", action_type="schedule_posts")


def test_unrecognized_custom_tool_cannot_hide_a_write(monkeypatch):
    monkeypatch.setattr(mcp, "get_settings", lambda: SimpleNamespace(xhs_mcp_base_url="http://invalid", xhs_mcp_timeout_sec=3))
    monkeypatch.setattr(mcp, "get_mcp_readonly_status", lambda: {"enabled": True})
    initialize = Mock(return_value=("", "no_network"))
    monkeypatch.setattr(mcp, "_ensure_mcp_session", initialize)
    monkeypatch.setattr(mcp, "_post_json", Mock(return_value={}))
    result = mcp._call_mcp_tool("sendNote", {})
    assert result["status"] == "blocked"
    initialize.assert_not_called()


def test_note_detail_collection_is_not_blocked(monkeypatch):
    monkeypatch.setattr(mcp, "get_settings", lambda: SimpleNamespace(xhs_mcp_base_url="http://invalid", xhs_mcp_timeout_sec=3))
    monkeypatch.setattr(mcp, "get_mcp_readonly_status", lambda: {"enabled": True})
    monkeypatch.setattr(mcp, "_ensure_mcp_session", lambda *a, **k: ("test-session", ""))
    monkeypatch.setattr(mcp, "_post_json", lambda *a, **k: {"result": {"content": [{"type": "text", "text": '{"comments": []}'}]}})
    result = mcp._call_mcp_tool("get_feed_detail", {"feed_id": "note"})
    assert result["status"] == "ok"
    assert result["result"] == {"comments": []}
