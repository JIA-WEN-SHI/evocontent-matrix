from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from supabase import Client

from app.db import get_supabase
from app.models import (
    Actor,
    KbAiJobListResponse,
    KbAssetCreateRequest,
    KbAssetListResponse,
    KbAssetPatchRequest,
    KbAssetRecord,
    KbCaseCreateRequest,
    KbCaseListResponse,
    KbCasePatchRequest,
    KbCaseRecord,
    KbEntityTagCreateRequest,
    KbEntityTagListResponse,
    KbEntityTagPatchRequest,
    KbEntityTagRecord,
    KbIngestionLogListResponse,
    KbIoRuleCreateRequest,
    KbIoRuleListResponse,
    KbIoRulePatchRequest,
    KbIoRuleRecord,
    KbOctopusImportRequest,
    KbOctopusImportResult,
    KbPlaybookCreateRequest,
    KbPlaybookListResponse,
    KbPlaybookPatchRequest,
    KbPlaybookRecord,
    KbReviewCreateRequest,
    KbReviewListResponse,
    KbReviewPatchRequest,
    KbReviewRecord,
    KbRulebookCreateRequest,
    KbRulebookListResponse,
    KbRulebookPatchRequest,
    KbRulebookRecord,
    KbTagCreateRequest,
    KbTagListResponse,
    KbTagPatchRequest,
    KbTagRecord,
    KbTopicCreateRequest,
    KbTopicListResponse,
    KbTopicPatchRequest,
    KbTopicRecommendRequest,
    KbTopicRecommendResponse,
    KbTopicRecord,
    KbUserNeedCreateRequest,
    KbUserNeedListResponse,
    KbUserNeedPatchRequest,
    KbUserNeedRecord,
    KbRpaCleanResultListResponse,
    KbRpaRawPayloadListResponse,
    KbRpaReplayResponse,
    KbRpaTaskRunListResponse,
    KbRpaTaskTemplateListResponse,
)
from app.security import require_roles
from app.services.kb_service import (
    import_octopus_payload,
    normalize_topic_status,
    recommend_topics,
    replay_rpa_run,
    resolve_domain,
    soft_delete_patch,
)

router = APIRouter(prefix="/api/kb", tags=["kb"])

_RULEBOOK_TYPES = {"platform_policy", "compliance", "quality_gate", "style", "delivery"}
_SEVERITIES = {"info", "warn", "blocker"}
_SOURCE_LEVELS = {"official", "law", "platform_help", "crawler_observed", "synthetic", "manual"}
_ENTITY_TYPES = {"case", "asset", "user_need", "review", "topic", "rulebook", "playbook"}
_IO_DIRECTIONS = {"ingest", "egress"}
_PLAYBOOK_STAGES = {"collect", "analysis", "rewrite", "copy", "review", "strategy"}
_COMMON_STATUSES = {"draft", "active", "inactive", "archived"}


def _normalize_account_id(value: str) -> str | None:
    text = str(value or "").strip()
    return text if text else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_text_list(values: Any, *, max_items: int = 30, max_len: int = 120) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values[:max_items]:
        text = str(item or "").strip()
        if not text:
            continue
        result.append(text[:max_len])
    return result


def _normalize_rulebook_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _RULEBOOK_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid rule_type")
    return normalized


