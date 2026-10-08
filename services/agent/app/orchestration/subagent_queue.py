from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Dict
from uuid import uuid4


_QUEUE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subagent-worker")
_QUEUE_LOCK = Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SubAgentJob:
    run_id: str
    agent_name: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str = ""


def _set_job(run_id: str, patch: Dict[str, Any]) -> None:
    with _QUEUE_LOCK:
        current = _JOBS.get(run_id, {})
        _JOBS[run_id] = {**current, **patch}


def submit_subagent_job(
    *,
    agent_name: str,
    payload: Dict[str, Any] | None,
    handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    timeout_seconds: float = 240.0,
) -> Dict[str, Any]:
    run_id = f"sr_{uuid4().hex[:16]}"
    created_at = _now_iso()
    _set_job(
        run_id,
        {
            "run_id": run_id,
            "agent_name": agent_name,
            "status": "queued",
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "error": "",
        },
    )

    def _wrapped() -> Dict[str, Any]:
        _set_job(run_id, {"status": "running", "started_at": _now_iso()})
        try:
            result = handler(payload if isinstance(payload, dict) else {})
            _set_job(run_id, {"status": "completed", "finished_at": _now_iso()})
            return result
        except Exception as exc:  # noqa: BLE001
            _set_job(run_id, {"status": "failed", "finished_at": _now_iso(), "error": str(exc)})
            raise

    future = _QUEUE_EXECUTOR.submit(_wrapped)
    try:
        result = future.result(timeout=max(3.0, timeout_seconds))
        return {
            "run_id": run_id,
            "status": "completed",
            "result": result if isinstance(result, dict) else {"status": "failed", "message": "invalid_subagent_result"},
            "queue": _JOBS.get(run_id, {}),
        }
    except TimeoutError:
        _set_job(run_id, {"status": "timeout", "finished_at": _now_iso(), "error": "timeout"})
        return {
            "run_id": run_id,
            "status": "timeout",
            "result": {"status": "retry_later", "code": "subagent_timeout", "retryable": True},
            "queue": _JOBS.get(run_id, {}),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "run_id": run_id,
            "status": "failed",
            "result": {"status": "failed", "code": "subagent_failed", "retryable": False, "message": str(exc)},
            "queue": _JOBS.get(run_id, {}),
        }


def get_subagent_job(run_id: str) -> Dict[str, Any] | None:
    with _QUEUE_LOCK:
        row = _JOBS.get(run_id)
        return dict(row) if isinstance(row, dict) else None
