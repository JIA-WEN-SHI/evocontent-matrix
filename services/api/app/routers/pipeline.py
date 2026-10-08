import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from supabase import Client

from app.config import Settings, get_settings
from app.db import get_supabase
from app.models import (
    Actor,
    PipelineApproveRequest,
    PipelineTaskCreateRequest,
    PipelineTaskEditRequest,
    PipelineTaskListResponse,
    PipelineRejectRequest,
    PipelineTaskRecord,
)
from app.security import require_roles
from app.services.pipeline_service import (
    approve_pipeline_task,
    create_pipeline_task,
    get_pipeline_task,
    list_pipeline_tasks,
    reject_pipeline_task,
    update_pipeline_task,
)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


def _build_pipeline_feedback(row: dict) -> dict:
    payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
    publish_jsonb = row.get("publish_jsonb") if isinstance(row.get("publish_jsonb"), dict) else {}
    metrics_jsonb = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
    feedback_analysis = (
        publish_jsonb.get("feedback_analysis")
        if isinstance(publish_jsonb.get("feedback_analysis"), dict)
        else {}
    )
    identity = publish_jsonb.get("identity") if isinstance(publish_jsonb.get("identity"), dict) else {}
    return {
        "task_id": row.get("id"),
        "account_id": row.get("account_id"),
        "title": str(payload.get("title") or ""),
        "status": str(row.get("status") or ""),
        "stage": str(row.get("stage") or ""),
        "feedback_state": str(publish_jsonb.get("feedback_state") or ""),
        "schedule_hours": publish_jsonb.get("feedback_schedule_hours")
        if isinstance(publish_jsonb.get("feedback_schedule_hours"), list)
        else [],
        "completed_hours": publish_jsonb.get("feedback_completed_hours")
        if isinstance(publish_jsonb.get("feedback_completed_hours"), list)
        else [],
        "next_feedback_at": publish_jsonb.get("next_feedback_at"),
        "last_feedback_at": publish_jsonb.get("last_feedback_at"),
        "identity": identity,
        "feedback_analysis": feedback_analysis,
        "metrics_jsonb": metrics_jsonb,
        "publish_jsonb": publish_jsonb,
        "published_at": row.get("published_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@router.get("/tasks", response_model=PipelineTaskListResponse)
def get_pipeline_tasks(
    status_value: str | None = Query(default=None, alias="status"),
    channel: str | None = Query(default=None),
    domain_slug: str | None = Query(default=None),
    account_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> PipelineTaskListResponse:
    try:
        rows = list_pipeline_tasks(
            client=client,
            status_value=status_value,
            channel=channel,
            domain_slug=domain_slug,
            account_id=account_id,
            limit=limit,
        )
    except Exception:  # noqa: BLE001
        rows = []
    return PipelineTaskListResponse(items=[PipelineTaskRecord(**item) for item in rows])


@router.get("/tasks/{pipeline_task_id}", response_model=PipelineTaskRecord)
def get_pipeline_task_by_id(
    pipeline_task_id: str,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> PipelineTaskRecord:
    row = get_pipeline_task(client=client, pipeline_task_id=pipeline_task_id)
    return PipelineTaskRecord(**row)


@router.get("/tasks/{pipeline_task_id}/feedback")
def get_pipeline_feedback_by_id(
    pipeline_task_id: str,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    rows = (
        client.table("pipeline_tasks")
        .select(
            "id,account_id,status,stage,payload_jsonb,publish_jsonb,metrics_jsonb,published_at,created_at,updated_at"
        )
        .eq("id", pipeline_task_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pipeline task not found")
    return _build_pipeline_feedback(rows[0])


@router.get("/tasks/{pipeline_task_id}/audits")
def get_pipeline_task_audits_by_id(
    pipeline_task_id: str,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    pipeline_items = (
        client.table("audit_logs")
        .select("*")
        .eq("target_type", "pipeline_task")
        .eq("target_id", pipeline_task_id)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    ).data or []
    return {"items": pipeline_items}


@router.post("/tasks", response_model=PipelineTaskRecord)
def post_pipeline_task(
    request: PipelineTaskCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> PipelineTaskRecord:
    row = create_pipeline_task(client=client, request=request, actor=actor)
    return PipelineTaskRecord(**row)


@router.patch("/tasks/{pipeline_task_id}", response_model=PipelineTaskRecord)
def patch_pipeline_task(
    pipeline_task_id: str,
    request: PipelineTaskEditRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> PipelineTaskRecord:
    return PipelineTaskRecord(**update_pipeline_task(
        client=client, pipeline_task_id=pipeline_task_id, actor=actor,
        title=request.title, body=request.body,
    ))


@router.post("/tasks/{pipeline_task_id}/approve", response_model=PipelineTaskRecord)
def post_approve_pipeline_task(
    pipeline_task_id: str,
    request: PipelineApproveRequest | None = None,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "reviewer")),
) -> PipelineTaskRecord:
    row = approve_pipeline_task(
        client=client,
        pipeline_task_id=pipeline_task_id,
        actor=actor,
        edited_title=request.title if request else None,
        edited_body=request.body if request else None,
    )
    return PipelineTaskRecord(**row)


@router.post("/tasks/{pipeline_task_id}/reject", response_model=PipelineTaskRecord)
def post_reject_pipeline_task(
    pipeline_task_id: str,
    request: PipelineRejectRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "reviewer")),
) -> PipelineTaskRecord:
    row = reject_pipeline_task(
        client=client,
        pipeline_task_id=pipeline_task_id,
        reason=request.reason,
        actor=actor,
    )
    return PipelineTaskRecord(**row)


@router.post("/tasks/{pipeline_task_id}/run")
async def run_pipeline_task(
    pipeline_task_id: str,
    settings: Settings = Depends(get_settings),
    database: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    task = get_pipeline_task(database, pipeline_task_id)
    if task.get("status") not in {"queued", "intel_ready", "drafting", "review_rejected", "pending_review"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI 代发布已取消，草稿保留，请自行发布。",
        )
    url = f"{settings.agent_service_url}/run-pipeline/{pipeline_task_id}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return response.json()
