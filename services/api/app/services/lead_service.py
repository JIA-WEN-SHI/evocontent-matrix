from datetime import datetime, timezone
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet
from fastapi import HTTPException, status
from supabase import Client

from app.config import get_settings
from app.models import AuditLogEntry, LeadEventPayload
from app.services.audit import write_audit_log


def _build_fernet() -> Fernet:
    settings = get_settings()
    key = settings.pii_encryption_key
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Invalid PII_ENCRYPTION_KEY; must be a valid Fernet key") from exc


def _normalize_contact_fields(payload: Dict[str, Any]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        normalized[str(key)] = text
    return normalized


def _encrypt_contact_fields(payload: Dict[str, Any]) -> Dict[str, str]:
    fernet = _build_fernet()
    normalized = _normalize_contact_fields(payload)
    return {k: fernet.encrypt(v.encode("utf-8")).decode("utf-8") for k, v in normalized.items()}


def _resolve_pipeline_task_id(client: Client, utm_code: str, form_id: str) -> Optional[str]:
    rows = (
        client.table("pipeline_tasks")
        .select("id,payload_jsonb,publish_jsonb,updated_at")
        .in_("status", ["published", "done"])
        .order("updated_at", desc=True)
        .limit(300)
        .execute()
    ).data or []
    target_utm = str(utm_code or "").strip()
    target_form = str(form_id or "").strip()
    if not target_utm or not target_form:
        return None
    for row in rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        publish = row.get("publish_jsonb") if isinstance(row.get("publish_jsonb"), dict) else {}
        candidate_utm = str(payload.get("utm_code") or publish.get("utm_code") or "").strip()
        candidate_form = str(payload.get("form_id") or publish.get("form_id") or "").strip()
        if candidate_utm == target_utm and candidate_form == target_form:
            task_id = str(row.get("id") or "").strip()
            if task_id:
                return task_id
    return None


def ingest_lead_event(client: Client, payload: LeadEventPayload) -> Dict[str, Any]:
    encrypted_fields = _encrypt_contact_fields(payload.contact_fields)
    pipeline_task_id = _resolve_pipeline_task_id(client, payload.utm_code, payload.form_id)
    event_time = payload.event_time or datetime.now(timezone.utc)
    extra_meta = dict(payload.meta_jsonb or {})
    if pipeline_task_id:
        extra_meta["pipeline_task_id"] = pipeline_task_id

    insert_body = {
        # Legacy task FK is retained for schema compatibility; pipeline mode stores relation in meta_jsonb.
        "task_id": None,
        "channel": payload.channel,
        "utm_code": payload.utm_code,
        "form_id": payload.form_id,
        "event_time": event_time.isoformat(),
        "contact_fields": encrypted_fields,
        "meta_jsonb": extra_meta,
    }
    inserted = client.table("lead_events").insert(insert_body).execute()
    if not inserted.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Lead insert failed")

    if pipeline_task_id:
        count_res = (
            client.table("lead_events")
            .select("id", count="exact")
            .eq("meta_jsonb->>pipeline_task_id", pipeline_task_id)
            .execute()
        )
        total = count_res.count or 0
        latest = (
            client.table("pipeline_tasks")
            .select("metrics_jsonb")
            .eq("id", pipeline_task_id)
            .limit(1)
            .execute()
        ).data or []
        current_metrics = latest[0].get("metrics_jsonb") if latest and isinstance(latest[0].get("metrics_jsonb"), dict) else {}
        next_metrics = dict(current_metrics or {})
        next_metrics["leads_generated"] = int(total)
        client.table("pipeline_tasks").update(
            {"metrics_jsonb": next_metrics, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", pipeline_task_id).execute()

        write_audit_log(
            client,
            AuditLogEntry(
                actor="system:webhook",
                action="lead.ingested",
                target_type="pipeline_task",
                target_id=pipeline_task_id,
                diff_jsonb={"increment_to": total},
            ),
        )

    return inserted.data[0]
