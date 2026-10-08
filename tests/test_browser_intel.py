from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app import models
from app.services.account_service import _normalize_collection_plan


def test_browser_ui_plan_is_preserved_and_account_scoped():
    plan = _normalize_collection_plan({"mode": "browser_ui", "steps": [
        {"tool": "browser_ui_search", "query": "AI 工作流", "limit": 3},
    ], "fallback": []})
    assert plan["mode"] == "browser_ui"
    assert plan["steps"][0]["tool"] == "browser_ui_search"
    assert plan["steps"][0]["scope"] == "account"


def test_browser_ui_route_shows_assistance_and_no_readonly_fallback():
    from app.services.execution_route_service import resolve_execution_route_view
    view = resolve_execution_route_view(account_config={}, domain_config={}, collection_mode="browser_ui", account_id="a", domain_slug="ai_content")
    assert "Chrome" in view["primary_route"]
    assert "不启用" in view["readonly_bridge"]
    assert "不会自动" in view["user_facing_status"]


def valid_item(**overrides):
    return {"source_url": "https://www.xiaohongshu.com/search_result/6a352608000000001503f510",
            "title": "AI 工作流", "raw_text": "页面中可见的工作流讲解摘要", **overrides}


@pytest.mark.parametrize("overrides", [
    {"source_url": "http://127.0.0.1/private"},
    {"source_url": "https://www.xiaohongshu.com/user/profile/abc"},
    {"source_url": "https://www.xiaohongshu.com.evil.test/explore/6a352608000000001503f510"},
    {"source_url": "https://user:secret@www.xiaohongshu.com/explore/6a352608000000001503f510"},
    {"raw_text": "  "},
    {"captured_at": datetime(2099, 1, 1, tzinfo=timezone.utc)},
])
def test_browser_evidence_rejects_invalid_targets_and_empty_content(overrides):
    item_model = getattr(models, "BrowserIntelItem", None)
    assert item_model is not None, "Browser evidence input validation is missing"
    with pytest.raises(ValidationError):
        item_model(**valid_item(**overrides))


def test_browser_evidence_strips_transient_query_tokens():
    item_model = getattr(models, "BrowserIntelItem", None)
    assert item_model is not None
    item = item_model(**valid_item(source_url=valid_item()["source_url"] + "?xsec_token=transient"))
    assert item.source_url == valid_item()["source_url"].replace("/search_result/", "/explore/")


def test_import_is_scoped_deduplicated_and_does_not_invent_metrics(monkeypatch):
    from app.services import browser_intel
    client = Mock()
    accounts, domains, intel = Mock(), Mock(), Mock()
    client.table.side_effect = lambda name: {"channel_accounts": accounts, "domains": domains, "intelligence_items": intel}[name]
    accounts.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [{"id": "a", "is_active": True, "channel": "xiaohongshu", "config_jsonb": {"onboarding": {"domain_slug": "ai_content"}}}]
    domains.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [{"id": "d"}]
    intel.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    intel.insert.return_value.execute.return_value.data = [{"id": "i"}]
    monkeypatch.setattr(browser_intel, "write_audit_log", Mock())
    item = models.BrowserIntelItem(**valid_item())
    request = models.BrowserIntelImportRequest(domain_slug="ai_content", items=[item, item])
    result = browser_intel.import_browser_intel(client, account_id="a", request=request, actor=models.Actor(user_id="test", role="admin"))
    assert result["inserted"] == 1
    assert result["duplicates"] == 1
    payload = intel.insert.call_args.args[0]
    assert payload["account_id"] == "a"
    assert payload["domain_id"] == "d"
    assert payload["meta_jsonb"]["provider"] == "chrome_browser_ui"
    assert "metrics" not in payload["meta_jsonb"]
    filters = [call.args for call in intel.mock_calls if call.args and call.args[0] == "source_type"]
    assert ("source_type", "hotspot_chrome_browser_ui") in filters
    hotspot_id = payload["id"]
    browser_intel.import_browser_intel(client, account_id="a", request=models.BrowserIntelImportRequest(domain_slug="ai_content", source_kind="viewpoint", items=[item]), actor=models.Actor(user_id="test", role="admin"))
    assert intel.insert.call_args.args[0]["id"] != hotspot_id


@pytest.mark.parametrize("lost_result", ["timeout", "empty"])
def test_uncertain_insert_reads_back_deterministic_id_without_replaying_write(monkeypatch, lost_result):
    from app.services import browser_intel
    from fake_store import MemoryStore
    client = MemoryStore()
    client.tables["channel_accounts"] = [{"id": "a", "is_active": True, "channel": "xiaohongshu", "config_jsonb": {}}]
    account = client.tables["channel_accounts"][0]
    domain = client.tables["domains"][0]
    original_table = client.table
    def table(name):
        query = original_table(name)
        if name == "intelligence_items":
            original_execute = query.execute
            def execute():
                response = original_execute()
                if query.action == "insert":
                    if lost_result == "empty":
                        return SimpleNamespace(data=[])
                    raise TimeoutError("committed but response lost")
                return response
            query.execute = execute
        return query
    client.table = table
    monkeypatch.setattr(browser_intel, "write_audit_log", Mock())
    request = models.BrowserIntelImportRequest(domain_slug=domain["slug"], items=[models.BrowserIntelItem(**valid_item())])
    result = browser_intel.import_browser_intel(client, account_id=account["id"], request=request, actor=models.Actor(user_id="test", role="admin"))
    assert result["inserted"] == 1 and len(client.tables["intelligence_items"]) == 1

