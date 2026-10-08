from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any, Dict

import httpx

from app.config import get_settings
from app.db import get_supabase
from app.orchestration.daily_ops import run_daily_ops_with_retry

_lock = Lock()
_stop_event = Event()
_thread: Thread | None = None
_state: Dict[str, Any] = {
    "enabled": False,
    "running": False,
    "started_at": None,
    "last_tick_at": None,
    "last_triggered_local_date": None,
    "last_run_at": None,
    "last_run_status": None,
    "last_run_key": None,
    "last_error": None,
    "next_run_at": None,
    "domain_slug": "japan_immigration",
    "last_feedback_run_at": None,
    "last_feedback_status": None,
    "last_feedback_error": None,
    "next_feedback_run_at": None,
    "feedback_enabled": False,
    "feedback_domain_slug": "japan_immigration",
    "feedback_provider": "api_observation",
}


def reconcile_publish_feedback(client: Any, *, domain_slug: str, limit: int) -> Dict[str, Any]:
    """The API collects CLI observations before delegating persisted review to the agent."""
    settings = get_settings()
    headers = {"x-user-id": "scheduler:feedback", "x-user-role": "operator"}
    if settings.internal_service_token:
        headers["x-internal-token"] = settings.internal_service_token
    with httpx.Client(timeout=30, trust_env=False) as http_client:
        response = http_client.post(
            f"{settings.browser_bridge_api_url.rstrip('/')}/api/ops/publish-feedback/reconcile",
            headers=headers, json={"domain_slug": domain_slug, "limit": max(1, min(limit, 100))},
        )
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict):
        raise ValueError("API observation path returned an invalid result")
    return result


def _parse_utc_offset(offset_text: str) -> timezone:
    text = (offset_text or "").strip()
    if len(text) != 6 or text[0] not in {"+", "-"} or text[3] != ":":
        return timezone.utc
    try:
        hours = int(text[1:3])
        minutes = int(text[4:6])
    except ValueError:
        return timezone.utc
    if hours > 14 or minutes > 59:
        return timezone.utc
    delta = timedelta(hours=hours, minutes=minutes)
    if text[0] == "-":
        delta = -delta
    return timezone(delta)


def _build_next_run_at(now_utc: datetime, tz: timezone, hour: int, minute: int) -> datetime:
    local_now = now_utc.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _update_state(fields: Dict[str, Any]) -> None:
    with _lock:
        _state.update(fields)


