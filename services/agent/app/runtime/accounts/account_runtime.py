from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock, RLock
from typing import Any, Dict, Iterator


_REGISTRY_LOCK = Lock()
_ACCOUNT_LOCKS: dict[str, RLock] = {}
_ACCOUNT_RUNTIME: dict[str, Dict[str, Any]] = {}
_IDLE_TTL = timedelta(minutes=20)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_account_lock(account_id: str) -> RLock:
    normalized = str(account_id or "").strip()
    if not normalized:
        return RLock()
    with _REGISTRY_LOCK:
        lock = _ACCOUNT_LOCKS.get(normalized)
        if lock is None:
            lock = RLock()
            _ACCOUNT_LOCKS[normalized] = lock
        return lock


def _ensure_runtime(account_id: str, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
    normalized = str(account_id or "").strip()
    with _REGISTRY_LOCK:
        runtime = _ACCOUNT_RUNTIME.get(normalized)
        if runtime is None:
            runtime = {
                "account_id": normalized,
                "status": "idle",
                "busy": False,
                "last_error": "",
                "current_action": "",
                "current_task_id": "",
                "last_used_at": None,
                "lock_key": f"account:{normalized}",
                "browser_context_key": f"browser:{normalized}",
                "channel": "",
                "login_mode": "",
                "storage_state_path": None,
                "user_data_dir": None,
                "cookies_json": None,
                "proxy_config": {},
            }
            _ACCOUNT_RUNTIME[normalized] = runtime
        if isinstance(metadata, dict):
            for key in [
                "channel",
                "login_mode",
                "storage_state_path",
                "user_data_dir",
                "cookies_json",
                "proxy_config",
            ]:
                if key in metadata:
                    runtime[key] = metadata.get(key)
        return dict(runtime)


def upsert_account_runtime(account_id: str, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _ensure_runtime(account_id, metadata)


def mark_account_runtime(account_id: str, **fields: Any) -> Dict[str, Any]:
    normalized = str(account_id or "").strip()
    with _REGISTRY_LOCK:
        runtime = _ACCOUNT_RUNTIME.get(normalized) or _ensure_runtime(normalized)
        runtime.update(fields)
        runtime["last_used_at"] = fields.get("last_used_at", _now_iso())
        _ACCOUNT_RUNTIME[normalized] = runtime
        return dict(runtime)


def snapshot_account_runtime(account_id: str) -> Dict[str, Any]:
    normalized = str(account_id or "").strip()
    with _REGISTRY_LOCK:
        runtime = _ACCOUNT_RUNTIME.get(normalized)
        if runtime is None:
            return {
                "account_id": normalized,
                "status": "idle",
                "busy": False,
                "current_action": "",
                "current_task_id": "",
                "last_error": "",
                "last_used_at": None,
                "lock_key": f"account:{normalized}",
                "browser_context_key": f"browser:{normalized}",
            }
        return dict(runtime)


def cleanup_idle_account_runtime() -> None:
    cutoff = datetime.now(timezone.utc) - _IDLE_TTL
    with _REGISTRY_LOCK:
        to_delete: list[str] = []
        for account_id, runtime in _ACCOUNT_RUNTIME.items():
            raw_last_used = str(runtime.get("last_used_at") or "").strip()
            if runtime.get("busy"):
                continue
            if not raw_last_used:
                continue
            try:
                last_used = datetime.fromisoformat(raw_last_used.replace("Z", "+00:00"))
            except ValueError:
                continue
            if last_used < cutoff:
                to_delete.append(account_id)
        for account_id in to_delete:
            _ACCOUNT_RUNTIME.pop(account_id, None)


@contextmanager
def account_execution_scope(
    account_id: str,
    *,
    action: str,
    task_id: str = "",
    metadata: Dict[str, Any] | None = None,
) -> Iterator[Dict[str, Any]]:
    normalized = str(account_id or "").strip()
    if not normalized:
        yield {
            "account_id": "",
            "status": "unscoped",
            "busy": False,
            "current_action": action,
            "current_task_id": task_id,
        }
        return

    lock = _get_account_lock(normalized)
    with lock:
        _ensure_runtime(normalized, metadata)
        mark_account_runtime(
            normalized,
            status="running",
            busy=True,
            current_action=action,
            current_task_id=task_id,
            last_error="",
        )
        try:
            yield snapshot_account_runtime(normalized)
            mark_account_runtime(
                normalized,
                status="idle",
                busy=False,
                current_action="",
                current_task_id="",
            )
        except Exception as exc:  # noqa: BLE001
            mark_account_runtime(
                normalized,
                status="error",
                busy=False,
                current_action=action,
                current_task_id=task_id,
                last_error=str(exc),
            )
            raise