def _normalize_severity(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _SEVERITIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid severity")
    return normalized


def _normalize_source_level(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _SOURCE_LEVELS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid source_level")
    return normalized


def _normalize_common_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _COMMON_STATUSES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid status")
    return normalized


def _normalize_playbook_stage(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _PLAYBOOK_STAGES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid stage")
    return normalized


def _normalize_io_direction(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _IO_DIRECTIONS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid direction")
    return normalized


def _normalize_entity_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _ENTITY_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid entity_type")
    return normalized


def _ensure_exists(client: Client, table: str, item_id: str) -> Dict[str, Any]:
    rows = client.table(table).select("*").eq("id", item_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{table} item not found")
    return rows[0]


def _list_query(
    client: Client,
    *,
    table: str,
    domain_id: str,
    account_id: str | None,
    limit: int,
):
    query = (
        client.table(table)
        .select("*")
        .eq("domain_id", domain_id)
        .is_("deleted_at", "null")
        .order("updated_at", desc=True)
        .limit(max(1, min(limit, 200)))
    )
    if account_id:
        query = query.eq("account_id", account_id)
    return query


def _load_latest_metrics_for_task(client: Client, pipeline_task_id: str) -> Dict[str, Any]:
    task_id = str(pipeline_task_id or "").strip()
    if not task_id:
        return {}
    rows = (
        client.table("task_metrics")
        .select("metrics_jsonb")
        .eq("pipeline_task_id", task_id)
        .order("captured_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return {}
    metrics = rows[0].get("metrics_jsonb")
    return metrics if isinstance(metrics, dict) else {}


@router.get("/cases", response_model=KbCaseListResponse)
def list_cases(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    rows = _list_query(client, table="cases", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit).execute().data or []
    return {"items": rows}


@router.post("/cases", response_model=KbCaseRecord)
def create_case(
    request: KbCaseCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "platform": request.platform,
        "author": request.author,
        "url": request.url,
        "title": request.title.strip(),
        "content": request.content,
        "metrics": request.metrics if isinstance(request.metrics, dict) else {},
        "hook": request.hook,
        "structure": request.structure,
        "analysis": request.analysis,
        "source_type": request.source_type,
        "source_ref": request.source_ref,
        "created_by": actor.user_id,
    }
    rows = client.table("cases").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create case failed")
    return rows[0]


@router.patch("/cases/{case_id}", response_model=KbCaseRecord)
def patch_case(
    case_id: str,
    request: KbCasePatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "cases", case_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    if "title" in patch and not str(patch["title"]).strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title cannot be empty")
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "cases", case_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("cases").update(patch).eq("id", case_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch case failed")
    return rows[0]


@router.get("/assets", response_model=KbAssetListResponse)
def list_assets(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    type: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = _list_query(client, table="assets", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit)
    if str(type or "").strip():
        query = query.eq("type", str(type).strip())
    return {"items": query.execute().data or []}


@router.post("/assets", response_model=KbAssetRecord)
def create_asset(
    request: KbAssetCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "type": request.type,
        "content": request.content,
        "source": request.source,
        "usable_scene": request.usable_scene,
        "is_verified": request.is_verified,
        "summary": request.summary,
        "source_type": request.source_type,
        "source_ref": request.source_ref,
        "created_by": actor.user_id,
    }
    rows = client.table("assets").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create asset failed")
    return rows[0]


@router.patch("/assets/{asset_id}", response_model=KbAssetRecord)
def patch_asset(
    asset_id: str,
    request: KbAssetPatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "assets", asset_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "assets", asset_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("assets").update(patch).eq("id", asset_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch asset failed")
    return rows[0]


@router.get("/user-needs", response_model=KbUserNeedListResponse)
def list_user_needs(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    demand_type: str = Query(default=""),
    emotion: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = _list_query(client, table="user_needs", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit)
    if str(demand_type or "").strip():
        query = query.eq("demand_type", demand_type.strip())
    if str(emotion or "").strip():
        query = query.eq("emotion", emotion.strip())
    return {"items": query.execute().data or []}


@router.post("/user-needs", response_model=KbUserNeedRecord)
def create_user_need(
    request: KbUserNeedCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "user_type": request.user_type,
        "original_text": request.original_text,
        "scenario": request.scenario,
        "demand_type": request.demand_type,
        "emotion": request.emotion,
        "real_problem": request.real_problem,
        "source_type": request.source_type,
        "source_ref": request.source_ref,
        "created_by": actor.user_id,
    }
    rows = client.table("user_needs").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create user need failed")
    return rows[0]


@router.patch("/user-needs/{need_id}", response_model=KbUserNeedRecord)
def patch_user_need(
    need_id: str,
    request: KbUserNeedPatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "user_needs", need_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "user_needs", need_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("user_needs").update(patch).eq("id", need_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch user need failed")
    return rows[0]


@router.get("/topics", response_model=KbTopicListResponse)
def list_topics(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    status_filter: str = Query(default="", alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = _list_query(client, table="topics", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit)
    if str(status_filter or "").strip():
        query = query.eq("status", normalize_topic_status(status_filter))
    return {"items": query.execute().data or []}


@router.post("/topics", response_model=KbTopicRecord)
def create_topic(
    request: KbTopicCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "title": request.title,
        "topic_description": request.topic_description,
        "target_user": request.target_user,
        "platform": request.platform,
        "structure_type": request.structure_type,
        "status": normalize_topic_status(request.status),
        "reason": request.reason,
        "source_type": request.source_type,
        "source_ref": request.source_ref,
        "created_by": actor.user_id,
    }
    rows = client.table("topics").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create topic failed")
    return rows[0]


@router.patch("/topics/{topic_id}", response_model=KbTopicRecord)
def patch_topic(
    topic_id: str,
    request: KbTopicPatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "topics", topic_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    if "status" in patch:
        patch["status"] = normalize_topic_status(str(patch["status"]))
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "topics", topic_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("topics").update(patch).eq("id", topic_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch topic failed")
    return rows[0]


@router.get("/reviews", response_model=KbReviewListResponse)
def list_reviews(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    rows = _list_query(client, table="reviews", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit).execute().data or []
    return {"items": rows}


@router.post("/reviews", response_model=KbReviewRecord)
def create_review(
    request: KbReviewCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    normalized_task_id = _normalize_account_id(request.pipeline_task_id)
    metrics = request.metrics if isinstance(request.metrics, dict) else {}
    if not metrics and normalized_task_id:
        metrics = _load_latest_metrics_for_task(client, normalized_task_id)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "topic_id": _normalize_account_id(request.topic_id),
        "pipeline_task_id": normalized_task_id,
        "content_item_ref": request.content_item_ref,
        "platform": request.platform,
        "metrics": metrics,
        "success_points": request.success_points,
        "failure_points": request.failure_points,
        "improvement": request.improvement,
        "summary_text": request.summary_text,
        "source_type": request.source_type,
        "source_ref": request.source_ref,
        "created_by": actor.user_id,
    }
    rows = client.table("reviews").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create review failed")
    return rows[0]


@router.patch("/reviews/{review_id}", response_model=KbReviewRecord)
def patch_review(
    review_id: str,
    request: KbReviewPatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "reviews", review_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    if "topic_id" in patch:
        patch["topic_id"] = _normalize_account_id(patch["topic_id"])
    if "pipeline_task_id" in patch:
        patch["pipeline_task_id"] = _normalize_account_id(patch["pipeline_task_id"])
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "reviews", review_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("reviews").update(patch).eq("id", review_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch review failed")
    return rows[0]


@router.get("/tags", response_model=KbTagListResponse)
def list_tags(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    category: str = Query(default=""),
    status_filter: str = Query(default="", alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = _list_query(client, table="tags", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit)
    if str(category or "").strip():
        query = query.eq("category", category.strip())
    if str(status_filter or "").strip():
        query = query.eq("status", status_filter.strip())
    return {"items": query.execute().data or []}


@router.post("/tags", response_model=KbTagRecord)
def create_tag(
    request: KbTagCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "name": request.name.strip(),
        "category": request.category.strip(),
        "status": request.status.strip() or "active",
        "source_type": request.source_type,
        "source_ref": request.source_ref,
        "created_by": actor.user_id,
    }
    rows = client.table("tags").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create tag failed")
    return rows[0]


@router.patch("/tags/{tag_id}", response_model=KbTagRecord)
def patch_tag(
    tag_id: str,
    request: KbTagPatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "tags", tag_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "tags", tag_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("tags").update(patch).eq("id", tag_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch tag failed")
    return rows[0]


@router.get("/entity-tags", response_model=KbEntityTagListResponse)
def list_entity_tags(
    domain_slug: str = Query(default="japan_immigration"),
    entity_type: str = Query(default=""),
    entity_id: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = (
        client.table("entity_tags")
        .select("*")
        .eq("domain_id", domain["id"])
        .is_("deleted_at", "null")
        .order("updated_at", desc=True)
        .limit(limit)
    )
    if str(entity_type or "").strip():
        query = query.eq("entity_type", entity_type.strip())
    if str(entity_id or "").strip():
        query = query.eq("entity_id", entity_id.strip())
    return {"items": query.execute().data or []}


@router.post("/entity-tags", response_model=KbEntityTagRecord)
def create_entity_tag(
    request: KbEntityTagCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "entity_type": request.entity_type,
        "entity_id": request.entity_id,
        "tag_id": request.tag_id,
        "source_type": request.source_type,
        "source_ref": request.source_ref,
        "created_by": actor.user_id,
    }
    rows = client.table("entity_tags").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create entity tag failed")
    return rows[0]


@router.patch("/entity-tags/{entity_tag_id}", response_model=KbEntityTagRecord)
def patch_entity_tag(
    entity_tag_id: str,
    request: KbEntityTagPatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "entity_tags", entity_tag_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "entity_tags", entity_tag_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("entity_tags").update(patch).eq("id", entity_tag_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch entity tag failed")
    return rows[0]


@router.get("/rulebooks", response_model=KbRulebookListResponse)
def list_rulebooks(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    rule_type: str = Query(default=""),
    severity: str = Query(default=""),
    status_filter: str = Query(default="", alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = _list_query(client, table="kb_rulebooks", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit)
    if str(rule_type or "").strip():
        query = query.eq("rule_type", _normalize_rulebook_type(rule_type))
    if str(severity or "").strip():
        query = query.eq("severity", _normalize_severity(severity))
    if str(status_filter or "").strip():
        query = query.eq("status", _normalize_common_status(status_filter))
    return {"items": query.execute().data or []}


@router.post("/rulebooks", response_model=KbRulebookRecord)
def create_rulebook(
    request: KbRulebookCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "rule_code": request.rule_code.strip().lower(),
        "rule_name": request.rule_name.strip(),
        "rule_type": _normalize_rulebook_type(request.rule_type),
        "applies_to": _normalized_text_list(request.applies_to, max_items=20, max_len=80),
        "severity": _normalize_severity(request.severity),
        "priority": max(1, min(1000, int(request.priority))),
        "source_level": _normalize_source_level(request.source_level),
        "effective_at": request.effective_at.isoformat() if request.effective_at else None,
        "citation_title": request.citation_title.strip(),
        "citation_url": request.citation_url.strip(),
        "rule_text": request.rule_text,
        "rule_jsonb": request.rule_jsonb if isinstance(request.rule_jsonb, dict) else {},
        "status": _normalize_common_status(request.status),
        "source_type": request.source_type.strip() or "manual",
        "source_ref": request.source_ref.strip(),
        "created_by": actor.user_id,
    }
    rows = client.table("kb_rulebooks").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create rulebook failed")
    return rows[0]


@router.patch("/rulebooks/{rulebook_id}", response_model=KbRulebookRecord)
def patch_rulebook(
    rulebook_id: str,
    request: KbRulebookPatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "kb_rulebooks", rulebook_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    if "account_id" in patch:
        patch["account_id"] = _normalize_account_id(patch["account_id"])
    if "rule_type" in patch:
        patch["rule_type"] = _normalize_rulebook_type(str(patch["rule_type"]))
    if "severity" in patch:
        patch["severity"] = _normalize_severity(str(patch["severity"]))
    if "source_level" in patch:
        patch["source_level"] = _normalize_source_level(str(patch["source_level"]))
    if "status" in patch:
        patch["status"] = _normalize_common_status(str(patch["status"]))
    if "applies_to" in patch:
        patch["applies_to"] = _normalized_text_list(patch["applies_to"], max_items=20, max_len=80)
    if "rule_jsonb" in patch and not isinstance(patch["rule_jsonb"], dict):
        patch["rule_jsonb"] = {}
    if "effective_at" in patch and hasattr(patch["effective_at"], "isoformat"):
        patch["effective_at"] = patch["effective_at"].isoformat()
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "kb_rulebooks", rulebook_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("kb_rulebooks").update(patch).eq("id", rulebook_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch rulebook failed")
    return rows[0]


@router.get("/playbooks", response_model=KbPlaybookListResponse)
def list_playbooks(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    stage: str = Query(default=""),
    status_filter: str = Query(default="", alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = _list_query(client, table="kb_playbooks", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit)
    if str(stage or "").strip():
        query = query.eq("stage", _normalize_playbook_stage(stage))
    if str(status_filter or "").strip():
        query = query.eq("status", _normalize_common_status(status_filter))
    return {"items": query.execute().data or []}


@router.post("/playbooks", response_model=KbPlaybookRecord)
def create_playbook(
    request: KbPlaybookCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "playbook_code": request.playbook_code.strip().lower(),
        "playbook_name": request.playbook_name.strip(),
        "stage": _normalize_playbook_stage(request.stage),
        "objective": request.objective,
        "applicability": request.applicability,
        "input_contract": request.input_contract if isinstance(request.input_contract, dict) else {},
        "method_steps": request.method_steps if isinstance(request.method_steps, list) else [],
        "output_contract": request.output_contract if isinstance(request.output_contract, dict) else {},
        "quality_checks": request.quality_checks if isinstance(request.quality_checks, dict) else {},
        "example_material": request.example_material if isinstance(request.example_material, dict) else {},
        "status": _normalize_common_status(request.status),
        "version": max(1, min(999, int(request.version))),
        "source_level": _normalize_source_level(request.source_level),
        "source_type": request.source_type.strip() or "manual",
        "source_ref": request.source_ref.strip(),
        "created_by": actor.user_id,
    }
    rows = client.table("kb_playbooks").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create playbook failed")
    return rows[0]


@router.patch("/playbooks/{playbook_id}", response_model=KbPlaybookRecord)
def patch_playbook(
    playbook_id: str,
    request: KbPlaybookPatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "kb_playbooks", playbook_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    if "account_id" in patch:
        patch["account_id"] = _normalize_account_id(patch["account_id"])
    if "stage" in patch:
        patch["stage"] = _normalize_playbook_stage(str(patch["stage"]))
    if "status" in patch:
        patch["status"] = _normalize_common_status(str(patch["status"]))
    if "source_level" in patch:
        patch["source_level"] = _normalize_source_level(str(patch["source_level"]))
    if "version" in patch:
        patch["version"] = max(1, min(999, int(patch["version"])))
    if "method_steps" in patch and not isinstance(patch["method_steps"], list):
        patch["method_steps"] = []
    for field in ("input_contract", "output_contract", "quality_checks", "example_material"):
        if field in patch and not isinstance(patch[field], dict):
            patch[field] = {}
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "kb_playbooks", playbook_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("kb_playbooks").update(patch).eq("id", playbook_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch playbook failed")
    return rows[0]


@router.get("/io-rules", response_model=KbIoRuleListResponse)
def list_io_rules(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    direction: str = Query(default=""),
    entity_type: str = Query(default=""),
    target_agent: str = Query(default=""),
    status_filter: str = Query(default="", alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = _list_query(client, table="kb_io_rules", domain_id=domain["id"], account_id=_normalize_account_id(account_id), limit=limit)
    if str(direction or "").strip():
        query = query.eq("direction", _normalize_io_direction(direction))
    if str(entity_type or "").strip():
        query = query.eq("entity_type", _normalize_entity_type(entity_type))
    if str(target_agent or "").strip():
        query = query.eq("target_agent", target_agent.strip())
    if str(status_filter or "").strip():
        query = query.eq("status", _normalize_common_status(status_filter))
    return {"items": query.execute().data or []}


@router.post("/io-rules", response_model=KbIoRuleRecord)
def create_io_rule(
    request: KbIoRuleCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    domain = resolve_domain(client, request.domain_slug)
    payload = {
        "domain_id": domain["id"],
        "account_id": _normalize_account_id(request.account_id),
        "rule_code": request.rule_code.strip().lower(),
        "direction": _normalize_io_direction(request.direction),
        "entity_type": _normalize_entity_type(request.entity_type),
        "target_agent": request.target_agent.strip(),
        "required_fields": request.required_fields if isinstance(request.required_fields, list) else [],
        "field_mapping": request.field_mapping if isinstance(request.field_mapping, dict) else {},
        "validation_jsonb": request.validation_jsonb if isinstance(request.validation_jsonb, dict) else {},
        "dedupe_keys": request.dedupe_keys if isinstance(request.dedupe_keys, list) else [],
        "quality_gate_jsonb": request.quality_gate_jsonb if isinstance(request.quality_gate_jsonb, dict) else {},
        "output_template": request.output_template if isinstance(request.output_template, dict) else {},
        "status": _normalize_common_status(request.status),
        "source_type": request.source_type.strip() or "manual",
        "source_ref": request.source_ref.strip(),
        "created_by": actor.user_id,
    }
    rows = client.table("kb_io_rules").insert(payload).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="create io rule failed")
    return rows[0]


@router.patch("/io-rules/{io_rule_id}", response_model=KbIoRuleRecord)
def patch_io_rule(
    io_rule_id: str,
    request: KbIoRulePatchRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    _ensure_exists(client, "kb_io_rules", io_rule_id)
    patch = {k: v for k, v in request.model_dump(exclude_none=True).items() if k != "deleted"}
    if "account_id" in patch:
        patch["account_id"] = _normalize_account_id(patch["account_id"])
    if "status" in patch:
        patch["status"] = _normalize_common_status(str(patch["status"]))
    for field in ("required_fields", "dedupe_keys"):
        if field in patch and not isinstance(patch[field], list):
            patch[field] = []
    for field in ("field_mapping", "validation_jsonb", "quality_gate_jsonb", "output_template"):
        if field in patch and not isinstance(patch[field], dict):
            patch[field] = {}
    patch.update(soft_delete_patch(request.deleted))
    if not patch:
        return _ensure_exists(client, "kb_io_rules", io_rule_id)
    patch["updated_at"] = _now_iso()
    rows = client.table("kb_io_rules").update(patch).eq("id", io_rule_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="patch io rule failed")
    return rows[0]


@router.get("/ingestion-logs", response_model=KbIngestionLogListResponse)
def list_ingestion_logs(
    domain_slug: str = Query(default="japan_immigration"),
    entity_type: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=300),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = (
        client.table("ingestion_logs")
        .select("*")
        .eq("domain_id", domain["id"])
        .order("updated_at", desc=True)
        .limit(limit)
    )
    if str(entity_type or "").strip():
        query = query.eq("entity_type", entity_type.strip())
    return {"items": query.execute().data or []}


@router.get("/ai-jobs", response_model=KbAiJobListResponse)
def list_ai_jobs(
    domain_slug: str = Query(default="japan_immigration"),
    status_filter: str = Query(default="", alias="status"),
    limit: int = Query(default=100, ge=1, le=300),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = (
        client.table("ai_jobs")
        .select("*")
        .eq("domain_id", domain["id"])
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .limit(limit)
    )
    if str(status_filter or "").strip():
        query = query.eq("status", status_filter.strip())
    return {"items": query.execute().data or []}


@router.post("/import/octopus", response_model=KbOctopusImportResult)
def import_octopus(
    request: KbOctopusImportRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    return import_octopus_payload(client, request, created_by=actor.user_id)


@router.post("/topics/recommend", response_model=KbTopicRecommendResponse)
def recommend_topic_items(
    request: KbTopicRecommendRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    return recommend_topics(client, request, created_by=actor.user_id)


@router.get("/rpa/task-templates", response_model=KbRpaTaskTemplateListResponse)
def list_rpa_task_templates(
    domain_slug: str = Query(default="japan_immigration"),
    active_only: bool = Query(default=True),
    limit: int = Query(default=200, ge=1, le=500),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = (
        client.table("rpa_task_templates")
        .select("*")
        .eq("domain_id", domain["id"])
        .is_("deleted_at", "null")
        .order("updated_at", desc=True)
        .limit(limit)
    )
    if active_only:
        query = query.eq("is_active", True)
    rows = query.execute().data or []
    return {"items": rows}


@router.get("/rpa/task-runs", response_model=KbRpaTaskRunListResponse)
def list_rpa_task_runs(
    domain_slug: str = Query(default="japan_immigration"),
    status_filter: str = Query(default="", alias="status"),
    task_type: str = Query(default=""),
    entity_type: str = Query(default=""),
    limit: int = Query(default=120, ge=1, le=500),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = (
        client.table("rpa_task_runs")
        .select("*")
        .eq("domain_id", domain["id"])
        .is_("deleted_at", "null")
        .order("updated_at", desc=True)
        .limit(limit)
    )
    if str(status_filter or "").strip():
        query = query.eq("status", status_filter.strip())
    if str(task_type or "").strip():
        query = query.eq("task_type", task_type.strip())
    if str(entity_type or "").strip():
        query = query.eq("entity_type", entity_type.strip())
    rows = query.execute().data or []
    return {"items": rows}


@router.get("/rpa/raw-payloads", response_model=KbRpaRawPayloadListResponse)
def list_rpa_raw_payloads(
    domain_slug: str = Query(default="japan_immigration"),
    run_id: str = Query(default=""),
    status_filter: str = Query(default="", alias="status"),
    limit: int = Query(default=200, ge=1, le=1000),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = (
        client.table("rpa_raw_payloads")
        .select("*")
        .eq("domain_id", domain["id"])
        .order("created_at", desc=True)
        .limit(limit)
    )
    if str(run_id or "").strip():
        query = query.eq("run_id", run_id.strip())
    if str(status_filter or "").strip():
        query = query.eq("status", status_filter.strip())
    rows = query.execute().data or []
    return {"items": rows}


@router.get("/rpa/clean-results", response_model=KbRpaCleanResultListResponse)
def list_rpa_clean_results(
    domain_slug: str = Query(default="japan_immigration"),
    run_id: str = Query(default=""),
    accepted: str = Query(default=""),
    reject_code: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    domain = resolve_domain(client, domain_slug)
    query = (
        client.table("rpa_clean_results")
        .select("*")
        .eq("domain_id", domain["id"])
        .order("updated_at", desc=True)
        .limit(limit)
    )
    if str(run_id or "").strip():
        query = query.eq("run_id", run_id.strip())
    if str(accepted or "").strip().lower() in {"true", "false"}:
        query = query.eq("accepted", accepted.strip().lower() == "true")
    if str(reject_code or "").strip():
        query = query.eq("reject_code", reject_code.strip())
    rows = query.execute().data or []
    return {"items": rows}


@router.post("/rpa/runs/{run_id}/replay", response_model=KbRpaReplayResponse)
def replay_rpa_task_run(
    run_id: str,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    return replay_rpa_run(client, run_id, created_by=f"{actor.user_id}:rpa_replay")