def _scheduler_loop() -> None:
    settings = get_settings()
    tz = _parse_utc_offset(settings.daily_scheduler_utc_offset)
    hour = min(23, max(0, settings.daily_scheduler_hour))
    minute = min(59, max(0, settings.daily_scheduler_minute))
    domain_slug = settings.daily_scheduler_domain_slug
    feedback_domain_slug = settings.publish_feedback_domain_slug or domain_slug
    feedback_poll_minutes = max(5, settings.publish_feedback_poll_minutes)

    _update_state(
        {
            "enabled": settings.daily_scheduler_enabled,
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "domain_slug": domain_slug,
            "last_error": None,
            "feedback_enabled": settings.publish_feedback_scheduler_enabled,
            "feedback_domain_slug": feedback_domain_slug,
        }
    )

    while not _stop_event.is_set():
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc.astimezone(tz)
        local_date = local_now.date().isoformat()
        should_run_today = (local_now.hour > hour) or (local_now.hour == hour and local_now.minute >= minute)
        next_run_at = _build_next_run_at(now_utc, tz, hour, minute).isoformat() if settings.daily_scheduler_enabled else None

        with _lock:
            already_triggered = _state.get("last_triggered_local_date") == local_date
            last_feedback_run_at = _parse_feedback_iso(_state.get("last_feedback_run_at"))
        _update_state({"last_tick_at": now_utc.isoformat(), "next_run_at": next_run_at})
        next_feedback_run_at = (
            (last_feedback_run_at + timedelta(minutes=feedback_poll_minutes)).isoformat()
            if last_feedback_run_at else now_utc.isoformat()
        ) if settings.publish_feedback_scheduler_enabled else None
        _update_state({"next_feedback_run_at": next_feedback_run_at})

        if settings.daily_scheduler_enabled and should_run_today and not already_triggered:
            run_key = f"scheduled:{domain_slug}:{local_date}"
            try:
                result = run_daily_ops_with_retry(
                    get_supabase(),
                    domain_slug=domain_slug,
                    run_key=run_key,
                    triggered_by="scheduler:auto",
                )
                run_status = str(result.get("status") or "failed")
                run_error = None if run_status in {"ok", "skipped"} else str(result.get("last_error") or "")
                _update_state(
                    {
                        "last_triggered_local_date": local_date,
                        "last_run_at": datetime.now(timezone.utc).isoformat(),
                        "last_run_status": run_status,
                        "last_run_key": run_key,
                        "last_error": run_error,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                _update_state(
                    {
                        "last_triggered_local_date": local_date,
                        "last_run_at": datetime.now(timezone.utc).isoformat(),
                        "last_run_status": "failed",
                        "last_run_key": run_key,
                        "last_error": str(exc),
                    }
                )

        feedback_due = settings.publish_feedback_scheduler_enabled and (
            last_feedback_run_at is None
            or (now_utc - last_feedback_run_at) >= timedelta(minutes=feedback_poll_minutes)
        )
        if feedback_due:
            try:
                feedback_result = reconcile_publish_feedback(
                    None,
                    domain_slug=feedback_domain_slug,
                    limit=20,
                )
                feedback_status = str(feedback_result.get("status") or "failed")
                feedback_error = None if feedback_status == "ok" else str(
                    feedback_result.get("reason") or feedback_result.get("errors") or feedback_status
                )
                finished_at = datetime.now(timezone.utc)
                _update_state(
                    {
                        "last_feedback_run_at": finished_at.isoformat(),
                        "last_feedback_status": feedback_status,
                        "last_feedback_error": feedback_error,
                        "next_feedback_run_at": (finished_at + timedelta(minutes=feedback_poll_minutes)).isoformat(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                finished_at = datetime.now(timezone.utc)
                _update_state(
                    {
                        "last_feedback_run_at": finished_at.isoformat(),
                        "last_feedback_status": "failed",
                        "last_feedback_error": str(exc),
                        "next_feedback_run_at": (finished_at + timedelta(minutes=feedback_poll_minutes)).isoformat(),
                    }
                )

        _stop_event.wait(timeout=20)

    _update_state({"running": False})


def start_daily_scheduler() -> None:
    settings = get_settings()
    _update_state(
        {
            "enabled": settings.daily_scheduler_enabled,
            "domain_slug": settings.daily_scheduler_domain_slug,
            "feedback_enabled": settings.publish_feedback_scheduler_enabled,
            "feedback_domain_slug": settings.publish_feedback_domain_slug or settings.daily_scheduler_domain_slug,
        }
    )
    if not (settings.daily_scheduler_enabled or settings.publish_feedback_scheduler_enabled):
        return

    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = Thread(target=_scheduler_loop, name="daily-ops-scheduler", daemon=True)
        _thread.start()


def stop_daily_scheduler() -> None:
    global _thread
    with _lock:
        _stop_event.set()
        worker = _thread
    if worker and worker.is_alive():
        worker.join(timeout=3)
    with _lock:
        if _thread is worker and not (worker and worker.is_alive()):
            _thread = None
        _state["running"] = bool(_thread and _thread.is_alive())
        _state["next_run_at"] = None
        _state["next_feedback_run_at"] = None


def get_scheduler_runtime_status() -> Dict[str, Any]:
    settings = get_settings()
    with _lock:
        snapshot = dict(_state)
        running = bool(_thread and _thread.is_alive())
    snapshot["running"] = running
    snapshot["configured"] = {
        "enabled": settings.daily_scheduler_enabled,
        "domain_slug": settings.daily_scheduler_domain_slug,
        "time": f"{min(23, max(0, settings.daily_scheduler_hour)):02d}:{min(59, max(0, settings.daily_scheduler_minute)):02d}",
        "utc_offset": settings.daily_scheduler_utc_offset,
        "publish_feedback_enabled": settings.publish_feedback_scheduler_enabled,
        "publish_feedback_domain_slug": settings.publish_feedback_domain_slug,
        "publish_feedback_poll_minutes": max(5, settings.publish_feedback_poll_minutes),
    }
    return snapshot


def _parse_feedback_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None
