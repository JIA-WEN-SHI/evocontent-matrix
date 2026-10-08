from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.tools.publishing import publisher


def test_retired_publish_never_clicks_even_when_retries_are_configured(monkeypatch):
    monkeypatch.setattr(publisher, "get_settings", lambda: SimpleNamespace(
        playwright_dry_run=False, playwright_publish_retries=3,
    ))
    attempt = Mock(return_value={"status": "failed", "error": "publish_not_confirmed_after_click", "attempt": 1})
    monkeypatch.setattr(publisher, "_publish_once", attempt)
    monkeypatch.setattr(publisher.time, "sleep", lambda *a: None)
    result = publisher.publish_with_playwright({"id": "p", "channel": "xiaohongshu"})
    assert result["status"] == "blocked"
    assert result["error"] == "delegated_publishing_disabled"
    assert result["attempt"] == 0
    attempt.assert_not_called()


@pytest.mark.parametrize("error_field", ["error", "error_code"])
def test_retired_fallback_never_publishes_or_exposes_credentials(monkeypatch, error_field):
    monkeypatch.setattr(publisher, "resolve_publish_route", lambda *a: {"order": ["playwright", "mcp", "rpa"]})
    primary = {"status": "failed", error_field: "publish_not_confirmed_after_click", "attempt": 1}
    monkeypatch.setattr(publisher, "publish_with_playwright", lambda *a: primary)
    backup = Mock(return_value={"status": "success", "remote_post_id": "duplicate"})
    monkeypatch.setattr(publisher, "publish_with_mcp", backup)
    result = publisher.publish_with_fallback({
        "id": "p", "channel": "xiaohongshu", "payload_jsonb": {"title": "Draft", "body": "Content"},
        "meta_jsonb": {"login_password": "test-secret"},
    })
    assert result["status"] == "blocked"
    assert result["publish_attempts"] == []
    assert "published_url" not in result
    assert "test-secret" not in str(result)
    backup.assert_not_called()
