from fastapi import APIRouter, Depends, Query
from supabase import Client

from app.db import get_supabase
from app.models import Actor
from app.security import require_roles

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


@router.get("")
def get_audit_logs(
    target_type: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    query = client.table("audit_logs").select("*").order("created_at", desc=True).limit(limit)
    if target_type:
        query = query.eq("target_type", target_type)
    if target_id:
        query = query.eq("target_id", target_id)
    try:
        result = query.execute()
    except Exception:  # noqa: BLE001
        return {"items": []}
    return {"items": result.data or []}
