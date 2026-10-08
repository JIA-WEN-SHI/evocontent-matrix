from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from supabase import Client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_ready(client: Client, table: str) -> bool:
    try:
        client.table(table).select("id").limit(1).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def _run_key(source: str, action_type: str) -> str:
    return f"{source}:{action_type}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}:{uuid4().hex[:8]}"


def _find_existing_run(
    client: Client,
    *,
    domain_id: str,
    source: str,
    idempotency_key: str,
) -> Dict[str, Any] | None:
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_key:
        return None
    try:
        rows = (
            client.table("action_runs")
            .select("id,run_key,trace_id,status,created_at,finished_at")
            .eq("domain_id", domain_id)
            .eq("source", str(source or "").strip())
            .eq("idempotency_key", normalized_key)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        return None
    return rows[0] if rows else None


def record_action_run(
    client: Client,
    *,
    domain_id: str,
    domain_slug: str,
    account_id: str = "",
    actor: str,
    source: str,
    source_ref: str = "",
    action_type: str,
    input_jsonb: Dict[str, Any] | None = None,
    result_jsonb: Dict[str, Any] | None = None,
    policy_snapshot: Dict[str, Any] | None = None,
    status: str = "success",
    retryable: bool = False,
    error_code: str = "",
    error_message: str = "",
    idempotency_key: str = "",
    trace_id: str = "",
) -> Dict[str, Any]:
    if not _table_ready(client, "action_runs"):
        return {
            "status": "skipped",
            "reason": "action_runs_table_missing",
            "trace_id": trace_id or "",
            "run_key": "",
        }

    normalized_idempotency_key = str(idempotency_key or "").strip()
    if normalized_idempotency_key:
        existing = _find_existing_run(
            client,
            domain_id=domain_id,
            source=source,
            idempotency_key=normalized_idempotency_key,
        )
        if existing:
            return {
                "status": "exists",
                "reason": "idempotent_replay",
                "trace_id": str(existing.get("trace_id") or ""),
                "run_key": str(existing.get("run_key") or ""),
                "existing_status": str(existing.get("status") or ""),
            }

    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"queued", "running", "success", "failed", "retry_later", "blocked", "canceled"}:
        normalized_status = "failed"
    normalized_trace_id = str(trace_id or "").strip() or f"tr_{uuid4().hex[:12]}"
    normalized_run_key = _run_key(source=source, action_type=action_type)
    payload = {
        "run_key": normalized_run_key,
        "domain_id": domain_id,
        "domain_slug": domain_slug,
        "account_id": str(account_id or "").strip() or None,
        "actor": str(actor or "").strip(),
        "source": str(source or "").strip(),
        "source_ref": str(source_ref or "").strip(),
        "action_type": str(action_type or "").strip(),
        "idempotency_key": normalized_idempotency_key,
        "trace_id": normalized_trace_id,
        "policy_snapshot": policy_snapshot if isinstance(policy_snapshot, dict) else {},
        "input_jsonb": input_jsonb if isinstance(input_jsonb, dict) else {},
        "result_jsonb": result_jsonb if isinstance(result_jsonb, dict) else {},
        "status": normalized_status,
        "retryable": bool(retryable),
        "error_code": str(error_code or "").strip(),
        "error_message": str(error_message or "").strip()[:2000],
        "started_at": _now_iso(),
        "finished_at": _now_iso(),
    }
    try:
        client.table("action_runs").insert(payload).execute()
    except Exception:  # noqa: BLE001
        return {
            "status": "error",
            "reason": "action_run_insert_failed",
            "trace_id": normalized_trace_id,
            "run_key": normalized_run_key,
        }
    return {
        "status": "ok",
        "trace_id": normalized_trace_id,
        "run_key": normalized_run_key,
    }
