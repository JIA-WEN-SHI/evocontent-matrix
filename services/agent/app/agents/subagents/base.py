from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


def load_source_rows(client: Any, *, table: str, domain_id: str, account_id: str, limit: int = 80) -> List[Dict[str, Any]]:
    if not domain_id:
        return []
    timestamp = "captured_at" if table == "intelligence_items" else "updated_at"
    query = client.table(table).select("*").eq("domain_id", domain_id)
    if table != "intelligence_items":
        query = query.is_("deleted_at", "null")
    if account_id:
        query = query.eq("account_id", account_id)
    else:
        query = query.is_("account_id", "null")
    return query.order(timestamp, desc=True).limit(limit).execute().data or []


def source_evidence(rows: List[Dict[str, Any]], source_type: str) -> List[Dict[str, Any]]:
    return [
        {
            "source_type": source_type,
            "source_ref": str(row.get("id") or row.get("source_url") or row.get("url")),
            "source_url": str(row.get("source_url") or row.get("url") or ""),
            "timestamp": str(row.get("captured_at") or row.get("updated_at") or ""),
        }
        for row in rows
        if row.get("id") or row.get("source_url") or row.get("url")
    ]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_envelope(
    *,
    agent_name: str,
    status: str,
    code: str = "",
    retryable: bool = False,
    trace_id: str = "",
    message: str = "",
    result: Dict[str, Any] | None = None,
    evidence_refs: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"success", "failed", "retry_later", "blocked"}:
        normalized_status = "failed"
    return {
        "agent_name": str(agent_name or "").strip(),
        "status": normalized_status,
        "code": str(code or "").strip(),
        "retryable": bool(retryable),
        "trace_id": str(trace_id or "").strip(),
        "message": str(message or "").strip(),
        "result": result if isinstance(result, dict) else {},
        "evidence_refs": evidence_refs if isinstance(evidence_refs, list) else [],
        "finished_at": _now_iso(),
    }
