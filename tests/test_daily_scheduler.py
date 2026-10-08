from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "services" / "agent"))

from app.services.daily_scheduler import _build_next_run_at, _parse_utc_offset
from app.orchestration import daily_scheduler as scheduler


def test_parse_utc_offset_valid_and_invalid():
    tokyo = _parse_utc_offset("+09:00")
    assert tokyo.utcoffset(None).total_seconds() == 9 * 3600

    invalid = _parse_utc_offset("bad-offset")
    assert invalid.utcoffset(None).total_seconds() == 0


def test_build_next_run_at_rolls_to_next_day_when_past_time():
    tz = _parse_utc_offset("+09:00")
    now_utc = datetime(2026, 2, 20, 10, 30, tzinfo=timezone.utc)  # 19:30 JST
    next_run = _build_next_run_at(now_utc, tz, hour=9, minute=0)
    assert next_run.isoformat() == "2026-02-21T00:00:00+00:00"


@pytest.fixture
def runtime(monkeypatch):
    settings = SimpleNamespace(
        daily_scheduler_enabled=False, publish_feedback_scheduler_enabled=False,
        daily_scheduler_domain_slug="daily-domain", publish_feedback_domain_slug="feedback-domain",
        daily_scheduler_hour=0, daily_scheduler_minute=0, daily_scheduler_utc_offset="+00:00",
        publish_feedback_poll_minutes=30, browser_bridge_api_url="http://api.test",
        internal_service_token="test-token",
    )
    class StopEvent:
        stopped = False
        def is_set(self):
            return self.stopped
        def clear(self):
            self.stopped = False
        def set(self):
            self.stopped = True
        def wait(self, timeout):
            self.stopped = True
    class Worker:
        def __init__(self, target, **kwargs):
            self.target, self.alive = target, False
            self.starts, self.joins = 0, 0
        def start(self):
            self.alive = True
            self.starts += 1
        def is_alive(self):
            return self.alive
        def join(self, timeout):
            self.alive = False
            self.joins += 1
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler, "_thread", None)
    monkeypatch.setattr(scheduler, "_stop_event", StopEvent())
    monkeypatch.setattr(scheduler, "_state", {**scheduler._state, "last_feedback_run_at": None,
                                             "last_triggered_local_date": None})
    monkeypatch.setattr(scheduler, "Thread", Worker)
    monkeypatch.setattr(scheduler, "get_supabase", lambda: object())
    return settings


def test_both_disabled_create_no_worker(runtime):
    scheduler.start_daily_scheduler()
    assert scheduler._thread is None
    assert scheduler.get_scheduler_runtime_status()["running"] is False


def test_feedback_can_start_and_stop_independently(runtime):
    runtime.publish_feedback_scheduler_enabled = True
    scheduler.start_daily_scheduler()
    worker = scheduler._thread
    assert worker is not None
    assert worker.is_alive()
    scheduler.start_daily_scheduler()
    assert scheduler._thread is worker
    assert worker.starts == 1
    scheduler.stop_daily_scheduler()
    assert worker.joins == 1
    assert scheduler.get_scheduler_runtime_status()["running"] is False
    scheduler.start_daily_scheduler()
    assert scheduler._thread is not worker
    assert scheduler._thread.is_alive()
    scheduler.stop_daily_scheduler()


def test_feedback_only_tick_never_runs_daily(runtime, monkeypatch):
    runtime.publish_feedback_scheduler_enabled = True
    calls = []
    def daily(*args, **kwargs):
        calls.append("daily")
        return {"status": "ok"}
    monkeypatch.setattr(scheduler, "run_daily_ops_with_retry", daily)
    def feedback(*args, **kwargs):
        calls.append("feedback")
        return {"status": "ok"}
    monkeypatch.setattr(scheduler, "reconcile_publish_feedback", feedback)
    scheduler._scheduler_loop()
    assert calls == ["feedback"]
    assert scheduler._state["enabled"] is False
    assert scheduler._state["next_run_at"] is None
    assert scheduler._state["next_feedback_run_at"] is not None


def test_disabled_feedback_has_no_due_time(runtime, monkeypatch):
    runtime.daily_scheduler_enabled = True
    monkeypatch.setattr(scheduler, "run_daily_ops_with_retry", lambda *args, **kwargs: {"status": "ok"})
    scheduler._scheduler_loop()
    assert scheduler._state["next_feedback_run_at"] is None


def test_shutdown_does_not_allow_second_worker_when_join_times_out(runtime):
    runtime.publish_feedback_scheduler_enabled = True
    scheduler.start_daily_scheduler()
    worker = scheduler._thread
    worker.join = lambda timeout: None
    scheduler.stop_daily_scheduler()
    scheduler.start_daily_scheduler()
    assert scheduler._thread is worker
    assert worker.starts == 1
    assert scheduler._stop_event.is_set()


def test_scheduled_feedback_delegates_to_api_owned_observation_path(runtime, monkeypatch):
    requests = []
    class HttpClient:
        def __init__(self, **kwargs):
            assert 0 < kwargs["timeout"] <= 60
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(200, json={"status": "ok", "reconciled": 1},
                                  request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "Client", HttpClient)
    result = scheduler.reconcile_publish_feedback(None, domain_slug="feedback-domain", limit=20)
    assert result == {"status": "ok", "reconciled": 1}
    assert requests[0][0] == "http://api.test/api/ops/publish-feedback/reconcile"
    assert requests[0][1]["json"] == {"domain_slug": "feedback-domain", "limit": 20}
    assert requests[0][1]["headers"]["x-user-role"] == "operator"


def test_feedback_provider_failure_is_visible_and_does_not_run_daily(runtime, monkeypatch):
    runtime.publish_feedback_scheduler_enabled = True
    def unavailable(*args, **kwargs):
        raise RuntimeError("API observation path unavailable")
    monkeypatch.setattr(scheduler, "reconcile_publish_feedback", unavailable)
    scheduler._scheduler_loop()
    state = scheduler.get_scheduler_runtime_status()
    assert state["last_feedback_status"] == "failed"
    assert "API observation path unavailable" in state["last_feedback_error"]
    assert state["last_run_at"] is None
    assert state["feedback_domain_slug"] == "feedback-domain"
    assert state["feedback_provider"] == "api_observation"
