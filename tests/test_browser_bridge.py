from importlib import import_module

import pytest
from fastapi import HTTPException


def store(clock=None):
    module = import_module("app.services.browser_bridge")
    return module.BrowserBridgeStore(clock=clock or (lambda: 100.0))


def connected(bridge):
    code = bridge.create_pairing("account-a", "ai_content", "tester")["code"]
    return bridge.pair(code, "chrome-extension://" + "a" * 32, 12)


def test_pairing_is_single_use_and_account_bound():
    bridge = store()
    code = bridge.create_pairing("account-a", "ai_content", "tester")["code"]
    session = bridge.pair(code, "chrome-extension://" + "a" * 32, 12)
    assert bridge.status("account-a")["connected"] is True
    assert bridge.status("other")["connected"] is False
    assert "token" not in bridge.status("account-a")
    with pytest.raises(HTTPException):
        bridge.pair(code, "chrome-extension://" + "a" * 32, 12)
    bridge.disconnect("account-a")
    with pytest.raises(HTTPException):
        bridge.extension_session(session["token"], "chrome-extension://" + "a" * 32)


def test_pairing_expiry_and_extension_origin():
    now = [100.0]
    bridge = store(lambda: now[0])
    code = bridge.create_pairing("a", "ai_content", "tester")["code"]
    with pytest.raises(HTTPException):
        bridge.pair(code, "https://evil.example", 1)
    now[0] = 401.0
    with pytest.raises(HTTPException):
        bridge.pair(code, "chrome-extension://" + "a" * 32, 1)


def test_pairing_preview_does_not_consume_code_or_expose_actor():
    bridge = store()
    code = bridge.create_pairing("a", "ai_content", "private-actor")["code"]
    preview = bridge.pairing_info(code)
    assert preview == {"account_id": "a", "domain_slug": "ai_content", "mode": "foreground_visual", "protocol_version": 2}
    assert bridge.pair(code, "chrome-extension://" + "a" * 32, 1)["account_id"] == "a"


def test_legacy_pairing_reports_actual_extension_protocol():
    bridge = store()
    code = bridge.create_pairing('a','ai_content','tester')['code']
    session = bridge.pair(code,'chrome-extension://'+'a'*32,1,protocol_version=1)
    assert session['protocol_version'] == 1
    assert bridge.status('a')['protocol_version'] == 1
    job = bridge.start_job('a','ai_content',['AI'],'hotspot')
    assert bridge.job_view(job['id'],'a')['protocol_version'] == 1
    assert bridge.enqueue(job['id'],'observe',{})['protocol_version'] == 1


def test_command_leases_are_once_only_and_stop_rejects_late_results():
    bridge = store()
    session = connected(bridge)
    job = bridge.start_job("account-a", "ai_content", ["AI 办公"], "hotspot")
    command = bridge.enqueue(job["id"], "observe", {})
    assert bridge.lease_command(session["token"], "chrome-extension://" + "a" * 32)["id"] == command["id"]
    assert bridge.lease_command(session["token"], "chrome-extension://" + "a" * 32) is None
    bridge.stop(job["id"], "account-a")
    with pytest.raises(HTTPException):
        bridge.complete_command(session["token"], "chrome-extension://" + "a" * 32, command["id"], {"ok": True})
    assert bridge.job_view(job["id"], "account-a")["status"] == "cancelled"


def test_cross_account_jobs_unknown_actions_and_concurrent_jobs_rejected():
    bridge = store()
    connected(bridge)
    job = bridge.start_job("account-a", "ai_content", ["AI 办公"], "hotspot")
    for action in ["publish", "javascript", "click", "navigate"]:
        with pytest.raises(HTTPException):
            bridge.enqueue(job["id"], action, {})
    with pytest.raises(HTTPException):
        bridge.start_job("account-a", "ai_content", ["AI"], "hotspot")
    with pytest.raises(HTTPException):
        bridge.job_view(job["id"], "other")
    with pytest.raises(HTTPException):
        bridge.start_job("account-a", "other-domain", ["AI"], "hotspot")


def test_command_result_pauses_and_resume_invalidates_old_command():
    bridge = store()
    session = connected(bridge)
    origin = "chrome-extension://" + "a" * 32
    job = bridge.start_job("account-a", "ai_content", ["AI"], "hotspot")
    command = bridge.enqueue(job["id"], "observe", {})
    bridge.lease_command(session["token"], origin)
    bridge.complete_command(session["token"], origin, command["id"], {"ok": False, "paused": True, "message": "请切回小红书标签页"})
    assert bridge.job_view(job["id"], "account-a")["status"] == "awaiting_user"
    bridge.resume(job["id"], "account-a")
    assert bridge.job_view(job["id"], "account-a")["status"] == "running"
    with pytest.raises(HTTPException):
        bridge.complete_command(session["token"], origin, command["id"], {"ok": True})


def test_bridge_routes_reject_network_and_untrusted_browser_origins():
    module = import_module("app.routers.browser_bridge")
    from starlette.requests import Request
    for host, origin in [("192.168.0.3", None), ("127.0.0.1", "https://evil.example"), ("127.0.0.1", "null")]:
        request = Request({"type": "http", "client": (host, 1000), "headers": [(b"origin", origin.encode())] if origin else []})
        with pytest.raises(HTTPException):
            module.local_control(request)
    request = Request({"type": "http", "client": ("127.0.0.1", 1000), "headers": [(b"origin", b"http://127.0.0.1:3001")]})
    module.local_control(request)


def test_bridge_observation_schema_drops_no_secrets_and_rejects_unknown_fields():
    module = import_module("app.routers.browser_bridge")
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        module.BrowserResult.model_validate({"ok": True, "cookies": "secret"})
    with pytest.raises(ValidationError):
        module.BrowserResult.model_validate({"ok": True, "data": {"kind": "note", "version": "v", "note": {"title": "x", "text": "body", "source_url": "https://evil.test", "captured_at": "2026-10-06T10:00:00Z"}}})


def test_expired_command_cannot_replace_cancelled_status():
    now = [100.0]
    bridge = store(lambda: now[0])
    session = connected(bridge)
    job = bridge.start_job("account-a", "ai_content", ["AI"], "hotspot")
    command = bridge.enqueue(job["id"], "observe", {})
    bridge.lease_command(session["token"], "chrome-extension://" + "a" * 32)
    bridge.stop(job["id"], "account-a")
    now[0] += 46
    result = bridge.command_result(bridge.jobs[job["id"]], command["id"])
    assert result["status"] == "cancelled"
