from __future__ import annotations

from collections import Counter
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
import httpx
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from supabase import Client

from app.config import Settings, get_settings
from app.db import get_supabase, user_facing_data_message
from app.models import (
    AccountCollectionRunRequest,
    Actor,
    AuditLogEntry,
    BrowserIntelImportRequest,
    ChiefEvolutionRequest,
    CrawlerTemplateUpsertRequest,
    HotPostAnalyzeRequest,
    IntelAnalyzeRequest,
    MemoryItemStatusUpdateRequest,
    PendingStrategyApplyRequest,
    McpReadonlyCollectIntelRequest,
    McpReadonlyCustomCallRequest,
    McpReadonlySearchRequest,
    McpReadonlySyncMetricsRequest,
    McpWriteCustomCallRequest,
    McpWritePublishRequest,
    OrchestratorControlUpsertRequest,
    OrchestratorControlRollbackRequest,
    OrchestratorSelfUpgradeRequest,
    OpsAutoConfigRequest,
    OpsAnalysisFeedbackRequest,
    PlaywrightToolRunRequest,
    PublishHistoryImportRequest,
    PublishIdentityImportRequest,
    PublishFeedbackReconcileRequest,
    RebuildXhsContentRequest,
    TaskPlanConfirmRequest,
    TaskPlanRunRequest,
)
from app.security import require_roles
from app.services.audit import write_audit_log
from app.services.account_service import (
    _normalize_collection_plan as _normalize_account_collection_plan,
    _normalize_feedback_plan as _normalize_account_feedback_plan,
    activate_prompt_version,
    create_prompt_version,
    get_prompt_version_by_version,
    list_prompt_versions,
)
from app.services.pipeline_service import pipeline_schema_ready
from app.services.browser_intel import import_browser_intel

router = APIRouter(prefix="/api/ops", tags=["ops"])


@router.post("/accounts/{account_id}/browser-intel")
def import_account_browser_intel(
    account_id: str,
    request: BrowserIntelImportRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    return import_browser_intel(client, account_id=account_id, request=request, actor=actor)


TOKEN_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}")
URL_RE = re.compile(r"https?://\S+")
HTML_ENTITY_RE = re.compile(r"&[a-zA-Z]{2,10};")
HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{5,}$")
STOPWORDS = {
    "日本",
    "我们",
    "你们",
    "这个",
    "那个",
    "内容",
    "视频",
    "文章",
    "今天",
    "最新",
    "相关",
    "一个",
    "可以",
    "进行",
}
WEB_NOISE_TOKENS = {
    "nbsp",
    "font",
    "href",
    "https",
    "http",
    "www",
    "com",
    "google",
    "news",
    "rss",
    "target",
    "blank",
    "color",
    "style",
    "span",
    "div",
    "article",
}
KEEP_LATIN_TOKENS = {"eb5", "eb1a", "h1b", "f1", "j1", "n1", "n2", "n3", "n4", "n5", "gpa", "toefl", "ielts"}
XHS_FEED_ID_RE = re.compile(r"/explore/([0-9A-Za-z]+)")


def _extract_publish_identity_from_url(url_text: str) -> Dict[str, str]:
    text = str(url_text or "").strip()
    if not text:
        return {}
    try:
        parsed = urlparse(text)
    except Exception:  # noqa: BLE001
        return {}
    path = str(parsed.path or "")
    feed_id = ""
    matched = XHS_FEED_ID_RE.search(path)
    if matched:
        feed_id = matched.group(1).strip()
    xsec_token = str((parse_qs(parsed.query or "").get("xsec_token") or [""])[0]).strip()
    if not xsec_token and parsed.fragment:
        xsec_token = str((parse_qs(parsed.fragment).get("xsec_token") or [""])[0]).strip()
    identity: Dict[str, str] = {"published_url": text}
    if feed_id:
        identity["feed_id"] = feed_id
        identity["remote_post_id"] = feed_id
    if xsec_token:
        identity["xsec_token"] = xsec_token
    return identity


def _normalize_title_for_match(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"))


def _task_identity_from_publish_json(task: Dict[str, Any]) -> Dict[str, str]:
    publish_jsonb = task.get("publish_jsonb") if isinstance(task.get("publish_jsonb"), dict) else {}
    identity = publish_jsonb.get("identity") if isinstance(publish_jsonb.get("identity"), dict) else {}
    last_result = publish_jsonb.get("last_result") if isinstance(publish_jsonb.get("last_result"), dict) else {}
    merged = {
        "feed_id": str(identity.get("feed_id") or last_result.get("feed_id") or "").strip(),
        "xsec_token": str(identity.get("xsec_token") or last_result.get("xsec_token") or "").strip(),
        "published_url": str(identity.get("published_url") or last_result.get("published_url") or "").strip(),
        "remote_post_id": str(identity.get("remote_post_id") or last_result.get("remote_post_id") or "").strip(),
    }
    if merged["feed_id"] and not merged["remote_post_id"]:
        merged["remote_post_id"] = merged["feed_id"]
    return merged


def _to_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result < 0:
        return 0.0
    return result


def _normalize_imported_metrics(raw: Dict[str, Any]) -> Dict[str, float]:
    source = raw if isinstance(raw, dict) else {}
    aliases: Dict[str, List[str]] = {
        "impressions": ["impressions", "exposure", "show_count"],
        "views": ["views", "view_count", "reads", "read_count"],
        "clicks": ["clicks", "click_count"],
        "ctr": ["ctr", "click_through_rate"],
        "likes": ["likes", "like_count"],
        "collects": ["collects", "collect_count", "favorites", "favorite_count"],
        "comments_count": ["comments_count", "comments", "comment_count"],
        "shares": ["shares", "share_count", "forwards"],
        "follows": ["follows", "followers_delta", "follow_count", "new_followers"],
        "avg_read_seconds": ["avg_read_seconds", "stay_seconds", "avg_stay_seconds", "avg_read_time"],
        "inquiries": ["inquiries", "private_inquiry_count", "leads_count", "consult_count"],
    }
    normalized: Dict[str, float] = {}
    for target, names in aliases.items():
        value: float | None = None
        for name in names:
            if name not in source:
                continue
            parsed = _to_number(source.get(name))
            if parsed is None:
                continue
            value = parsed
            break
        if value is not None:
            normalized[target] = value
    return normalized


def _compute_metric_change_from_snapshots(snapshots: List[Dict[str, Any]]) -> Dict[str, float]:
    if len(snapshots) < 2:
        return {}
    first = snapshots[0] if isinstance(snapshots[0], dict) else {}
    last = snapshots[-1] if isinstance(snapshots[-1], dict) else {}
    delta: Dict[str, float] = {}
    for key in ["impressions", "views", "clicks", "likes", "collects", "comments_count", "shares", "follows", "inquiries"]:
        before = _to_number(first.get(key))
        after = _to_number(last.get(key))
        if before is None or after is None:
            continue
        delta[key] = round(after - before, 4)
    return delta


def _build_history_memory_content(
    *,
    title: str,
    published_at: str,
    metrics: Dict[str, float],
    metric_change: Dict[str, float],
    tags: List[str],
    note: str,
) -> str:
    lines = [
        f"历史帖子：{title or '未命名'}",
        f"发布时间：{published_at or '未知'}",
    ]
    if metrics:
        lines.append(
            "当前指标："
            + " / ".join(
                [
                    f"曝光 {int(metrics.get('impressions', 0))}" if "impressions" in metrics else "",
                    f"阅读 {int(metrics.get('views', 0))}" if "views" in metrics else "",
                    f"点赞 {int(metrics.get('likes', 0))}" if "likes" in metrics else "",
                    f"收藏 {int(metrics.get('collects', 0))}" if "collects" in metrics else "",
                    f"评论 {int(metrics.get('comments_count', 0))}" if "comments_count" in metrics else "",
                    f"转发 {int(metrics.get('shares', 0))}" if "shares" in metrics else "",
                    f"关注增量 {int(metrics.get('follows', 0))}" if "follows" in metrics else "",
                ]
            ).replace(" /  / ", " / ")
        )
    if metric_change:
        lines.append(
            "变化趋势："
            + " / ".join(
                [
                    f"阅读Δ {int(metric_change.get('views', 0))}" if "views" in metric_change else "",
                    f"收藏Δ {int(metric_change.get('collects', 0))}" if "collects" in metric_change else "",
                    f"评论Δ {int(metric_change.get('comments_count', 0))}" if "comments_count" in metric_change else "",
                    f"关注Δ {int(metric_change.get('follows', 0))}" if "follows" in metric_change else "",
                ]
            ).replace(" /  / ", " / ")
        )
    if tags:
        lines.append("标签：" + "、".join(tags[:12]))
    if note:
        lines.append("备注：" + note[:500])
    return "\n".join([line for line in lines if line.strip()])[:3800]


def _normalize_history_snapshot_rows(raw_snapshots: Any) -> List[Dict[str, Any]]:
    source = raw_snapshots if isinstance(raw_snapshots, list) else []
    rows: List[Dict[str, Any]] = []
    for raw in source[:300]:
        item = raw if isinstance(raw, dict) else {}
        metrics = _normalize_imported_metrics(item)
        captured_at = str(item.get("captured_at") or item.get("time") or item.get("at") or "").strip()
        row = {"captured_at": captured_at, **metrics}
        if any(key for key in row.keys() if key != "captured_at"):
            rows.append(row)
    return rows


def _merge_snapshot_rows(existing_rows: Any, incoming_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    existing = _normalize_history_snapshot_rows(existing_rows)
    all_rows = existing + list(incoming_rows or [])
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in all_rows:
        key = "|".join(
            [
                str(row.get("captured_at") or ""),
                str(row.get("views") or ""),
                str(row.get("collects") or ""),
                str(row.get("comments_count") or ""),
                str(row.get("follows") or ""),
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped[-200:]


@router.post("/accounts/{account_id}/collect")
async def run_account_collection_ops(
    http_request: Request,
    account_id: str,
    request: AccountCollectionRunRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator")),
    db_client: Client = Depends(get_supabase),
) -> Dict[str, Any]:
    from app.routers.browser_bridge import AccountInput, local_control, start_collection
    from app.services.browser_bridge import bridge_store
    from app.services.xhs_cli_collection import collect_account, uses_cli
    from starlette.concurrency import run_in_threadpool
    if uses_cli(db_client, account_id):
        local_control(http_request)
        return await run_in_threadpool(collect_account, db_client, account_id=account_id,
            domain_slug=request.domain_slug, source_kind=request.source_kind, actor=_)
    if bridge_store.status(account_id)["connected"]:
        local_control(http_request)
        return await start_collection(account_id, AccountInput(domain_slug=request.domain_slug, source_kind=request.source_kind), db_client)
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            response = await client.post(
                f"{settings.agent_service_url}/accounts/{account_id}/collect",
                json={
                    "domain_slug": request.domain_slug,
                    "account_id": account_id,
                    "source_kind": request.source_kind,
                    "triggered_by": "api:ops.account_collect",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/playwright-tools/run")
async def run_playwright_tool_ops(
    request: PlaywrightToolRunRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            response = await client.post(
                f"{settings.agent_service_url}/playwright-tools/run",
                json={
                    "account_id": request.account_id,
                    "tool_name": request.tool_name,
                    "params": request.params,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/task-plan/confirm")
async def confirm_task_plan_ops(
    request: TaskPlanConfirmRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                f"{settings.agent_service_url}/task-plan/confirm",
                json={
                    "task_plan": request.task_plan,
                    "confirm_text": request.confirm_text,
                    "second_confirm_text": request.second_confirm_text,
                    "actor": request.actor,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/task-plan/run")
async def run_task_plan_ops(
    request: TaskPlanRunRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            response = await client.post(
                f"{settings.agent_service_url}/task-plan/run",
                json={
                    "task_plan": request.task_plan,
                    "domain_slug": request.domain_slug,
                    "account_id": request.account_id,
                    "triggered_by": request.triggered_by,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/publish-feedback/reconcile")
async def reconcile_publish_feedback_ops(
    request: PublishFeedbackReconcileRequest,
    settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles("admin", "operator")),
    database: Client = Depends(get_supabase),
) -> Dict[str, Any]:
    from app.services.feedback_collection import collect_due_observations
    observations = await asyncio.to_thread(collect_due_observations, database,
        domain_slug=request.domain_slug, account_id=request.account_id, limit=request.limit, actor=actor)
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            response = await client.post(
                f"{settings.agent_service_url}/publish-feedback/reconcile",
                json={
                    "domain_slug": request.domain_slug,
                    "account_id": request.account_id,
                    "limit": request.limit,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {**response.json(), 'observation_collection': observations}


@router.post("/publish-feedback/import-identities")
async def import_publish_feedback_identities_ops(
    request: PublishIdentityImportRequest,
    settings: Settings = Depends(get_settings),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    account_id = str(request.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="account_id is required")
    items = request.items or []
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="items is required")

    safe_recent = max(request.limit_recent, len(items) * 3)
    safe_recent = max(20, min(safe_recent, 300))
    raw_rows = (
        client.table("pipeline_tasks")
        .select("id,account_id,status,stage,payload_jsonb,publish_jsonb,published_at,updated_at")
        .eq("domain_id", domain["id"])
        .eq("channel", "xiaohongshu")
        .in_("status", ["published", "metrics_ready", "done", "publish_failed", "reflecting", "reflection_failed"])
        .order("published_at", desc=True)
        .order("updated_at", desc=True)
        .limit(safe_recent)
        .execute()
    ).data or []

    rows: List[Dict[str, Any]] = []
    for row in raw_rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        row_account = str(row.get("account_id") or payload.get("channel_account_id") or "").strip()
        if row_account != account_id:
            continue
        rows.append(row)

    rows_by_id = {str(row.get("id") or "").strip(): row for row in rows}
    pending_identity_rows = [
        row
        for row in rows
        if not _task_identity_from_publish_json(row).get("feed_id")
    ]
    pending_pointer = 0
    used_task_ids: set[str] = set()
    now_iso = datetime.now(timezone.utc).isoformat()

    updated_items: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    matched_counter: Counter[str] = Counter()

    for idx, item in enumerate(items):
        item_task_id = str(item.task_id or "").strip()
        item_title = str(item.title or "").strip()
        item_url = str(item.published_url or "").strip()

        parsed_identity = _extract_publish_identity_from_url(item_url)
        explicit_identity = {
            "published_url": item_url,
            "feed_id": str(item.feed_id or "").strip(),
            "xsec_token": str(item.xsec_token or "").strip(),
            "remote_post_id": str(item.remote_post_id or "").strip(),
        }
        identity = {
            **parsed_identity,
            **{k: v for k, v in explicit_identity.items() if v},
        }
        if identity.get("feed_id") and not identity.get("remote_post_id"):
            identity["remote_post_id"] = str(identity.get("feed_id") or "").strip()
        if not identity.get("published_url") and identity.get("feed_id"):
            if identity.get("xsec_token"):
                identity["published_url"] = (
                    f"https://www.xiaohongshu.com/explore/{identity['feed_id']}?xsec_token={identity['xsec_token']}"
                )
            else:
                identity["published_url"] = f"https://www.xiaohongshu.com/explore/{identity['feed_id']}"

        if not identity.get("feed_id") and not identity.get("published_url"):
            skipped_items.append(
                {
                    "index": idx,
                    "task_id": item_task_id,
                    "title": item_title,
                    "reason": "missing_identity_input",
                }
            )
            continue

        target_row: Dict[str, Any] | None = None
        matched_by = ""

        if item_task_id:
            target_row = rows_by_id.get(item_task_id)
            matched_by = "task_id" if target_row else ""

        if target_row is None and item_title:
            target_title_key = _normalize_title_for_match(item_title)
            for row in rows:
                row_id = str(row.get("id") or "").strip()
                if row_id in used_task_ids:
                    continue
                payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
                row_title = str(payload.get("title") or payload.get("topic") or payload.get("keyword") or "").strip()
                if not row_title:
                    continue
                row_key = _normalize_title_for_match(row_title)
                if target_title_key and row_key and (
                    row_key == target_title_key or target_title_key in row_key or row_key in target_title_key
                ):
                    target_row = row
                    matched_by = "title_match"
                    break

        if target_row is None:
            while pending_pointer < len(pending_identity_rows):
                candidate = pending_identity_rows[pending_pointer]
                pending_pointer += 1
                candidate_id = str(candidate.get("id") or "").strip()
                if candidate_id in used_task_ids:
                    continue
                target_row = candidate
                matched_by = "auto_recent_missing"
                break

        if target_row is None:
            skipped_items.append(
                {
                    "index": idx,
                    "task_id": item_task_id,
                    "title": item_title,
                    "reason": "no_target_task",
                }
            )
            continue

        task_id = str(target_row.get("id") or "").strip()
        publish_jsonb = (
            target_row.get("publish_jsonb")
            if isinstance(target_row.get("publish_jsonb"), dict)
            else {}
        )
        existing_identity = _task_identity_from_publish_json(target_row)
        merged_identity = {
            **existing_identity,
            **identity,
            "recovered_by": "manual_import",
            "recovered_at": now_iso,
        }
        if merged_identity.get("feed_id") and not merged_identity.get("remote_post_id"):
            merged_identity["remote_post_id"] = str(merged_identity.get("feed_id") or "").strip()

        feedback_state = str(publish_jsonb.get("feedback_state") or "").strip()
        if merged_identity.get("feed_id") and (not feedback_state or feedback_state == "pending_identity"):
            feedback_state = "pending_metrics"

        next_publish_jsonb = {
            **publish_jsonb,
            "feedback_state": feedback_state or publish_jsonb.get("feedback_state") or "",
            "identity": {
                **(publish_jsonb.get("identity") if isinstance(publish_jsonb.get("identity"), dict) else {}),
                **merged_identity,
            },
        }
        update_payload: Dict[str, Any] = {
            "publish_jsonb": next_publish_jsonb,
            "updated_at": now_iso,
        }
        if not str(target_row.get("account_id") or "").strip():
            update_payload["account_id"] = account_id
        if str(target_row.get("status") or "") == "publish_failed" and merged_identity.get("feed_id"):
            update_payload["status"] = "published"
            if str(target_row.get("stage") or "") in {"publish_failed", "publishing", ""}:
                update_payload["stage"] = "feedback_pending"

        client.table("pipeline_tasks").update(update_payload).eq("id", task_id).execute()
        used_task_ids.add(task_id)
        matched_counter.update([matched_by or "unknown"])
        updated_items.append(
            {
                "index": idx,
                "task_id": task_id,
                "matched_by": matched_by or "unknown",
                "feed_id": str(merged_identity.get("feed_id") or ""),
                "has_xsec_token": bool(str(merged_identity.get("xsec_token") or "").strip()),
                "published_url": str(merged_identity.get("published_url") or ""),
            }
        )

    reconcile_result: Dict[str, Any] = {
        "status": "skipped",
        "reason": "trigger_reconcile=false_or_no_updates",
    }
    if request.trigger_reconcile and updated_items:
        async with httpx.AsyncClient(timeout=180.0) as http_client:
            try:
                resp = await http_client.post(
                    f"{settings.agent_service_url}/publish-feedback/reconcile",
                    json={
                        "domain_slug": request.domain_slug,
                        "account_id": account_id,
                        "limit": max(20, min(50, len(updated_items) * 4)),
                    },
                )
                resp.raise_for_status()
                reconcile_result = resp.json()
            except httpx.HTTPError as exc:
                reconcile_result = {"status": "error", "reason": str(exc)}

    return {
        "status": "ok",
        "domain_slug": request.domain_slug,
        "account_id": account_id,
        "received": len(items),
        "updated": len(updated_items),
        "matched_summary": dict(matched_counter),
        "updated_items": updated_items,
        "skipped_items": skipped_items,
        "reconcile": reconcile_result,
    }


@router.post("/publish-feedback/import-history")
async def import_publish_feedback_history_ops(
    request: PublishHistoryImportRequest,
    settings: Settings = Depends(get_settings),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    account_id = str(request.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="account_id is required")
    items = request.items or []
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="items is required")

    account_rows = (
        client.table("channel_accounts")
        .select("id,account_name,config_jsonb")
        .eq("id", account_id)
        .limit(1)
        .execute()
    ).data or []
    if not account_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    account = account_rows[0]
    account_name = str(account.get("account_name") or "").strip() or "当前账号"
    account_cfg = account.get("config_jsonb") if isinstance(account.get("config_jsonb"), dict) else {}
    feedback_plan = _normalize_account_feedback_plan(
        account_cfg.get("feedback_plan") if isinstance(account_cfg.get("feedback_plan"), dict) else {}
    )
    checkpoints = feedback_plan.get("checkpoints_hours") if isinstance(feedback_plan.get("checkpoints_hours"), list) else [1, 3, 24]

    raw_rows = (
        client.table("pipeline_tasks")
        .select("id,account_id,status,stage,payload_jsonb,publish_jsonb,metrics_jsonb,published_at,updated_at")
        .eq("domain_id", domain["id"])
        .eq("channel", "xiaohongshu")
        .order("updated_at", desc=True)
        .limit(400)
        .execute()
    ).data or []

    rows: List[Dict[str, Any]] = []
    for row in raw_rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        row_account = str(row.get("account_id") or payload.get("channel_account_id") or "").strip()
        if row_account != account_id:
            continue
        rows.append(row)

    rows_by_id = {str(row.get("id") or "").strip(): row for row in rows}
    rows_by_feed: Dict[str, Dict[str, Any]] = {}
    rows_by_title: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        identity = _task_identity_from_publish_json(row)
        feed_key = str(identity.get("feed_id") or "").strip()
        if feed_key and feed_key not in rows_by_feed:
            rows_by_feed[feed_key] = row
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        title_key = _normalize_title_for_match(str(payload.get("title") or ""))
        if title_key and title_key not in rows_by_title:
            rows_by_title[title_key] = row

    now_iso = datetime.now(timezone.utc).isoformat()
    imported_items: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    memory_created = 0
    created_tasks = 0
    updated_tasks = 0
    match_counter: Counter[str] = Counter()

    for idx, item in enumerate(items):
        title = str(item.title or "").strip()
        identity = {
            **_extract_publish_identity_from_url(str(item.published_url or "").strip()),
            **{
                "feed_id": str(item.feed_id or "").strip(),
                "xsec_token": str(item.xsec_token or "").strip(),
                "remote_post_id": str(item.remote_post_id or "").strip(),
                "published_url": str(item.published_url or "").strip(),
            },
        }
        identity = {k: v for k, v in identity.items() if str(v or "").strip()}
        if identity.get("feed_id") and not identity.get("remote_post_id"):
            identity["remote_post_id"] = str(identity.get("feed_id") or "").strip()
        if not identity.get("published_url") and identity.get("feed_id"):
            token = str(identity.get("xsec_token") or "").strip()
            identity["published_url"] = (
                f"https://www.xiaohongshu.com/explore/{identity['feed_id']}?xsec_token={token}"
                if token
                else f"https://www.xiaohongshu.com/explore/{identity['feed_id']}"
            )

        published_at = str(item.published_at or "").strip()
        parsed_published = _parse_iso(published_at) if published_at else None
        if parsed_published is None:
            parsed_published = datetime.now(timezone.utc)
        published_at_iso = parsed_published.isoformat()

        normalized_metrics = _normalize_imported_metrics(item.metrics if isinstance(item.metrics, dict) else {})
        snapshot_source: List[Dict[str, Any]] = []
        for raw_snapshot in (item.snapshots or []):
            if hasattr(raw_snapshot, "model_dump"):
                snapshot_source.append(raw_snapshot.model_dump())
            elif hasattr(raw_snapshot, "dict"):
                snapshot_source.append(raw_snapshot.dict())  # type: ignore[call-arg]
            elif isinstance(raw_snapshot, dict):
                snapshot_source.append(raw_snapshot)
        snapshot_rows = _normalize_history_snapshot_rows(snapshot_source)
        if not snapshot_rows and normalized_metrics:
            snapshot_rows = [{"captured_at": now_iso, **normalized_metrics}]
        if snapshot_rows and not normalized_metrics:
            normalized_metrics = {
                k: v for k, v in snapshot_rows[-1].items() if k != "captured_at" and isinstance(v, (int, float))
            }
        metric_change = _compute_metric_change_from_snapshots(snapshot_rows)

        if not identity.get("feed_id") and not identity.get("published_url"):
            skipped_items.append({"index": idx, "title": title, "reason": "missing_identity_input"})
            continue

        target_row: Dict[str, Any] | None = None
        matched_by = ""
        task_id_hint = str(item.task_id or "").strip()
        if task_id_hint and task_id_hint in rows_by_id:
            target_row = rows_by_id[task_id_hint]
            matched_by = "task_id"
        if target_row is None:
            feed_key = str(identity.get("feed_id") or "").strip()
            if feed_key and feed_key in rows_by_feed:
                target_row = rows_by_feed[feed_key]
                matched_by = "feed_id"
        if target_row is None and title:
            title_key = _normalize_title_for_match(title)
            if title_key and title_key in rows_by_title:
                target_row = rows_by_title[title_key]
                matched_by = "title_match"

        if target_row is None and not request.create_task_if_missing:
            skipped_items.append({"index": idx, "title": title, "reason": "no_target_task"})
            continue

        if target_row is None:
            payload_jsonb = {
                "title": title or f"历史导入帖子#{idx + 1}",
                "channel_account_id": account_id,
                "source": "historical_feedback_import",
                "historical_import": True,
                "historical_note": str(item.note or "").strip()[:800],
            }
            publish_jsonb = {
                "identity": {
                    **identity,
                    "recovered_by": "history_import",
                    "recovered_at": now_iso,
                },
                "feedback_state": "pending_metrics" if identity.get("feed_id") else "pending_identity",
                "feedback_schedule_hours": checkpoints,
                "feedback_completed_hours": [],
                "imported_history": {
                    "enabled": True,
                    "imported_at": now_iso,
                    "import_source": "manual",
                },
            }
            metrics_jsonb = {
                "post_metrics": normalized_metrics,
                "historical_snapshots": snapshot_rows,
                "historical_metric_change": metric_change,
                "historical_import": {
                    "enabled": True,
                    "imported_at": now_iso,
                    "source": "manual",
                    "snapshot_count": len(snapshot_rows),
                },
            }
            insert_result = (
                client.table("pipeline_tasks")
                .insert(
                    {
                        "domain_id": domain["id"],
                        "account_id": account_id,
                        "channel": "xiaohongshu",
                        "content_type": "post",
                        "status": "published",
                        "stage": "feedback_pending",
                        "payload_jsonb": payload_jsonb,
                        "publish_jsonb": publish_jsonb,
                        "metrics_jsonb": metrics_jsonb,
                        "published_at": published_at_iso,
                        "created_by": "system:history_import",
                        "updated_at": now_iso,
                    }
                )
                .execute()
            ).data or []
            if not insert_result:
                skipped_items.append({"index": idx, "title": title, "reason": "insert_failed"})
                continue
            target_row = insert_result[0]
            created_tasks += 1
            matched_by = "created_new"
        else:
            task_id = str(target_row.get("id") or "").strip()
            payload_jsonb = target_row.get("payload_jsonb") if isinstance(target_row.get("payload_jsonb"), dict) else {}
            publish_jsonb = target_row.get("publish_jsonb") if isinstance(target_row.get("publish_jsonb"), dict) else {}
            metrics_jsonb = target_row.get("metrics_jsonb") if isinstance(target_row.get("metrics_jsonb"), dict) else {}
            existed_identity = _task_identity_from_publish_json(target_row)
            next_identity = {
                **existed_identity,
                **identity,
                "recovered_by": "history_import",
                "recovered_at": now_iso,
            }
            merged_snapshots = _merge_snapshot_rows(metrics_jsonb.get("historical_snapshots"), snapshot_rows)
            merged_metric_change = _compute_metric_change_from_snapshots(merged_snapshots)
            current_post_metrics = (
                metrics_jsonb.get("post_metrics") if isinstance(metrics_jsonb.get("post_metrics"), dict) else {}
            )
            merged_post_metrics = {
                **_normalize_imported_metrics(current_post_metrics),
                **normalized_metrics,
            }

            if not str(payload_jsonb.get("title") or "").strip() and title:
                payload_jsonb = {**payload_jsonb, "title": title}
            payload_jsonb["channel_account_id"] = account_id
            payload_jsonb["historical_import"] = True
            payload_jsonb["historical_note"] = str(item.note or "").strip()[:800]

            feedback_state = str(publish_jsonb.get("feedback_state") or "").strip()
            if next_identity.get("feed_id") and (not feedback_state or feedback_state == "pending_identity"):
                feedback_state = "pending_metrics"

            next_publish_jsonb = {
                **publish_jsonb,
                "identity": {
                    **(publish_jsonb.get("identity") if isinstance(publish_jsonb.get("identity"), dict) else {}),
                    **next_identity,
                },
                "feedback_state": feedback_state or "pending_metrics",
                "feedback_schedule_hours": (
                    publish_jsonb.get("feedback_schedule_hours")
                    if isinstance(publish_jsonb.get("feedback_schedule_hours"), list)
                    and publish_jsonb.get("feedback_schedule_hours")
                    else checkpoints
                ),
                "imported_history": {
                    "enabled": True,
                    "imported_at": now_iso,
                    "import_source": "manual",
                },
            }
            next_metrics_jsonb = {
                **metrics_jsonb,
                "post_metrics": merged_post_metrics,
                "historical_snapshots": merged_snapshots,
                "historical_metric_change": merged_metric_change,
                "historical_import": {
                    "enabled": True,
                    "imported_at": now_iso,
                    "source": "manual",
                    "snapshot_count": len(merged_snapshots),
                },
            }
            update_payload: Dict[str, Any] = {
                "payload_jsonb": payload_jsonb,
                "publish_jsonb": next_publish_jsonb,
                "metrics_jsonb": next_metrics_jsonb,
                "updated_at": now_iso,
            }
            if not str(target_row.get("account_id") or "").strip():
                update_payload["account_id"] = account_id
            if str(target_row.get("status") or "") == "publish_failed" and next_identity.get("feed_id"):
                update_payload["status"] = "published"
                update_payload["stage"] = "feedback_pending"
            if not target_row.get("published_at"):
                update_payload["published_at"] = published_at_iso
            client.table("pipeline_tasks").update(update_payload).eq("id", task_id).execute()
            updated_tasks += 1

        final_task_id = str(target_row.get("id") or "").strip()
        if final_task_id:
            rows_by_id[final_task_id] = target_row
        if identity.get("feed_id"):
            rows_by_feed[str(identity.get("feed_id") or "").strip()] = target_row
        if title:
            rows_by_title[_normalize_title_for_match(title)] = target_row

        if request.write_pending_memory:
            tags = [str(tag).strip() for tag in (item.tags or []) if str(tag).strip()]
            tags = [*tags, "history_import", "feedback", "bootstrap"]
            _upsert_pending_memory_item(
                client,
                domain_id=domain["id"],
                account_id=account_id,
                title=f"历史复盘沉淀：{(title or str(identity.get('feed_id') or final_task_id))[:80]}",
                content=_build_history_memory_content(
                    title=title,
                    published_at=published_at_iso,
                    metrics=normalized_metrics,
                    metric_change=metric_change,
                    tags=tags,
                    note=str(item.note or ""),
                ),
                tags=tags,
                confidence=0.76,
                created_by="system:history_import",
            )
            memory_created += 1

            if request.write_global_memory:
                global_title = f"跨账号经验模板：{(title or str(identity.get('feed_id') or final_task_id))[:80]}"
                existing_global = (
                    client.table("memory_items")
                    .select("id")
                    .eq("domain_id", domain["id"])
                    .is_("account_id", "null")
                    .eq("title", global_title[:200])
                    .eq("status", "pending")
                    .limit(1)
                    .execute()
                ).data or []
                global_payload = {
                    "content": _build_history_memory_content(
                        title=title,
                        published_at=published_at_iso,
                        metrics=normalized_metrics,
                        metric_change=metric_change,
                        tags=["global_bootstrap", *tags],
                        note=f"来源账号：{account_name}。{str(item.note or '')}",
                    ),
                    "tags": ["global_bootstrap", "history_import", "feedback"],
                    "confidence": 0.68,
                    "updated_at": now_iso,
                }
                if existing_global:
                    client.table("memory_items").update(global_payload).eq("id", existing_global[0]["id"]).execute()
                else:
                    client.table("memory_items").insert(
                        {
                            "domain_id": domain["id"],
                            "account_id": None,
                            "source_run_id": None,
                            "type": "strategy_rule",
                            "title": global_title[:200],
                            "content": global_payload["content"][:4000],
                            "tags": global_payload["tags"],
                            "status": "pending",
                            "confidence": global_payload["confidence"],
                            "created_by": "system:history_import",
                        }
                    ).execute()

        match_counter.update([matched_by or "unknown"])
        imported_items.append(
            {
                "index": idx,
                "task_id": final_task_id,
                "matched_by": matched_by or "unknown",
                "title": title,
                "feed_id": str(identity.get("feed_id") or ""),
                "snapshot_count": len(snapshot_rows),
                "metric_keys": sorted(list(normalized_metrics.keys())),
            }
        )

    reconcile_result: Dict[str, Any] = {"status": "skipped", "reason": "trigger_reconcile=false_or_no_items"}
    if request.trigger_reconcile and imported_items:
        async with httpx.AsyncClient(timeout=180.0) as http_client:
            try:
                resp = await http_client.post(
                    f"{settings.agent_service_url}/publish-feedback/reconcile",
                    json={
                        "domain_slug": request.domain_slug,
                        "account_id": account_id,
                        "limit": max(20, min(100, len(imported_items) * 4)),
                    },
                )
                resp.raise_for_status()
                reconcile_result = resp.json()
            except httpx.HTTPError as exc:
                reconcile_result = {"status": "error", "reason": str(exc)}

    return {
        "status": "ok",
        "domain_slug": request.domain_slug,
        "account_id": account_id,
        "received": len(items),
        "imported": len(imported_items),
        "created_tasks": created_tasks,
        "updated_tasks": updated_tasks,
        "memory_created": memory_created,
        "matched_summary": dict(match_counter),
        "imported_items": imported_items,
        "skipped_items": skipped_items,
        "reconcile": reconcile_result,
    }


@router.get("/publish-feedback/summary")
def get_publish_feedback_summary(
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    limit: int = 20,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    try:
        domain = _resolve_domain(client, domain_slug)
        safe_limit = max(1, min(limit, 100))
        query = (
            client.table("pipeline_tasks")
            .select("id,account_id,status,stage,payload_jsonb,publish_jsonb,metrics_jsonb,published_at,updated_at")
            .eq("domain_id", domain["id"])
            .in_("status", ["published", "metrics_ready", "done", "publish_failed", "reflecting", "reflection_failed"])
            .order("updated_at", desc=True)
            .limit(safe_limit)
        )
        normalized_account_id = str(account_id or "").strip()
        if normalized_account_id:
            query = query.eq("account_id", normalized_account_id)
        rows = _run_with_db_retry(lambda: query.execute().data or [], fallback=[])

        account_name_map: Dict[str, str] = {}
        account_config_map: Dict[str, Dict[str, Any]] = {}
        account_ids = [str(row.get("account_id") or "").strip() for row in rows if str(row.get("account_id") or "").strip()]
        if normalized_account_id and normalized_account_id not in account_ids:
            account_ids.append(normalized_account_id)
        if account_ids:
            account_rows = _run_with_db_retry(
                lambda: (
                    client.table("channel_accounts")
                    .select("id,account_name,config_jsonb")
                    .in_("id", list(sorted(set(account_ids))))
                    .execute()
                ).data
                or [],
                fallback=[],
            )
            account_name_map = {str(row.get("id") or ""): str(row.get("account_name") or "") for row in account_rows}
            account_config_map = {
                str(row.get("id") or ""): (
                    row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
                )
                for row in account_rows
            }

        items: List[Dict[str, Any]] = []
        for row in rows:
            payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
            publish_jsonb = row.get("publish_jsonb") if isinstance(row.get("publish_jsonb"), dict) else {}
            metrics_jsonb = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
            feedback_analysis = (
                publish_jsonb.get("feedback_analysis")
                if isinstance(publish_jsonb.get("feedback_analysis"), dict)
                else {}
            )
            identity = publish_jsonb.get("identity") if isinstance(publish_jsonb.get("identity"), dict) else {}
            ces = feedback_analysis.get("ces") if isinstance(feedback_analysis.get("ces"), dict) else {}
            attribution = (
                feedback_analysis.get("attribution")
                if isinstance(feedback_analysis.get("attribution"), dict)
                else {}
            )
            ee = feedback_analysis.get("ee") if isinstance(feedback_analysis.get("ee"), dict) else {}
            metric_source = (
                metrics_jsonb.get("post_metrics")
                if isinstance(metrics_jsonb.get("post_metrics"), dict)
                else metrics_jsonb
                if isinstance(metrics_jsonb, dict)
                else {}
            )
            metrics_current = _normalize_imported_metrics(metric_source if isinstance(metric_source, dict) else {})
            snapshot_rows = _normalize_history_snapshot_rows(metrics_jsonb.get("historical_snapshots"))
            metric_change = _compute_metric_change_from_snapshots(snapshot_rows)
            account_key = str(row.get("account_id") or "").strip()
            account_cfg = account_config_map.get(account_key, {})
            account_feedback_plan = (
                account_cfg.get("feedback_plan")
                if isinstance(account_cfg.get("feedback_plan"), dict)
                else {}
            )

            raw_schedule = publish_jsonb.get("feedback_schedule_hours")
            if isinstance(raw_schedule, list):
                schedule_hours = []
                for value in raw_schedule:
                    try:
                        item = int(value)
                    except Exception:  # noqa: BLE001
                        continue
                    if item > 0:
                        schedule_hours.append(item)
                schedule_hours = sorted(set(schedule_hours))
            else:
                fallback_raw = account_feedback_plan.get("checkpoints_hours")
                schedule_hours = []
                if isinstance(fallback_raw, list):
                    for value in fallback_raw:
                        try:
                            item = int(value)
                        except Exception:  # noqa: BLE001
                            continue
                        if item > 0:
                            schedule_hours.append(item)
                if not schedule_hours:
                    schedule_hours = [1, 3, 24]
                schedule_hours = sorted(set(schedule_hours))

            raw_completed = publish_jsonb.get("feedback_completed_hours")
            completed_hours: List[int] = []
            if isinstance(raw_completed, list):
                for value in raw_completed:
                    try:
                        item = int(value)
                    except Exception:  # noqa: BLE001
                        continue
                    if item > 0:
                        completed_hours.append(item)
            completed_hours = sorted(set(completed_hours))

            published_at_value = row.get("published_at") or publish_jsonb.get("published_at")
            published_at_dt = _parse_iso(str(published_at_value) if published_at_value else None)
            now_utc = datetime.now(timezone.utc)
            checkpoints: List[Dict[str, Any]] = []
            if published_at_dt:
                for hour in schedule_hours:
                    due_at = published_at_dt + timedelta(hours=hour)
                    if hour in completed_hours:
                        status_key = "done"
                    elif due_at <= now_utc:
                        status_key = "overdue"
                    else:
                        status_key = "waiting"
                    checkpoints.append(
                        {
                            "hour": hour,
                            "due_at": due_at.isoformat(),
                            "status": status_key,
                            "label": f"发布+{hour}h",
                        }
                    )

            ces_score_value: float | None = None
            if ces:
                ces_score_value = _to_number(ces.get("score"))

            items.append(
                {
                    "task_id": row.get("id"),
                    "account_id": row.get("account_id"),
                    "account_name": account_name_map.get(account_key, "未绑定账号"),
                    "title": str(payload.get("title") or "未命名任务"),
                    "status": str(row.get("status") or ""),
                    "stage": str(row.get("stage") or ""),
                    "feedback_state": str(publish_jsonb.get("feedback_state") or ""),
                    "metrics_mode": str(feedback_analysis.get("metrics_mode") or ""),
                    "next_feedback_at": publish_jsonb.get("next_feedback_at"),
                    "last_feedback_at": publish_jsonb.get("last_feedback_at"),
                    "ces_score": ces_score_value,
                    "primary_bottleneck": str(attribution.get("primary_bottleneck") or ""),
                    "ee_mode": str(ee.get("mode") or ""),
                    "published_url": str(identity.get("published_url") or ""),
                    "feed_id": str(identity.get("feed_id") or ""),
                    "published_at": published_at_value,
                    "updated_at": row.get("updated_at"),
                    "metrics_current": metrics_current,
                    "metric_change": metric_change,
                    "snapshot_count": len(snapshot_rows),
                    "historical_imported": bool(_safe_dict(metrics_jsonb.get("historical_import")).get("enabled")),
                    "schedule_hours": schedule_hours,
                    "completed_hours": completed_hours,
                    "checkpoints": checkpoints,
                }
            )

        return {"status": "ok", "domain_slug": domain_slug, "items": items}
    except Exception:  # noqa: BLE001
        return {"status": "degraded", "domain_slug": domain_slug, "items": []}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


_NO_FALLBACK = object()
_TRANSIENT_DB_TOKENS = (
    "server disconnected",
    "remoteprotocolerror",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "eof",
    "network",
)


def _is_transient_db_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(token in text for token in _TRANSIENT_DB_TOKENS)


def _run_with_db_retry(func, *, retries: int = 2, fallback: Any = _NO_FALLBACK) -> Any:
    last_exc: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            return func()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries and _is_transient_db_error(exc):
                time.sleep(0.15 * (attempt + 1))
                continue
            if fallback is not _NO_FALLBACK:
                return fallback
            raise
    if fallback is not _NO_FALLBACK:
        return fallback
    if last_exc:
        raise last_exc
    raise RuntimeError("db retry exhausted")


def _resolve_domain(client: Client, slug: str) -> Dict[str, Any]:
    try:
        res = _run_with_db_retry(
            lambda: client.table("domains").select("id,slug,name,config_jsonb").eq("slug", slug).limit(1).execute(),
            retries=2,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=user_facing_data_message(exc, default="当前无法读取域配置，请稍后重试。"),
        ) from exc
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="domain not found")
    return res.data[0]


def _safe_basename(path_text: str | None) -> str:
    raw = str(path_text or "").strip()
    if not raw:
        return ""
    try:
        return Path(raw).name or raw
    except Exception:  # noqa: BLE001
        return raw


def _read_env_file_value(key: str) -> str:
    env_path = Path(".env")
    if not env_path.exists():
        return ""
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != key:
                continue
            value = v.strip()
            if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
                value = value[1:-1]
            return value.strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _parse_env_bool(value: str | None, default: bool) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _active_channel_account(client: Client, channel: str) -> Dict[str, Any] | None:
    try:
        rows = (
            client.table("channel_accounts")
            .select(
                "id,channel,account_name,account_handle,login_mode,storage_state_path,user_data_dir,login_username,"
                "login_password,last_login_check_status,last_login_check_at,last_login_check_message,is_active,config_jsonb"
            )
            .eq("channel", channel)
            .eq("is_active", True)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


def _latest_task_row(
    client: Client,
    domain_id: str,
    *,
    published_only: bool = False,
    account_id: str = "",
) -> Dict[str, Any] | None:
    normalized_account_id = str(account_id or "").strip()
    if not pipeline_schema_ready(client):
        return None

    query = (
        client.table("pipeline_tasks")
        .select("id,status,payload_jsonb,published_at,updated_at,created_at")
        .eq("domain_id", domain_id)
        .order("updated_at", desc=True)
        .limit(1)
    )
    if normalized_account_id:
        query = query.eq("account_id", normalized_account_id)
    if published_only:
        query = query.in_("status", ["published", "done"])
    rows = _run_with_db_retry(lambda: query.execute().data or [], fallback=[])
    return rows[0] if rows else None


def _latest_intel_time(client: Client, domain_id: str, source_type: str, account_id: str = "") -> str | None:
    try:
        rows = _load_intelligence_rows(
            client,
            domain_id=domain_id,
            limit=1,
            account_id=account_id,
            select_fields="captured_at,source_type",
        )
        rows = [row for row in rows if str(row.get("source_type") or "") == source_type]
        return rows[0].get("captured_at") if rows else None
    except Exception:  # noqa: BLE001
        return None


def _table_ready(client: Client, table_name: str) -> bool:
    try:
        _run_with_db_retry(lambda: client.table(table_name).select("id").limit(1).execute(), retries=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def _intelligence_account_scope_ready(client: Client) -> bool:
    try:
        _run_with_db_retry(lambda: client.table("intelligence_items").select("account_id").limit(1).execute(), retries=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_intelligence_rows(
    client: Client,
    *,
    domain_id: str,
    limit: int,
    account_id: str = "",
    select_fields: str = "id,account_id,source_type,source_url,captured_at,raw_text,meta_jsonb",
) -> List[Dict[str, Any]]:
    base_query = (
        client.table("intelligence_items")
        .select(select_fields)
        .eq("domain_id", domain_id)
        .order("captured_at", desc=True)
        .limit(limit)
    )
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id or not _intelligence_account_scope_ready(client):
        return _run_with_db_retry(lambda: base_query.execute().data or [], fallback=[])
    try:
        return _run_with_db_retry(
            lambda: base_query.or_(f"account_id.eq.{normalized_account_id},account_id.is.null").execute().data or [],
            fallback=[],
        )
    except Exception:  # noqa: BLE001
        return _run_with_db_retry(lambda: base_query.execute().data or [], fallback=[])


def _default_crawler_template(domain: Dict[str, Any]) -> Dict[str, Any]:
    config = domain.get("config_jsonb") or {}
    hotspot_queries = config.get("hotspot_queries")
    if not isinstance(hotspot_queries, list) or not hotspot_queries:
        hotspot_queries = ["日本移民", "日本经营管理签证", "日本永住", "日本留学"]
    return {
        "enabled": True,
        "timezone": "Asia/Tokyo",
        "daily_schedule": {"hour": 8, "minute": 30},
        "sources": {
            "xiaohongshu_search": {"enabled": True, "max_per_query": 5},
            "google_news_rss": {"enabled": True, "max_per_query": 5},
        },
        "keyword_groups": {
            "hotspot_queries": hotspot_queries,
            "viewpoint_queries": config.get("viewpoint_queries") or [],
            "focus_keywords": config.get("focus_keywords") or [],
        },
        "analysis_template": {
            "steps": [
                "按来源汇总高频主题",
                "筛选和赛道最相关的信号",
                "给出今日选题与标题角度",
                "标注风险边界与禁用承诺",
            ],
            "output_style": "先结论后证据，面向运营决策",
        },
    }


def _normalize_crawler_template(domain: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    defaults = _default_crawler_template(domain)
    result = dict(defaults)
    result.update(payload or {})
    if not isinstance(result.get("sources"), dict):
        result["sources"] = defaults["sources"]
    if not isinstance(result.get("keyword_groups"), dict):
        result["keyword_groups"] = defaults["keyword_groups"]
    if not isinstance(result.get("analysis_template"), dict):
        result["analysis_template"] = defaults["analysis_template"]
    return result


def _default_orchestrator_control() -> Dict[str, Any]:
    return {
        "version": 1,
        "nodes": {
            "collect": {"enabled": True},
            "analyze": {"enabled": True},
            "rebuild": {"enabled": True},
            "publish": {"enabled": True},
            "reflect": {"enabled": True},
        },
        "prompt_overrides": {
            "hot_post_analysis_prompt": "",
            "rebuild_strategy_prompt": "",
            "rebuild_copy_prompt": "",
        },
        "data_input": {
            "mcp_mode": "hotspot",
            "default_query": "日本移民",
            "default_limit": 8,
            "include_detail_metrics": True,
            "publish_method_order": "playwright,mcp",
        },
        "self_upgrade": {
            "suggestions": [],
            "last_suggested_at": None,
            "last_applied_at": None,
            "last_note": "",
        },
        "tool_policies": {
            "default_action": "allow",
            "audiences": {
                "global": {
                    "allow": [],
                    "deny": [],
                },
                "assistant": {
                    "allow": [],
                    "deny": ["approve_and_publish_pending"],
                },
                "coach": {
                    "allow": ["collect_intel", "reconcile_feedback"],
                    "deny": ["approve_and_publish_pending", "run_task", "schedule_posts"],
                },
                "system": {
                    "allow": [],
                    "deny": [],
                },
            },
        },
    }


def _normalize_orchestrator_control(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    defaults = _default_orchestrator_control()
    source = payload if isinstance(payload, dict) else {}
    result = dict(defaults)

    raw_nodes = source.get("nodes") if isinstance(source.get("nodes"), dict) else {}
    nodes = {}
    for name in ["collect", "analyze", "rebuild", "publish", "reflect"]:
        raw_value = raw_nodes.get(name) if isinstance(raw_nodes.get(name), dict) else {}
        nodes[name] = {"enabled": bool(raw_value.get("enabled", True))}
    result["nodes"] = nodes

    raw_prompts = source.get("prompt_overrides") if isinstance(source.get("prompt_overrides"), dict) else {}
    result["prompt_overrides"] = {
        "hot_post_analysis_prompt": str(raw_prompts.get("hot_post_analysis_prompt") or "")[:20000],
        "rebuild_strategy_prompt": str(raw_prompts.get("rebuild_strategy_prompt") or "")[:20000],
        "rebuild_copy_prompt": str(raw_prompts.get("rebuild_copy_prompt") or "")[:20000],
    }

    raw_data_input = source.get("data_input") if isinstance(source.get("data_input"), dict) else {}
    mode = str(raw_data_input.get("mcp_mode") or "hotspot").strip().lower()
    if mode not in {"home", "keyword", "hotspot", "profile"}:
        mode = "hotspot"
    try:
        default_limit = int(raw_data_input.get("default_limit", 8))
    except Exception:  # noqa: BLE001
        default_limit = 8
    default_limit = max(1, min(50, default_limit))
    raw_publish_order = str(raw_data_input.get("publish_method_order") or "playwright,mcp").strip().lower()
    tokens = [token.strip() for token in raw_publish_order.split(",") if token.strip() in {"playwright", "mcp"}]
    if "playwright" not in tokens:
        tokens.append("playwright")
    if "mcp" not in tokens:
        tokens.append("mcp")
    publish_method_order = ",".join(tokens[:2])
    result["data_input"] = {
        "mcp_mode": mode,
        "default_query": str(raw_data_input.get("default_query") or "日本移民")[:300].strip() or "日本移民",
        "default_limit": default_limit,
        "include_detail_metrics": bool(raw_data_input.get("include_detail_metrics", True)),
        "publish_method_order": publish_method_order,
    }

    raw_upgrade = source.get("self_upgrade") if isinstance(source.get("self_upgrade"), dict) else {}
    suggestions = raw_upgrade.get("suggestions")
    if not isinstance(suggestions, list):
        suggestions = []
    result["self_upgrade"] = {
        "suggestions": suggestions[:20],
        "last_suggested_at": raw_upgrade.get("last_suggested_at"),
        "last_applied_at": raw_upgrade.get("last_applied_at"),
        "last_note": str(raw_upgrade.get("last_note") or "")[:2000],
    }

    raw_tool_policies = source.get("tool_policies") if isinstance(source.get("tool_policies"), dict) else {}
    raw_default_action = str(raw_tool_policies.get("default_action") or "allow").strip().lower()
    default_action = raw_default_action if raw_default_action in {"allow", "deny"} else "allow"
    raw_audiences = raw_tool_policies.get("audiences") if isinstance(raw_tool_policies.get("audiences"), dict) else {}
    default_audiences = defaults["tool_policies"]["audiences"] if isinstance(defaults["tool_policies"], dict) else {}

    def _normalize_tool_list(value: Any, fallback: List[str]) -> List[str]:
        if not isinstance(value, list):
            return list(fallback)
        items: List[str] = []
        seen: set[str] = set()
        for raw in value:
            text = str(raw or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(text[:80])
            if len(items) >= 64:
                break
        return items if items else list(fallback)

    normalized_audiences: Dict[str, Dict[str, List[str]]] = {}
    names = set(default_audiences.keys()) | set(raw_audiences.keys())
    for audience in names:
        default_policy = default_audiences.get(audience) if isinstance(default_audiences.get(audience), dict) else {}
        raw_policy = raw_audiences.get(audience) if isinstance(raw_audiences.get(audience), dict) else {}
        normalized_audiences[str(audience)] = {
            "allow": _normalize_tool_list(raw_policy.get("allow"), default_policy.get("allow") if isinstance(default_policy.get("allow"), list) else []),
            "deny": _normalize_tool_list(raw_policy.get("deny"), default_policy.get("deny") if isinstance(default_policy.get("deny"), list) else []),
        }
    result["tool_policies"] = {
        "default_action": default_action,
        "audiences": normalized_audiences,
    }
    return result


def _build_orchestrator_self_upgrade(
    client: Client,
    *,
    domain_id: str,
    control: Dict[str, Any],
) -> Dict[str, Any]:
    rows = (
        client.table("intelligence_items")
        .select("source_type,raw_text,meta_jsonb")
        .eq("domain_id", domain_id)
        .order("captured_at", desc=True)
        .limit(80)
        .execute()
    ).data or []

    xhs_rows = [row for row in rows if "xiaohongshu" in str(row.get("source_type") or "").lower()]
    total = len(xhs_rows)
    with_body = 0
    with_metrics = 0
    for row in xhs_rows:
        raw_text = str(row.get("raw_text") or "").strip()
        meta = row.get("meta_jsonb") if isinstance(row.get("meta_jsonb"), dict) else {}
        detail_text = str(meta.get("post_detail_text") or "").strip()
        if len(detail_text) >= 20 or len(raw_text) >= 20:
            with_body += 1
        if isinstance(meta.get("post_metrics"), dict):
            with_metrics += 1

    coverage_body = (with_body / total) if total else 0.0
    coverage_metrics = (with_metrics / total) if total else 0.0
    data_input = control.get("data_input") if isinstance(control.get("data_input"), dict) else {}
    prompt_overrides = control.get("prompt_overrides") if isinstance(control.get("prompt_overrides"), dict) else {}

    suggestions: List[Dict[str, Any]] = []
    patch: Dict[str, Any] = {}

    if total < 10:
        next_limit = max(12, min(30, int(data_input.get("default_limit", 8)) + 4))
        suggestions.append(
            {
                "id": "raise_sampling_limit",
                "reason": f"最近小红书样本仅 {total} 条，信息不足以稳定判断。",
                "change": {"data_input.default_limit": next_limit},
            }
        )
        patch["default_limit"] = next_limit

    if coverage_metrics < 0.5 and not bool(data_input.get("include_detail_metrics", True)):
        suggestions.append(
            {
                "id": "enable_detail_metrics",
                "reason": f"指标覆盖仅 {coverage_metrics:.0%}，建议开启详情指标补齐。",
                "change": {"data_input.include_detail_metrics": True},
            }
        )
        patch["include_detail_metrics"] = True

    if coverage_body < 0.7:
        suggestions.append(
            {
                "id": "prefer_hotspot_mode",
                "reason": f"正文覆盖仅 {coverage_body:.0%}，建议默认使用“热点采集（首页+关键词）”。",
                "change": {"data_input.mcp_mode": "hotspot"},
            }
        )
        patch["mcp_mode"] = "hotspot"

    if not str(prompt_overrides.get("rebuild_strategy_prompt") or "").strip():
        suggestions.append(
            {
                "id": "set_strategy_prompt",
                "reason": "策略重构提示词为空，建议配置固定策略提示词，减少漂移。",
                "change": {"prompt_overrides.rebuild_strategy_prompt": "建议配置"},
            }
        )
    if not str(prompt_overrides.get("rebuild_copy_prompt") or "").strip():
        suggestions.append(
            {
                "id": "set_copy_prompt",
                "reason": "正文改写提示词为空，建议配置固定文案提示词，稳定风格。",
                "change": {"prompt_overrides.rebuild_copy_prompt": "建议配置"},
            }
        )

    return {
        "sample_summary": {
            "xhs_sample_count": total,
            "body_coverage_ratio": round(coverage_body, 3),
            "metrics_coverage_ratio": round(coverage_metrics, 3),
        },
        "suggestions": suggestions,
        "patch": patch,
    }


def _extract_title_from_intel(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""
    first_line = text.split("\n", 1)[0].strip()
    return first_line[:120]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:  # noqa: BLE001
        return default


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _load_competitor_dataset(
    client: Client,
    *,
    domain_id: str,
    limit: int,
    account_id: str = "",
) -> List[Dict[str, Any]]:
    rows = _load_intelligence_rows(
        client,
        domain_id=domain_id,
        limit=limit,
        account_id=account_id,
        select_fields="id,source_type,source_url,captured_at,raw_text,meta_jsonb",
    )

    scored: List[Dict[str, Any]] = []
    for row in rows:
        source_type = str(row.get("source_type") or "")
        if "hotspot" not in source_type.lower() and "viewpoint" not in source_type.lower():
            continue
        raw_text = str(row.get("raw_text") or "")
        meta = row.get("meta_jsonb") if isinstance(row.get("meta_jsonb"), dict) else {}
        metrics = meta.get("post_metrics") if isinstance(meta.get("post_metrics"), dict) else {}
        likes = _to_int(metrics.get("likes"))
        collects = _to_int(metrics.get("collects"))
        comments = _to_int(metrics.get("comments")) + _to_int(metrics.get("comments_count"))
        shares = _to_int(metrics.get("shares"))
        score = likes * 0.8 + collects * 1.4 + comments * 1.0 + shares * 1.1
        scored.append(
            {
                "id": row.get("id"),
                "title": _extract_title_from_intel(raw_text),
                "body": raw_text[:4000],
                "source_type": source_type,
                "source_url": str(row.get("source_url") or ""),
                "captured_at": row.get("captured_at"),
                "engagement_score": round(score, 2),
                "metrics": metrics,
                "features": {
                    "has_number_title": bool(re.search(r"\d", raw_text[:80])),
                    "has_policy_words": bool(re.search(r"(政策|签证|门槛|条件|费用|材料|流程|避坑)", raw_text[:200])),
                    "has_emotion_words": bool(re.search(r"(后悔|避雷|崩溃|救命|一定|千万)", raw_text[:200])),
                },
            }
        )

    scored.sort(key=lambda item: (item.get("engagement_score", 0), str(item.get("captured_at") or "")), reverse=True)
    return scored[:limit]


def _load_our_history_dataset(
    client: Client,
    *,
    domain_id: str,
    limit: int,
    account_id: str = "",
) -> List[Dict[str, Any]]:
    if not pipeline_schema_ready(client):
        return []

    query = (
        client.table("pipeline_tasks")
        .select("id,status,channel,published_at,payload_jsonb,metrics_jsonb")
        .eq("domain_id", domain_id)
        .in_("status", ["published", "done"])
        .order("published_at", desc=True)
        .limit(limit)
    )
    if account_id:
        query = query.eq("account_id", account_id)
    rows = query.execute().data or []
    dataset: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        metrics = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
        views = _to_int(metrics.get("views"))
        likes = _to_int(metrics.get("likes"))
        collects = _to_int(metrics.get("collects"))
        comments_count = _to_int(metrics.get("comments")) + _to_int(metrics.get("comments_count"))
        follows = _to_int(metrics.get("follows"))
        leads = _to_int(metrics.get("leads_generated"))
        engagement_rate = _safe_ratio(likes + collects + comments_count, max(views, 1))
        follow_rate = _safe_ratio(follows, max(views, 1))
        dataset.append(
            {
                "id": row.get("id"),
                "title": str(payload.get("title") or ""),
                "channel": row.get("channel"),
                "published_at": row.get("published_at"),
                "views": views,
                "likes": likes,
                "collects": collects,
                "comments_count": comments_count,
                "follows": follows,
                "leads_generated": leads,
                "engagement_rate": engagement_rate,
                "follow_rate": follow_rate,
            }
        )
    return dataset


def _load_current_system_version(
    client: Client,
    *,
    domain: Dict[str, Any],
    account_id: str = "",
) -> Dict[str, Any]:
    config = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
    control = _normalize_orchestrator_control(config.get("orchestrator_control") if isinstance(config.get("orchestrator_control"), dict) else {})
    strategy_version_id = domain.get("active_strategy_version_id")
    strategy_prompt = {}
    if strategy_version_id:
        row = (
            client.table("strategy_versions")
            .select("id,version,prompt_jsonb,created_at")
            .eq("id", strategy_version_id)
            .limit(1)
            .execute()
        ).data or []
        if row:
            strategy_prompt = row[0]

    prompt_versions = config.get("orchestrator_prompt_versions")
    if not isinstance(prompt_versions, list):
        prompt_versions = []
    control_versions = config.get("orchestrator_control_versions")
    if not isinstance(control_versions, list):
        control_versions = []

    account_prompt_versions: List[Dict[str, Any]] = []
    if account_id:
        account_prompt_versions = (
            client.table("prompt_versions")
            .select("id,agent_name,version,status,reason,updated_at,created_at")
            .eq("account_id", account_id)
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(20)
            .execute()
        ).data or []

    return {
        "active_strategy_version_id": strategy_version_id,
        "strategy_prompt_version": strategy_prompt,
        "agent2_prompt": control.get("prompt_overrides", {}).get("rebuild_strategy_prompt", ""),
        "agent3_prompt": control.get("prompt_overrides", {}).get("rebuild_copy_prompt", ""),
        "account_id": account_id or None,
        "account_prompt_versions": account_prompt_versions,
        "prompt_version_history": prompt_versions[-20:],
        "orchestrator_control_active_version": config.get("orchestrator_control_active_version"),
        "orchestrator_control_history": control_versions[-20:],
        "data_input": control.get("data_input", {}),
    }


def _next_account_prompt_version(agent_rows: List[Dict[str, Any]]) -> str:
    if not agent_rows:
        return "v1.0"
    latest = str(agent_rows[0].get("version") or "").strip()
    major, minor = _parse_version_value(latest)
    return f"v{major}.{minor + 1}"


def _history_driven_data_input_patch(
    *,
    our_history: List[Dict[str, Any]],
    control: Dict[str, Any],
) -> Dict[str, Any]:
    if not our_history:
        return {}
    data_input = control.get("data_input") if isinstance(control.get("data_input"), dict) else {}
    current_mode = str(data_input.get("mcp_mode") or "hotspot").strip().lower()
    current_limit = int(data_input.get("default_limit") or 8)
    include_detail = bool(data_input.get("include_detail_metrics", True))

    rows = our_history[:20]
    avg_views = _safe_ratio(sum(_to_int(item.get("views")) for item in rows), max(len(rows), 1))
    avg_engagement = _safe_ratio(sum(_to_float(item.get("engagement_rate")) for item in rows), max(len(rows), 1))
    total_leads = sum(_to_int(item.get("leads_generated")) for item in rows)

    patch: Dict[str, Any] = {}
    if total_leads <= 0 and current_mode in {"home", "keyword"}:
        patch["mcp_mode"] = "hotspot"
    if avg_views < 300 and current_limit < 12:
        patch["default_limit"] = 12
    if avg_engagement < 0.03 and current_limit < 16:
        patch["default_limit"] = max(patch.get("default_limit", current_limit), 16)
    if not include_detail:
        patch["include_detail_metrics"] = True
    return patch


def _parse_version_value(value: str) -> tuple[int, int]:
    match = re.match(r"^v(\d+)\.(\d+)$", str(value or "").strip())
    if not match:
        return (1, 0)
    return (int(match.group(1)), int(match.group(2)))


def _next_prompt_version(version_rows: List[Dict[str, Any]]) -> str:
    if not version_rows:
        return "v1.0"
    latest = str(version_rows[-1].get("version") or "v1.0")
    major, minor = _parse_version_value(latest)
    return f"v{major}.{minor + 1}"


def _parse_control_version_value(value: str) -> tuple[int, int]:
    match = re.match(r"^c(\d+)\.(\d+)$", str(value or "").strip())
    if not match:
        return (1, 0)
    return (int(match.group(1)), int(match.group(2)))


def _next_control_version(version_rows: List[Dict[str, Any]]) -> str:
    if not version_rows:
        return "c1.0"
    latest = str(version_rows[-1].get("version") or "c1.0")
    major, minor = _parse_control_version_value(latest)
    return f"c{major}.{minor + 1}"


def _append_orchestrator_control_version(
    *,
    config: Dict[str, Any],
    control: Dict[str, Any],
    reason: str,
    actor: str,
) -> str:
    versions = config.get("orchestrator_control_versions")
    if not isinstance(versions, list):
        versions = []
    version = _next_control_version(versions)
    versions.append(
        {
            "version": version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason[:2000],
            "actor": actor[:120],
            "control_jsonb": control,
        }
    )
    config["orchestrator_control_versions"] = versions[-60:]
    config["orchestrator_control_active_version"] = version
    return version


def _upsert_pending_memory_item(
    client: Client,
    *,
    domain_id: str,
    account_id: str,
    title: str,
    content: str,
    tags: List[str],
    confidence: float = 0.72,
    created_by: str = "system:chief_evolution",
) -> None:
    if not account_id or not _table_ready(client, "memory_items"):
        return
    normalized_tags = [str(tag).strip()[:80] for tag in tags if str(tag).strip()][:20]
    existing = (
        client.table("memory_items")
        .select("id")
        .eq("domain_id", domain_id)
        .eq("account_id", account_id)
        .eq("title", title[:200])
        .eq("status", "pending")
        .limit(1)
        .execute()
    ).data or []
    payload = {
        "content": content[:4000],
        "tags": normalized_tags,
        "confidence": max(0.0, min(1.0, confidence)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        client.table("memory_items").update(payload).eq("id", existing[0]["id"]).execute()
        return
    client.table("memory_items").insert(
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "source_run_id": None,
            "type": "strategy_rule",
            "title": title[:200],
            "content": content[:4000],
            "tags": normalized_tags,
            "status": "pending",
            "confidence": max(0.0, min(1.0, confidence)),
            "created_by": created_by[:120],
        }
    ).execute()


def _tokens(text: str) -> List[str]:
    cleaned = URL_RE.sub(" ", text or "")
    cleaned = HTML_ENTITY_RE.sub(" ", cleaned)
    tokens: List[str] = []
    for token in TOKEN_RE.findall(cleaned):
        lower = token.lower()
        if not lower:
            continue
        if lower in STOPWORDS or lower in WEB_NOISE_TOKENS:
            continue
        if HEX_TOKEN_RE.match(token):
            continue
        if re.fullmatch(r"[A-Za-z0-9]+", token):
            has_digit = any(ch.isdigit() for ch in token)
            if not has_digit and lower not in KEEP_LATIN_TOKENS:
                continue
            if len(token) > 10:
                continue
        tokens.append(token)
    return tokens


def _basic_intel_analysis(
    rows: List[Dict[str, Any]],
    *,
    custom_logic: str = "",
) -> Dict[str, Any]:
    source_counter: Counter[str] = Counter()
    query_counter: Counter[str] = Counter()
    token_counter: Counter[str] = Counter()
    sample_lines: List[str] = []

    for row in rows:
        source = str(row.get("source_type") or "unknown")
        source_counter[source] += 1
        meta = row.get("meta_jsonb") if isinstance(row.get("meta_jsonb"), dict) else {}
        query = str(meta.get("query") or "").strip()
        if query:
            query_counter[query] += 1
        raw_text = str(row.get("raw_text") or "")
        token_counter.update(_tokens(raw_text))
        if raw_text and len(sample_lines) < 8:
            sample_lines.append(raw_text[:120])

    top_keywords = [{"keyword": k, "count": v} for k, v in token_counter.most_common(12)]
    top_queries = [{"query": k, "count": v} for k, v in query_counter.most_common(8)]
    source_distribution = [{"source": k, "count": v} for k, v in source_counter.most_common()]

    recommendation_lines: List[str] = []
    if top_keywords:
        top3 = "、".join([item["keyword"] for item in top_keywords[:3]])
        recommendation_lines.append(f"今日优先围绕关键词：{top3} 组织内容。")
    if top_queries:
        top_query = top_queries[0]["query"]
        recommendation_lines.append(f"优先选题方向：{top_query}。")
    if not recommendation_lines:
        recommendation_lines.append("今日信号较弱，建议先做观点型内容并减少发布条数。")

    if custom_logic.strip():
        recommendation_lines.append(f"用户补充逻辑：{custom_logic.strip()}")

    return {
        "summary": {
            "total_items": len(rows),
            "source_count": len(source_distribution),
            "top_keyword": top_keywords[0]["keyword"] if top_keywords else "",
        },
        "source_distribution": source_distribution,
        "top_queries": top_queries,
        "top_keywords": top_keywords,
        "samples": sample_lines,
        "recommendations": recommendation_lines,
    }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _source_bucket(source_type: str) -> str:
    text = (source_type or "").strip().lower()
    if "xiaohongshu" in text or "xhs" in text:
        return "xhs_hotspot"
    if "google" in text:
        return "google_news"
    if "viewpoint" in text:
        return "viewpoint"
    return "other"


def _extract_official_keywords(domain: Dict[str, Any]) -> List[str]:
    config = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
    template = config.get("crawler_template") if isinstance(config.get("crawler_template"), dict) else {}
    groups = template.get("keyword_groups") if isinstance(template.get("keyword_groups"), dict) else {}

    candidates = [
        config.get("official_keywords"),
        groups.get("official_keywords"),
        groups.get("focus_keywords"),
        config.get("focus_keywords"),
    ]
    merged: List[str] = []
    for row in candidates:
        if not isinstance(row, list):
            continue
        for item in row:
            text = str(item).strip()
            if not text or len(text) < 2:
                continue
            if text not in merged:
                merged.append(text)
            if len(merged) >= 60:
                return merged
    return merged


def _load_intel_rows(client: Client, domain_id: str, limit: int, account_id: str = "") -> List[Dict[str, Any]]:
    return _load_intelligence_rows(
        client,
        domain_id=domain_id,
        limit=limit,
        account_id=account_id,
        select_fields="id,source_type,source_url,captured_at,raw_text,meta_jsonb",
    )


def _collect_hotspot_terms(rows: List[Dict[str, Any]]) -> tuple[Counter[str], Counter[str], List[str]]:
    query_counter: Counter[str] = Counter()
    keyword_counter: Counter[str] = Counter()
    source_paths: List[str] = []

    for row in rows:
        source_type = str(row.get("source_type") or "")
        bucket = _source_bucket(source_type)
        if bucket != "xhs_hotspot":
            continue
        meta = row.get("meta_jsonb") if isinstance(row.get("meta_jsonb"), dict) else {}
        query = str(meta.get("query") or "").strip()
        if query:
            query_counter[query] += 1
        raw_text = str(row.get("raw_text") or "")
        keyword_counter.update(_tokens(raw_text))
        source_url = str(row.get("source_url") or "").strip()
        source_id = str(row.get("id") or "").strip()
        if source_url:
            source_paths.append(f"intelligence_items/{source_id}:{source_url}")
    return query_counter, keyword_counter, source_paths


def _metric_score(metrics_jsonb: Dict[str, Any] | None, leads_count: float = 0.0) -> float:
    metrics = metrics_jsonb or {}
    likes = _to_float(metrics.get("likes"))
    collects = _to_float(metrics.get("collects"))
    comments = _to_float(metrics.get("comments")) + _to_float(metrics.get("comments_count"))
    shares = _to_float(metrics.get("shares"))
    leads = _to_float(metrics.get("leads_generated"), default=leads_count) + leads_count
    score = likes * 0.8 + collects * 1.4 + comments * 1.0 + shares * 1.2 + leads * 3.0
    return max(0.0, min(120.0, score))


def _history_keyword_scores(client: Client, domain_id: str, limit: int = 80) -> tuple[Counter[str], List[str]]:
    keyword_counter: Counter[str] = Counter()
    sample_titles: List[str] = []

    if not pipeline_schema_ready(client):
        return keyword_counter, sample_titles

    rows = (
        client.table("pipeline_tasks")
        .select("id,status,channel,published_at,payload_jsonb,metrics_jsonb")
        .eq("domain_id", domain_id)
        .in_("status", ["published", "done"])
        .order("published_at", desc=True)
        .limit(limit)
        .execute()
    ).data or []

    for row in rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if title and len(sample_titles) < 6:
            sample_titles.append(title[:120])
        score = _metric_score(row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {})
        weight = 1.0 + score / 40.0
        for token in _tokens(title):
            keyword_counter[token] += weight * 1.5
        for token in _tokens(body[:220]):
            keyword_counter[token] += weight * 0.8
    return keyword_counter, sample_titles


def _infer_track_from_keywords(keywords: List[str]) -> str:
    text = " ".join(keywords).lower()
    if "经营" in text or "签证" in text:
        return "business_manager_visa"
    if "永住" in text or "归化" in text:
        return "permanent_residency"
    if "留学" in text or "语言学校" in text:
        return "study_abroad"
    if "税" in text or "资产" in text or "财富" in text:
        return "asset_tax_planning"
    return "business_manager_visa"


def _infer_audience_tag(keywords: List[str]) -> str:
    text = " ".join(keywords).lower()
    if "家长" in text or "孩子" in text:
        return "japan_family"
    if "留学" in text or "学校" in text:
        return "japan_student"
    if "经营" in text or "创业" in text:
        return "japan_biz"
    if "资产" in text or "税" in text:
        return "japan_asset"
    return "japan_core"


def _build_topic_from_signals(top_queries: List[str], top_keywords: List[str]) -> str:
    if top_queries:
        return f"{top_queries[0]}：2026 最新变化与实操避坑"
    head = " / ".join(top_keywords[:2]) if top_keywords else "日本移民"
    return f"{head}：今天该怎么准备才更稳"


def _load_tasks(client: Client, *, domain_id: str, limit: int) -> List[Dict[str, Any]]:
    if not pipeline_schema_ready(client):
        return []

    rows = _run_with_db_retry(
        lambda: (
            client.table("pipeline_tasks")
            .select(
                "id,channel,status,stage,intent_jsonb,payload_jsonb,review_jsonb,publish_jsonb,metrics_jsonb,scheduled_at,created_at,updated_at,published_at"
            )
            .eq("domain_id", domain_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        ),
        fallback=None,
    )
    if rows is None:
        return []
    return rows.data or []


def _normalize_topic_key(text: str) -> str:
    clean = re.sub(r"\s+", " ", str(text or "").strip().lower())
    clean = re.sub(r"[`'\"“”‘’]", "", clean)
    return clean[:220]


def _topic_from_task_row(row: Dict[str, Any]) -> str:
    intent = row.get("intent_jsonb") if isinstance(row.get("intent_jsonb"), dict) else {}
    payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
    topic = str(intent.get("topic") or "").strip()
    if topic:
        return topic
    title = str(payload.get("title") or "").strip()
    if title:
        return title
    body = str(payload.get("body") or "").strip()
    return body[:80]


def _schedule_bucket(raw_value: str | None) -> str:
    dt = _parse_iso(raw_value)
    if not dt:
        return "unscheduled"
    minute = (dt.minute // 20) * 20
    snapped = dt.replace(minute=minute, second=0, microsecond=0)
    return snapped.isoformat()


def _status_priority_for_dedupe(status_value: str) -> int:
    priority = {
        "approved": 7,
        "pending_review": 6,
        "review_rejected": 5,
        "drafting": 4,
        "intel_ready": 3,
        "queued": 2,
    }
    return priority.get(status_value, 1)


def _active_dedupe_statuses(include_review: bool) -> set[str]:
    base = {"queued", "intel_ready", "drafting", "review_rejected", "approved"}
    if include_review:
        base.add("pending_review")
    return base


def _channel_stats(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    stats: Dict[str, Dict[str, int]] = {}
    for row in rows:
        channel = str(row.get("channel") or "unknown")
        status = str(row.get("status") or "")
        node = stats.setdefault(
            channel,
            {
                "total": 0,
                "pending_review": 0,
                "published": 0,
                "done": 0,
                "failed": 0,
                "published_7d": 0,
            },
        )
        node["total"] += 1
        if status == "pending_review":
            node["pending_review"] += 1
        if status == "published":
            node["published"] += 1
        if status == "done":
            node["done"] += 1
        if status in {"publish_failed", "reflection_failed"}:
            node["failed"] += 1

        published_at = _parse_iso(str(row.get("published_at") or ""))
        if published_at and published_at >= seven_days_ago:
            node["published_7d"] += 1
    return stats


@router.post("/cleanup-duplicate-tasks")
def cleanup_duplicate_tasks(
    domain_slug: str = "japan_immigration",
    limit: int = 500,
    include_review: bool = True,
    dry_run: bool = False,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, domain_slug)
    safe_limit = max(50, min(limit, 1200))
    rows = _load_tasks(client, domain_id=domain["id"], limit=safe_limit)
    active_statuses = _active_dedupe_statuses(include_review)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    scanned = 0
    for row in rows:
        status_value = str(row.get("status") or "").strip().lower()
        if status_value not in active_statuses:
            continue
        topic = _topic_from_task_row(row)
        topic_key = _normalize_topic_key(topic)
        if not topic_key:
            continue
        channel = str(row.get("channel") or "unknown")
        bucket = _schedule_bucket(str(row.get("scheduled_at") or ""))
        key = f"{channel}|{bucket}|{topic_key}"
        groups.setdefault(key, []).append(row)
        scanned += 1

    kept_ids: List[str] = []
    deleted_ids: List[str] = []
    duplicate_groups: List[Dict[str, Any]] = []
    pipeline_mode = pipeline_schema_ready(client)

    for key, bucket_rows in groups.items():
        if len(bucket_rows) <= 1:
            continue
        ranked = sorted(
            bucket_rows,
            key=lambda row: (
                _status_priority_for_dedupe(str(row.get("status") or "").strip().lower()),
                _parse_iso(str(row.get("updated_at") or "")) or datetime.fromtimestamp(0, tz=timezone.utc),
                _parse_iso(str(row.get("created_at") or "")) or datetime.fromtimestamp(0, tz=timezone.utc),
            ),
            reverse=True,
        )
        keeper = ranked[0]
        keeper_id = str(keeper.get("id") or "")
        if keeper_id:
            kept_ids.append(keeper_id)
        drops = ranked[1:]
        if not drops:
            continue

        drop_ids = [str(item.get("id") or "") for item in drops if str(item.get("id") or "")]
        duplicate_groups.append(
            {
                "key": key,
                "keep_id": keeper_id,
                "drop_ids": drop_ids,
            }
        )

        if dry_run:
            deleted_ids.extend(drop_ids)
            continue

        for drop_id in drop_ids:
            try:
                client.table("pipeline_tasks").delete().eq("id", drop_id).execute()
                deleted_ids.append(drop_id)
            except Exception:  # noqa: BLE001
                continue

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="ops.cleanup_duplicate_tasks",
            target_type="domain",
            target_id=str(domain["id"]),
            diff_jsonb={
                "domain_slug": domain_slug,
                "limit": safe_limit,
                "include_review": include_review,
                "dry_run": dry_run,
                "scanned": scanned,
                "duplicate_groups": len(duplicate_groups),
                "deleted": len(deleted_ids),
            },
        ),
    )

    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "pipeline_mode": "native" if pipeline_mode else "schema_missing",
        "dry_run": dry_run,
        "scanned": scanned,
        "duplicate_groups": len(duplicate_groups),
        "deleted": len(deleted_ids),
        "kept_ids": kept_ids[:20],
        "deleted_ids": deleted_ids[:120],
        "groups": duplicate_groups[:40],
    }


@router.get("/overview")
def get_ops_overview(
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    limit: int = 20,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    normalized_limit = max(5, min(limit, 100))
    normalized_account_id = str(account_id or "").strip()
    try:
        domain = _resolve_domain(client, domain_slug)
        tasks = _load_tasks(client, domain_id=domain["id"], limit=max(60, normalized_limit * 3))
        if normalized_account_id:
            filtered_tasks: List[Dict[str, Any]] = []
            for row in tasks:
                payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
                task_account_id = str(payload.get("channel_account_id") or "").strip()
                if task_account_id == normalized_account_id:
                    filtered_tasks.append(row)
            tasks = filtered_tasks
        channel_stats = _channel_stats(tasks)

        references = _load_intelligence_rows(
            client,
            domain_id=str(domain["id"]),
            limit=normalized_limit,
            account_id=normalized_account_id,
        )

        reports = _run_with_db_retry(
            lambda: (
                client.table("daily_ops_reports")
                .select("id,run_key,status,attempt,started_at,finished_at,error_message,result_jsonb")
                .eq("domain_slug", domain_slug)
                .order("started_at", desc=True)
                .limit(10)
                .execute()
            ).data
            or [],
            fallback=[],
        )

        latest_tasks: List[Dict[str, Any]] = []
        for row in tasks[:normalized_limit]:
            payload = row.get("payload_jsonb") or {}
            intent = row.get("intent_jsonb") or {}
            analysis_jsonb = payload.get("analysis_jsonb") if isinstance(payload.get("analysis_jsonb"), dict) else {}
            latest_tasks.append(
                {
                    "id": row.get("id"),
                    "channel": row.get("channel"),
                    "status": row.get("status"),
                    "stage": row.get("stage"),
                    "created_at": row.get("created_at"),
                    "published_at": row.get("published_at"),
                    "topic": intent.get("topic"),
                    "title": payload.get("title"),
                    "body": payload.get("body"),
                    "image_prompt": payload.get("image_prompt"),
                    "analysis_jsonb": analysis_jsonb,
                }
            )

        config = domain.get("config_jsonb") or {}
        analysis_logic = [
            "采集参考内容（热点/观点）",
            "结合账号历史表现提取优化线索",
            "生成标题、正文、图片提示词",
            "人工反馈与AI分析合并后再执行发布",
            "发布后回收数据并触发反思升级",
        ]
        automation_policy = config.get("automation_policy") if isinstance(config.get("automation_policy"), dict) else {}

        return {
            "status": "ok",
            "domain": {
                "id": domain["id"],
                "slug": domain["slug"],
                "name": domain["name"],
            },
            "account_id": normalized_account_id or None,
            "channel_stats": channel_stats,
            "reference_items": references,
            "latest_tasks": latest_tasks,
            "latest_reports": reports,
            "analysis_logic": analysis_logic,
            "strategy_snapshot": {
                "focus_keywords": config.get("focus_keywords") or [],
                "hotspot_queries": config.get("hotspot_queries") or [],
                "automation_policy": automation_policy,
            },
        }
    except HTTPException as exc:
        if int(exc.status_code) == 404:
            raise
        return {
            "status": "degraded",
            "domain": {"id": "", "slug": domain_slug, "name": domain_slug},
            "account_id": normalized_account_id or None,
            "channel_stats": {},
            "reference_items": [],
            "latest_tasks": [],
            "latest_reports": [],
            "analysis_logic": [],
            "strategy_snapshot": {"focus_keywords": [], "hotspot_queries": [], "automation_policy": {}},
            "error": user_facing_data_message(str(exc.detail)),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "degraded",
            "domain": {"id": "", "slug": domain_slug, "name": domain_slug},
            "account_id": normalized_account_id or None,
            "channel_stats": {},
            "reference_items": [],
            "latest_tasks": [],
            "latest_reports": [],
            "analysis_logic": [],
            "strategy_snapshot": {"focus_keywords": [], "hotspot_queries": [], "automation_policy": {}},
            "error": user_facing_data_message(exc),
        }


@router.get("/runtime-context")
def get_runtime_context(
    domain_slug: str = "japan_immigration",
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    try:
        domain = _resolve_domain(client, domain_slug)
        config = domain.get("config_jsonb") or {}
        template_raw = config.get("crawler_template") if isinstance(config.get("crawler_template"), dict) else {}
        template = _normalize_crawler_template(domain, template_raw)
        sources = template.get("sources") if isinstance(template.get("sources"), dict) else {}
        xhs_source = sources.get("xiaohongshu_search") if isinstance(sources.get("xiaohongshu_search"), dict) else {}
        google_source = sources.get("google_news_rss") if isinstance(sources.get("google_news_rss"), dict) else {}

        publish_account = _active_channel_account(client, "xiaohongshu")
        auth_hint = ""
        strategy_profile: Dict[str, Any] = {}
        if publish_account:
            login_mode = str(publish_account.get("login_mode") or "")
            if login_mode == "credential":
                username = str(publish_account.get("login_username") or "").strip()
                has_password = bool(str(publish_account.get("login_password") or "").strip())
                auth_hint = f"账号密码（{username or '未填用户名'} / {'已配置密码' if has_password else '未配置密码'}）"
            else:
                auth_hint = _safe_basename(publish_account.get("storage_state_path") or publish_account.get("user_data_dir"))
            config_jsonb = publish_account.get("config_jsonb") if isinstance(publish_account.get("config_jsonb"), dict) else {}
            raw_strategy = config_jsonb.get("strategy_profile") if isinstance(config_jsonb.get("strategy_profile"), dict) else {}
            strategy_profile = {
                "persona_name": str(raw_strategy.get("persona_name") or "").strip(),
                "ip_positioning": str(raw_strategy.get("ip_positioning") or "").strip(),
                "tone_style": str(raw_strategy.get("tone_style") or "").strip(),
                "primary_goal": str(raw_strategy.get("primary_goal") or "").strip(),
                "mcp_mode": str(raw_strategy.get("mcp_mode") or "").strip(),
                "mcp_query_default": str(raw_strategy.get("mcp_query_default") or "").strip(),
                "mcp_limit_default": raw_strategy.get("mcp_limit_default"),
                "mcp_profile_tool": str(raw_strategy.get("mcp_profile_tool") or "").strip(),
                "mcp_profile_id_key": str(raw_strategy.get("mcp_profile_id_key") or "").strip(),
                "has_mcp_extra_args": isinstance(raw_strategy.get("mcp_extra_args"), dict),
            }
        publish_account_view = {
            "configured": bool(publish_account),
            "account_id": publish_account.get("id") if publish_account else None,
            "account_name": publish_account.get("account_name") if publish_account else None,
            "account_handle": publish_account.get("account_handle") if publish_account else None,
            "login_mode": publish_account.get("login_mode") if publish_account else None,
            "auth_hint": auth_hint,
            "strategy_profile": strategy_profile,
            "last_login_check_status": publish_account.get("last_login_check_status") if publish_account else None,
            "last_login_check_at": publish_account.get("last_login_check_at") if publish_account else None,
            "last_login_check_message": publish_account.get("last_login_check_message") if publish_account else None,
        }
        current_account_id = str(publish_account.get("id") or "") if publish_account else ""

        env_storage = str(os.getenv("PLAYWRIGHT_STORAGE_STATE_PATH", "")).strip() or _read_env_file_value(
            "PLAYWRIGHT_STORAGE_STATE_PATH"
        )
        env_user_data = str(os.getenv("PLAYWRIGHT_USER_DATA_DIR", "")).strip() or _read_env_file_value(
            "PLAYWRIGHT_USER_DATA_DIR"
        )
        env_cookies = str(os.getenv("PLAYWRIGHT_SESSION_COOKIES_JSON", "")).strip() or _read_env_file_value(
            "PLAYWRIGHT_SESSION_COOKIES_JSON"
        )
        env_dry_run_raw = str(os.getenv("PLAYWRIGHT_DRY_RUN", "")).strip() or _read_env_file_value("PLAYWRIGHT_DRY_RUN")
        env_dry_run = _parse_env_bool(env_dry_run_raw or "true", True)

        crawler_source = "missing"
        crawler_auth_hint = ""
        if env_storage:
            crawler_source = "env_storage_state"
            crawler_auth_hint = _safe_basename(env_storage)
        elif env_user_data:
            crawler_source = "env_user_data_dir"
            crawler_auth_hint = _safe_basename(env_user_data)
        elif env_cookies:
            crawler_source = "env_cookies_json"
            crawler_auth_hint = "已配置 cookies"

        latest_draft_task = _latest_task_row(client, domain["id"], published_only=False, account_id=current_account_id)
        latest_published_task = _latest_task_row(client, domain["id"], published_only=True, account_id=current_account_id)

        publish_blockers: List[str] = []
        if env_dry_run:
            publish_blockers.append("当前处于 Demo 模式：PLAYWRIGHT_DRY_RUN=true")
        if not publish_account:
            publish_blockers.append("未配置可用发布账号")
        if publish_account and str(publish_account.get("last_login_check_status") or "") not in {"ok", "warning"}:
            publish_blockers.append("发布账号登录态未校验通过")
        if not env_storage and not env_user_data and not env_cookies:
            publish_blockers.append("采集侧未配置登录凭证（Storage/UserData/Cookies）")

        ready_for_real_publish = len(publish_blockers) == 0

        latest_task_account_name = ""
        latest_task_account_id = ""
        payload_jsonb = latest_draft_task.get("payload_jsonb") if isinstance(latest_draft_task, dict) else {}
        if isinstance(payload_jsonb, dict):
            latest_task_account_id = str(payload_jsonb.get("channel_account_id") or "")
            if latest_task_account_id:
                try:
                    account_rows = (
                        client.table("channel_accounts")
                        .select("account_name")
                        .eq("id", latest_task_account_id)
                        .limit(1)
                        .execute()
                    ).data or []
                    if account_rows:
                        latest_task_account_name = str(account_rows[0].get("account_name") or "")
                except Exception:  # noqa: BLE001
                    latest_task_account_name = ""

        return {
            "status": "ok",
            "domain_slug": domain_slug,
            "publish_runtime": {
                "mode": "demo" if env_dry_run else "real",
                "dry_run": env_dry_run,
                "ready_for_real_publish": ready_for_real_publish,
                "blockers": publish_blockers,
                "next_actions": [
                    "在 /accounts 配置并校验小红书发布账号",
                    "确认 PLAYWRIGHT_DRY_RUN=false 后再做真实发布",
                    "先执行一次“采集→草稿→审批→发布”全流程测试",
                ],
            },
            "publish_account": publish_account_view,
            "crawler_account": {
                "source": crawler_source,
                "auth_hint": crawler_auth_hint,
                "xhs_enabled": bool(xhs_source.get("enabled", True)),
                "xhs_max_per_query": int(xhs_source.get("max_per_query", 5)),
                "google_enabled": bool(google_source.get("enabled", True)),
                "google_max_per_query": int(google_source.get("max_per_query", 5)),
            },
            "latest": {
                "last_xhs_intel_at": _latest_intel_time(client, domain["id"], "hotspot_xiaohongshu_search", current_account_id),
                "last_google_hotspot_at": _latest_intel_time(client, domain["id"], "hotspot_google_news", current_account_id),
                "last_google_viewpoint_at": _latest_intel_time(client, domain["id"], "viewpoint_google_news", current_account_id),
                "last_draft_task_id": latest_draft_task.get("id") if latest_draft_task else None,
                "last_draft_task_status": latest_draft_task.get("status") if latest_draft_task else None,
                "last_published_task_id": latest_published_task.get("id") if latest_published_task else None,
                "last_published_at": latest_published_task.get("published_at") if latest_published_task else None,
                "last_task_account_id": latest_task_account_id or None,
                "last_task_account_name": latest_task_account_name or None,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "degraded",
            "domain_slug": domain_slug,
            "publish_runtime": {
                "mode": "unknown",
                "dry_run": True,
                "ready_for_real_publish": False,
                "blockers": ["后台数据暂时不可用，当前进入降级模式"],
                "next_actions": [
                    "先继续使用主屏与流程页的演示/缓存数据",
                    "检查 API 到 Supabase 的网络连通",
                    "恢复后再执行真实采集与发布链路",
                ],
            },
            "publish_account": {
                "configured": False,
                "account_id": None,
                "account_name": None,
                "account_handle": None,
                "login_mode": None,
                "auth_hint": "",
                "strategy_profile": {},
                "last_login_check_status": "degraded",
                "last_login_check_at": None,
                "last_login_check_message": user_facing_data_message(exc),
            },
            "crawler_account": {
                "source": "degraded",
                "auth_hint": "",
                "xhs_enabled": False,
                "xhs_max_per_query": 0,
                "google_enabled": False,
                "google_max_per_query": 0,
            },
            "latest": {
                "last_xhs_intel_at": None,
                "last_google_hotspot_at": None,
                "last_google_viewpoint_at": None,
                "last_draft_task_id": None,
                "last_draft_task_status": None,
                "last_published_task_id": None,
                "last_published_at": None,
                "last_task_account_id": None,
                "last_task_account_name": None,
            },
        }


@router.get("/run-ledger")
def get_run_ledger(
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    limit: int = 20,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, domain_slug)
    if not _table_ready(client, "runs_daily"):
        return {
            "status": "degraded",
            "domain_slug": domain_slug,
            "reason": "runs_daily_table_missing",
            "items": [],
        }

    safe_limit = max(1, min(limit, 100))
    query = (
        client.table("runs_daily")
        .select("*")
        .eq("domain_id", domain["id"])
        .order("created_at", desc=True)
        .limit(safe_limit)
    )
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        query = query.eq("account_id", normalized_account_id)
    runs = query.execute().data or []
    run_ids = [str(row.get("id") or "") for row in runs if str(row.get("id") or "").strip()]

    decisions_map: Dict[str, int] = {}
    if run_ids and _table_ready(client, "decisions"):
        decision_rows = (
            client.table("decisions")
            .select("run_id")
            .in_("run_id", run_ids)
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        ).data or []
        for row in decision_rows:
            run_id = str(row.get("run_id") or "")
            if not run_id:
                continue
            decisions_map[run_id] = decisions_map.get(run_id, 0) + 1

    account_ids = [str(row.get("account_id") or "") for row in runs if str(row.get("account_id") or "").strip()]
    account_name_map: Dict[str, str] = {}
    if account_ids and _table_ready(client, "channel_accounts"):
        account_rows = (
            client.table("channel_accounts")
            .select("id,account_name")
            .in_("id", account_ids)
            .limit(300)
            .execute()
        ).data or []
        for row in account_rows:
            account_name_map[str(row.get("id") or "")] = str(row.get("account_name") or "")

    items: List[Dict[str, Any]] = []
    for row in runs:
        run_id = str(row.get("id") or "")
        evidence = row.get("evidence_pack") if isinstance(row.get("evidence_pack"), dict) else {}
        goal = evidence.get("goal") if isinstance(evidence.get("goal"), dict) else {}
        account_snapshot = evidence.get("account") if isinstance(evidence.get("account"), dict) else {}
        items.append(
            {
                "id": run_id,
                "run_key": row.get("run_key"),
                "flow": row.get("flow"),
                "run_date": row.get("run_date"),
                "status": row.get("status"),
                "target_posts_min": row.get("target_posts_min"),
                "account_id": row.get("account_id"),
                "account_name": account_name_map.get(str(row.get("account_id") or ""), ""),
                "account_persona_name": account_snapshot.get("persona_name") or "",
                "primary_goal": goal.get("primary_goal") or "",
                "topic": goal.get("topic") or "",
                "decisions_count": decisions_map.get(run_id, 0),
                "retro_report": row.get("retro_report"),
                "error_message": row.get("error_message"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "finished_at": row.get("finished_at"),
            }
        )

    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "items": items,
    }


@router.get("/action-runs")
def get_action_runs(
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    source: str = "",
    action_type: str = "",
    status_filter: str = "",
    limit: int = 100,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    if not _table_ready(client, "action_runs"):
        return {
            "status": "degraded",
            "domain_slug": domain_slug,
            "reason": "action_runs_table_missing",
            "items": [],
        }
    domain = _resolve_domain(client, domain_slug)
    safe_limit = max(1, min(limit, 300))
    query = (
        client.table("action_runs")
        .select("*")
        .eq("domain_id", domain["id"])
        .order("created_at", desc=True)
        .limit(safe_limit)
    )
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        query = query.eq("account_id", normalized_account_id)
    normalized_source = str(source or "").strip()
    if normalized_source:
        query = query.eq("source", normalized_source)
    normalized_action_type = str(action_type or "").strip()
    if normalized_action_type:
        query = query.eq("action_type", normalized_action_type)
    normalized_status = str(status_filter or "").strip().lower()
    if normalized_status:
        query = query.eq("status", normalized_status)
    rows = query.execute().data or []
    return {"status": "ok", "domain_slug": domain_slug, "items": rows}


@router.get("/subagent-runs")
def get_subagent_runs(
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    status_filter: str = "",
    subagent: str = "",
    limit: int = 100,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    try:
        if not _table_ready(client, "action_runs"):
            return {
                "status": "degraded",
                "domain_slug": domain_slug,
                "reason": "action_runs_table_missing",
                "items": [],
            }
        domain = _resolve_domain(client, domain_slug)
        safe_limit = max(1, min(limit, 300))
        query = (
            client.table("action_runs")
            .select("*")
            .eq("domain_id", domain["id"])
            .like("source", "subagent.%")
            .order("created_at", desc=True)
            .limit(safe_limit)
        )
        normalized_account_id = str(account_id or "").strip()
        if normalized_account_id:
            query = query.eq("account_id", normalized_account_id)
        normalized_status = str(status_filter or "").strip().lower()
        if normalized_status:
            query = query.eq("status", normalized_status)
        normalized_subagent = str(subagent or "").strip().lower()
        if normalized_subagent:
            query = query.like("source", f"subagent.{normalized_subagent}%")
        rows = query.execute().data or []
        return {"status": "ok", "domain_slug": domain_slug, "items": rows}
    except Exception:  # noqa: BLE001
        return {"status": "degraded", "domain_slug": domain_slug, "items": []}


@router.get("/action-runs/summary")
def get_action_runs_summary(
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    source: str = "",
    since_hours: int = 24,
    limit: int = 1000,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    if not _table_ready(client, "action_runs"):
        return {
            "status": "degraded",
            "domain_slug": domain_slug,
            "reason": "action_runs_table_missing",
            "summary": {
                "total": 0,
                "by_status": {},
                "by_action_type": {},
                "top_error_codes": [],
            },
            "items": [],
        }
    domain = _resolve_domain(client, domain_slug)
    safe_hours = max(1, min(since_hours, 24 * 30))
    safe_limit = max(1, min(limit, 3000))
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=safe_hours)).isoformat()
    query = (
        client.table("action_runs")
        .select("status,action_type,error_code,retryable,source,created_at,trace_id")
        .eq("domain_id", domain["id"])
        .gte("created_at", since_iso)
        .order("created_at", desc=True)
        .limit(safe_limit)
    )
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        query = query.eq("account_id", normalized_account_id)
    normalized_source = str(source or "").strip()
    if normalized_source:
        query = query.eq("source", normalized_source)
    rows = query.execute().data or []

    status_counter: Counter[str] = Counter()
    action_counter: Counter[str] = Counter()
    error_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    retryable_count = 0
    for row in rows:
        status_text = str(row.get("status") or "").strip().lower() or "unknown"
        action_text = str(row.get("action_type") or "").strip() or "unknown"
        source_text = str(row.get("source") or "").strip() or "unknown"
        status_counter[status_text] += 1
        action_counter[action_text] += 1
        source_counter[source_text] += 1
        if bool(row.get("retryable")):
            retryable_count += 1
        err = str(row.get("error_code") or "").strip()
        if err:
            error_counter[err] += 1

    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "window_hours": safe_hours,
        "summary": {
            "total": len(rows),
            "retryable_count": retryable_count,
            "by_status": dict(status_counter),
            "by_action_type": dict(action_counter),
            "by_source": dict(source_counter),
            "top_error_codes": [{"code": code, "count": count} for code, count in error_counter.most_common(10)],
        },
        "items": rows[:100],
    }


@router.get("/coach-actions")
def get_coach_actions(
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    status_filter: str = "",
    limit: int = 100,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    if not _table_ready(client, "coach_actions"):
        return {
            "status": "degraded",
            "domain_slug": domain_slug,
            "reason": "coach_actions_table_missing",
            "items": [],
        }
    safe_limit = max(1, min(limit, 300))
    query = (
        client.table("coach_actions")
        .select("*")
        .eq("domain_slug", domain_slug)
        .order("created_at", desc=True)
        .limit(safe_limit)
    )
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        query = query.eq("account_id", normalized_account_id)
    normalized_status = str(status_filter or "").strip().lower()
    if normalized_status:
        query = query.eq("status", normalized_status)
    rows = query.execute().data or []
    return {"status": "ok", "domain_slug": domain_slug, "items": rows}


@router.get("/memory-items")
def list_memory_items(
    domain_slug: str = "japan_immigration",
    status: str = "active",
    account_id: str = "",
    include_global: bool = True,
    limit: int = 50,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, domain_slug)
    if not _table_ready(client, "memory_items"):
        return {
            "status": "degraded",
            "domain_slug": domain_slug,
            "reason": "memory_items_table_missing",
            "items": [],
        }

    safe_limit = max(1, min(limit, 200))
    query = (
        client.table("memory_items")
        .select("*")
        .eq("domain_id", domain["id"])
        .order("updated_at", desc=True)
        .limit(safe_limit)
    )
    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"active", "pending", "deprecated"}:
        query = query.eq("status", normalized_status)
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        if include_global:
            query = query.or_(f"account_id.is.null,account_id.eq.{normalized_account_id}")
        else:
            query = query.eq("account_id", normalized_account_id)
    rows = query.execute().data or []
    return {"status": "ok", "domain_slug": domain_slug, "items": rows}


def _contains_any(text: str, keywords: List[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _infer_pending_bottleneck(tags: List[str], title: str, content: str) -> str:
    normalized_tags = {str(tag or "").strip().lower() for tag in tags}
    for token in ["exposure", "value", "conversion"]:
        if token in normalized_tags:
            return token
    merged = f"{title} {content}".lower()
    if _contains_any(merged, ["曝光", "点击", "ctr", "hook", "标题"]):
        return "exposure"
    if _contains_any(merged, ["收藏", "信息密度", "结构", "停留", "价值"]):
        return "value"
    if _contains_any(merged, ["评论", "转化", "cta", "私信", "关注"]):
        return "conversion"
    return "value"


def _summarize_collection_plan(plan: Dict[str, Any]) -> str:
    mode = str(plan.get("mode") or "hybrid")
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    fallback = plan.get("fallback") if isinstance(plan.get("fallback"), list) else []
    return f"{mode} · 主步骤 {len(steps)} 个 / 降级 {len(fallback)} 个"


def _summarize_feedback_plan(plan: Dict[str, Any]) -> str:
    checkpoints = plan.get("checkpoints_hours") if isinstance(plan.get("checkpoints_hours"), list) else [1, 3, 24]
    checkpoints_text = " / ".join(str(int(x)) for x in checkpoints[:8] if isinstance(x, (int, float)))
    return (
        f"{checkpoints_text or '1 / 3 / 24'}h · "
        f"真实数据后升级={bool(plan.get('require_real_metrics_for_upgrade', True))} · "
        f"临时预览={bool(plan.get('synthetic_preview_enabled', True))}"
    )


def _summarize_publish_preferences(value: Dict[str, Any]) -> str:
    slot = str(value.get("next_publish_slot_local") or "未设定")
    min_gap = int(value.get("min_action_gap_seconds") or 2)
    max_gap = int(value.get("max_action_gap_seconds") or 3)
    return f"发布时间建议 {slot} · 动作间隔 {min_gap}-{max_gap}s"


def _build_pending_strategy_proposals(
    row: Dict[str, Any],
    *,
    account_id: str,
    account_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not account_id:
        return []
    tags = row.get("tags") if isinstance(row.get("tags"), list) else []
    title = str(row.get("title") or "")
    content = str(row.get("content") or "")
    bottleneck = _infer_pending_bottleneck(tags, title, content)
    reason_map = {
        "exposure": "本轮瓶颈在曝光层，优先增强入口与关键词覆盖。",
        "value": "本轮瓶颈在价值层，优先补足信息密度与详情样本。",
        "conversion": "本轮瓶颈在转化层，优先优化CTA承接与反馈节奏。",
    }
    reason = reason_map.get(bottleneck, reason_map["value"])
    proposals: List[Dict[str, Any]] = []

    # collection plan proposal
    collection_before = _normalize_account_collection_plan(
        account_config.get("collection_plan") if isinstance(account_config.get("collection_plan"), dict) else {}
    )
    collection_after = {
        "mode": collection_before.get("mode", "hybrid"),
        "steps": list(collection_before.get("steps") if isinstance(collection_before.get("steps"), list) else []),
        "fallback": list(collection_before.get("fallback") if isinstance(collection_before.get("fallback"), list) else []),
        "notes": str(collection_before.get("notes") or ""),
    }
    changed_collection = False
    if not collection_after["steps"]:
        collection_after["steps"] = [
            {"tool": "mcp_search", "limit": 8, "query": "日本移民", "scope": "global", "reason": "补公共关键词输入", "label": "公共关键词补采"},
            {"tool": "playwright_account_search", "limit": 6, "query": "日本移民", "scope": "account", "reason": "补账号私有搜索观察", "label": "账号关键词搜索"},
        ]
        changed_collection = True
    if bottleneck == "value":
        has_detail = any(
            isinstance(step, dict) and str(step.get("tool") or "") == "playwright_post_detail"
            for step in collection_after["steps"]
        )
        if not has_detail:
            collection_after["steps"].append(
                {
                    "tool": "playwright_post_detail",
                    "limit": 4,
                    "scope": "account",
                    "reason": "价值层偏弱，补详情正文与互动数据",
                    "label": "账号帖子详情",
                }
            )
            changed_collection = True
    else:
        for step in collection_after["steps"]:
            if not isinstance(step, dict):
                continue
            tool = str(step.get("tool") or "")
            if tool in {"mcp_search", "playwright_account_search"}:
                try:
                    before_limit = int(step.get("limit") or 8)
                except Exception:  # noqa: BLE001
                    before_limit = 8
                next_limit = max(before_limit, min(20, before_limit + 2))
                if next_limit != before_limit:
                    step["limit"] = next_limit
                    changed_collection = True
                break
    if changed_collection:
        collection_after = _normalize_account_collection_plan(collection_after)
        proposals.append(
            {
                "target": "collection_plan",
                "label": "采集计划",
                "reason": reason,
                "why": "让下一轮输入更充分，避免只靠当前少量样本做判断。",
                "before_summary": _summarize_collection_plan(collection_before),
                "after_summary": _summarize_collection_plan(collection_after),
                "before": collection_before,
                "after": collection_after,
                "effect": "确认后会直接更新当前账号的 collection_plan，并立即影响后续采集步骤。",
            }
        )

    # feedback plan proposal
    feedback_before = _normalize_account_feedback_plan(
        account_config.get("feedback_plan") if isinstance(account_config.get("feedback_plan"), dict) else {}
    )
    feedback_after = dict(feedback_before)
    if bottleneck == "exposure":
        feedback_after["checkpoints_hours"] = [1, 2, 6, 24]
    elif bottleneck == "conversion":
        feedback_after["checkpoints_hours"] = [1, 4, 24]
    else:
        feedback_after["checkpoints_hours"] = [1, 3, 12, 24]
    feedback_after = _normalize_account_feedback_plan(feedback_after)
    if feedback_after != feedback_before:
        proposals.append(
            {
                "target": "feedback_plan",
                "label": "反馈计划",
                "reason": reason,
                "why": "把反馈检查点调整到更匹配该账号当前瓶颈的节奏。",
                "before_summary": _summarize_feedback_plan(feedback_before),
                "after_summary": _summarize_feedback_plan(feedback_after),
                "before": feedback_before,
                "after": feedback_after,
                "effect": "确认后会更新反馈回收时机（1h/3h/24h等），影响复盘触发时间。",
            }
        )

    # publish preference proposal
    publish_before = (
        account_config.get("publish_preferences")
        if isinstance(account_config.get("publish_preferences"), dict)
        else {}
    )
    publish_after = dict(publish_before)
    publish_after["min_action_gap_seconds"] = max(1, int(publish_after.get("min_action_gap_seconds") or 2))
    publish_after["max_action_gap_seconds"] = max(
        publish_after["min_action_gap_seconds"],
        int(publish_after.get("max_action_gap_seconds") or 3),
    )
    if bottleneck == "exposure":
        publish_after["next_publish_slot_local"] = "20:30"
    elif bottleneck == "conversion":
        publish_after["next_publish_slot_local"] = "21:00"
    else:
        publish_after["next_publish_slot_local"] = "19:30"
    publish_after["updated_by_feedback"] = True
    if publish_after != publish_before:
        proposals.append(
            {
                "target": "publish_preferences",
                "label": "发布时间/节奏",
                "reason": reason,
                "why": "把下一轮发布时间和执行间隔固化为账号级偏好，便于复用和回看。",
                "before_summary": _summarize_publish_preferences(publish_before),
                "after_summary": _summarize_publish_preferences(publish_after),
                "before": publish_before,
                "after": publish_after,
                "effect": "确认后写入账号 publish_preferences，供下一轮调度直接读取。",
            }
        )
    return proposals


def _resolve_pending_strategy_proposals(row, *, account_id, account_config):
    from app.services.strategy_confirmation import _validate_executable
    try:
        content = row.get('content')
        body = content if isinstance(content, dict) else json.loads(str(content or '{}'))
        body = body if isinstance(body, dict) else {}
        _validate_executable(row, body, None)
    except (ValueError, TypeError, HTTPException):
        return []
    explicit = body.get('proposed_changes')
    if isinstance(body.get('proposal'), dict):
        explicit = [body['proposal']]
    proposals = explicit if isinstance(explicit, list) else _build_pending_strategy_proposals(row,
        account_id=account_id, account_config=account_config)
    summaries = {'collection_plan': ('采集计划', _summarize_collection_plan),
        'feedback_plan': ('反馈计划', _summarize_feedback_plan),
        'publish_preferences': ('发布时间建议', _summarize_publish_preferences)}
    results = []
    for proposal in proposals:
        if not isinstance(proposal, dict) or proposal.get('target') not in summaries:
            continue
        if not isinstance(proposal.get('before'), dict) or not isinstance(proposal.get('after'), dict):
            continue
        label, summary = summaries[proposal['target']]
        fingerprint = hashlib.sha256(json.dumps(proposal, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        results.append({**proposal, 'label': proposal.get('label') or label,
            'before_summary': proposal.get('before_summary') or summary(proposal['before']),
            'after_summary': proposal.get('after_summary') or summary(proposal['after']),
            'proposal_fingerprint': fingerprint})
    return results


@router.get("/pending-strategy-items")
def list_pending_strategy_items(
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    limit: int = 20,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    try:
        domain = _resolve_domain(client, domain_slug)
        if not _table_ready(client, "memory_items"):
            return {"status": "degraded", "domain_slug": domain_slug, "items": []}
        safe_limit = max(1, min(limit, 100))
        normalized_account_id = str(account_id or "").strip()
        query = (
            client.table("memory_items")
            .select("id,account_id,type,title,content,confidence,tags,created_at,updated_at,created_by")
            .eq("domain_id", domain["id"])
            .eq("status", "pending")
            .order("updated_at", desc=True)
            .limit(safe_limit)
        )
        if normalized_account_id:
            query = query.or_(f"account_id.eq.{normalized_account_id},account_id.is.null")
        rows = _run_with_db_retry(lambda: query.execute().data or [], fallback=[])
        account_name_map: Dict[str, str] = {}
        account_ids = [str(row.get("account_id") or "").strip() for row in rows if str(row.get("account_id") or "").strip()]
        account_config_map: Dict[str, Dict[str, Any]] = {}
        if account_ids:
            account_rows = _run_with_db_retry(
                lambda: (
                    client.table("channel_accounts")
                    .select("id,account_name,config_jsonb")
                    .in_("id", list(sorted(set(account_ids))))
                    .execute()
                ).data
                or [],
                fallback=[],
            )
            account_name_map = {str(row.get("id") or ""): str(row.get("account_name") or "") for row in account_rows}
            account_config_map = {
                str(row.get("id") or ""): (
                    row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
                )
                for row in account_rows
            }

        prompt_candidates: List[Dict[str, Any]] = []
        if normalized_account_id:
            try:
                prompt_candidates = list_prompt_versions(client, normalized_account_id, domain_slug=domain_slug, limit=20)
            except Exception:  # noqa: BLE001
                prompt_candidates = []

        items: List[Dict[str, Any]] = []
        for row in rows:
            tags = row.get("tags") if isinstance(row.get("tags"), list) else []
            source = "reflection"
            if "chief_evolution" in tags:
                source = "chief_evolution"
            elif "daily_ops" in tags:
                source = "daily_ops"
            affected_agent = ""
            for token in tags:
                text = str(token or "").strip()
                if text in {"draft_writer", "hot_post_analysis", "rebuild_strategy", "rebuild_copy"}:
                    affected_agent = text
                    break
            suggested_action = "activate_memory_item"
            if affected_agent:
                suggested_action = "generate_prompt_version"
            effective_account_id = str(row.get("account_id") or normalized_account_id or "").strip()
            proposals = _resolve_pending_strategy_proposals(
                row,
                account_id=effective_account_id,
                account_config=account_config_map.get(effective_account_id, {}),
            )
            if proposals:
                suggested_action = "confirm_apply_change"
            items.append(
                {
                    "id": row.get("id"),
                    "account_id": row.get("account_id"),
                    "account_name": account_name_map.get(
                        str(row.get("account_id") or ""),
                        account_name_map.get(normalized_account_id, "全局策略"),
                    ),
                    "type": str(row.get("type") or ""),
                    "title": str(row.get("title") or ""),
                    "content": str(row.get("content") or ""),
                    "source": source,
                    "suggested_action": suggested_action,
                    "affected_agent": affected_agent,
                    "confidence": float(row.get("confidence") or 0),
                    "tags": tags,
                    "linked_prompt_version_candidates": prompt_candidates if effective_account_id == normalized_account_id else [],
                    "proposed_changes": proposals,
                    "effective_account_id": effective_account_id or None,
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
        return {"status": "ok", "domain_slug": domain_slug, "items": items}
    except Exception:  # noqa: BLE001
        return {"status": "degraded", "domain_slug": domain_slug, "items": []}


@router.post("/pending-strategy-items/{item_id}/apply")
def apply_pending_strategy_item(
    item_id: str,
    request: PendingStrategyApplyRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    if not _table_ready(client, "memory_items"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="memory_items table missing")
    current_rows = (
        client.table("memory_items")
        .select("id,domain_id,account_id,type,title,content,tags,status,confidence,updated_at")
        .eq("id", item_id)
        .eq("domain_id", domain["id"])
        .limit(1)
        .execute()
    ).data or []
    if not current_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pending strategy item not found")
    item = current_rows[0]
    current_status = str(item.get("status") or "").strip().lower()
    if current_status not in {"pending", "active"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="item is not pending")

    apply_target = str(request.target or "").strip().lower()
    if apply_target not in {"collection_plan", "feedback_plan", "publish_preferences"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported apply target")

    effective_account_id = str(item.get("account_id") or "").strip() or str(request.account_id or "").strip()
    if request.account_id and str(request.account_id) != effective_account_id:
        raise HTTPException(status_code=404, detail="建议不属于当前账号")
    if not effective_account_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing account_id for apply target")

    account_rows = (
        client.table("channel_accounts")
        .select("id,account_name,config_jsonb")
        .eq("id", effective_account_id)
        .limit(1)
        .execute()
    ).data or []
    if not account_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    account_row = account_rows[0]
    current_config = account_row.get("config_jsonb") if isinstance(account_row.get("config_jsonb"), dict) else {}
    from app.services.strategy_confirmation import apply_confirmed_strategy
    if item_id in (current_config.get('strategy_confirmation_receipts') or {}):
        return apply_confirmed_strategy(client, item_id=item_id, domain_slug=request.domain_slug,
            account_id=effective_account_id, target=apply_target, actor=actor)
    proposals = _resolve_pending_strategy_proposals(
        item,
        account_id=effective_account_id,
        account_config=current_config,
    )
    proposal = next((entry for entry in proposals if str(entry.get("target") or "") == apply_target), None)
    if not proposal:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no applicable proposal for target")
    if request.proposal_fingerprint and request.proposal_fingerprint != proposal.get('proposal_fingerprint'):
        raise HTTPException(409, '建议已变化，请刷新后重新确认')
    return apply_confirmed_strategy(client, item_id=item_id, domain_slug=request.domain_slug,
        account_id=effective_account_id, target=apply_target, actor=actor, proposal=proposal)


@router.patch("/memory-items/{item_id}")
def patch_memory_item_status(
    item_id: str,
    request: MemoryItemStatusUpdateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    next_status = str(request.status or "").strip().lower()
    if next_status not in {"active", "pending", "deprecated"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid memory status")
    if not _table_ready(client, "memory_items"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="memory_items table missing")

    current = client.table("memory_items").select("*").eq("id", item_id).limit(1).execute().data or []
    if not current:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory item not found")
    before = current[0]
    updated = (
        client.table("memory_items")
        .update({"status": next_status, "updated_at": datetime.now(timezone.utc).isoformat(), "created_by": actor.user_id})
        .eq("id", item_id)
        .execute()
    ).data or []
    after = updated[0] if updated else before
    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="ops.memory_item_status_updated",
            target_type="memory_item",
            target_id=item_id,
            diff_jsonb={"from_status": before.get("status"), "to_status": next_status},
        ),
    )
    return {"status": "ok", "item": after}


@router.get("/crawler-template")
def get_crawler_template(
    domain_slug: str = "japan_immigration",
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, domain_slug)
    config = domain.get("config_jsonb") or {}
    template_raw = config.get("crawler_template") if isinstance(config.get("crawler_template"), dict) else {}
    template = _normalize_crawler_template(domain, template_raw)
    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "crawler_template": template,
        "template_example": _default_crawler_template(domain),
    }


@router.put("/crawler-template")
def upsert_crawler_template(
    request: CrawlerTemplateUpsertRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    config = dict(domain.get("config_jsonb") or {})
    template = _normalize_crawler_template(domain, request.crawler_template)
    config["crawler_template"] = template

    keyword_groups = template.get("keyword_groups") if isinstance(template.get("keyword_groups"), dict) else {}
    hotspot_queries = keyword_groups.get("hotspot_queries")
    viewpoint_queries = keyword_groups.get("viewpoint_queries")
    focus_keywords = keyword_groups.get("focus_keywords")
    if isinstance(hotspot_queries, list):
        config["hotspot_queries"] = hotspot_queries
    if isinstance(viewpoint_queries, list):
        config["viewpoint_queries"] = viewpoint_queries
    if isinstance(focus_keywords, list):
        config["focus_keywords"] = focus_keywords

    client.table("domains").update({"config_jsonb": config, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", domain["id"]).execute()
    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="ops.crawler_template_updated",
            target_type="domain",
            target_id=str(domain["id"]),
            diff_jsonb={"reason": request.reason, "crawler_template": template},
        ),
    )
    return {"status": "ok", "domain_slug": request.domain_slug, "crawler_template": template}


@router.post("/analyze-intel")
def analyze_intel(
    request: IntelAnalyzeRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    rows = _load_intel_rows(
        client,
        str(domain["id"]),
        request.limit,
        str(request.account_id or "").strip(),
    )

    analysis = _basic_intel_analysis(rows, custom_logic=request.custom_logic)
    prompt_for_ai = (
        "请基于以下信息生成今天可发布的内容草案（标题+正文结构+图片建议）：\n"
        f"数据摘要：{analysis.get('summary')}\n"
        f"高频关键词：{analysis.get('top_keywords')}\n"
        f"我补充的分析逻辑：{request.custom_logic or '无'}\n"
        "要求：先给3个标题，再给1份正文框架。"
    )
    return {
        "status": "ok",
        "domain_slug": request.domain_slug,
        "account_id": str(request.account_id or "").strip() or None,
        "analysis": analysis,
        "ai_discussion_prompt": prompt_for_ai,
    }


@router.post("/auto-config")
def auto_config_for_studio(
    request: OpsAutoConfigRequest,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    rows = _load_intel_rows(
        client,
        str(domain["id"]),
        limit=request.limit,
        account_id=str(request.account_id or "").strip(),
    )
    official_keywords = _extract_official_keywords(domain)
    xhs_query_counter, xhs_keyword_counter, source_paths = _collect_hotspot_terms(rows)
    if not xhs_query_counter and not xhs_keyword_counter:
        generic_query_counter: Counter[str] = Counter()
        generic_keyword_counter: Counter[str] = Counter()
        for row in rows:
            meta = row.get("meta_jsonb") if isinstance(row.get("meta_jsonb"), dict) else {}
            query = str(meta.get("query") or "").strip()
            if query:
                generic_query_counter[query] += 1
            raw_text = str(row.get("raw_text") or "")
            generic_keyword_counter.update(_tokens(raw_text))
        xhs_query_counter = generic_query_counter
        xhs_keyword_counter = generic_keyword_counter
    history_counter, sample_titles = _history_keyword_scores(client, str(domain["id"]))

    weighted: Counter[str] = Counter()
    evidence_map: Dict[str, List[str]] = {}

    for keyword in official_keywords:
        weighted[keyword] += 8.0
        evidence_map.setdefault(keyword, []).append("official")

    for query, count in xhs_query_counter.items():
        weighted[query] += float(count) * 4.0
        evidence_map.setdefault(query, []).append("xhs_query")

    for keyword, count in xhs_keyword_counter.items():
        weighted[keyword] += float(count) * 2.4
        evidence_map.setdefault(keyword, []).append("xhs_hotspot")

    for keyword, score in history_counter.items():
        weighted[keyword] += float(score) * 1.1
        evidence_map.setdefault(keyword, []).append("history")

    top_weighted = [
        {
            "keyword": keyword,
            "score": round(score, 2),
            "sources": sorted(set(evidence_map.get(keyword, []))),
        }
        for keyword, score in weighted.most_common(20)
    ]
    if not top_weighted:
        fallback = official_keywords[:4]
        if not fallback:
            fallback = ["日本移民", "经营管理签证", "日本永住", "日本留学"]
        top_weighted = [
            {"keyword": token, "score": 1.0, "sources": ["default_seed"]}
            for token in fallback
        ]
    top_keywords = [str(item["keyword"]) for item in top_weighted[:8]]
    top_queries = [query for query, _ in xhs_query_counter.most_common(5)]
    if not top_queries:
        cfg = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
        hotspot_queries = cfg.get("hotspot_queries") if isinstance(cfg.get("hotspot_queries"), list) else []
        top_queries = [str(x).strip() for x in hotspot_queries if str(x).strip()][:5]

    topic = _build_topic_from_signals(top_queries, top_keywords)
    inferred_track = _infer_track_from_keywords(top_keywords)
    inferred_audience = _infer_audience_tag(top_keywords)

    suggestions = [
        "优先使用“关键词 + 最新变化 + 避坑”结构写标题。",
        "正文先给结论，再给条件边界，最后引导私信/收藏。",
        "尽量把高频词放在首段前 80 字，提高搜索命中。",
    ]
    if request.custom_logic.strip():
        suggestions.append(f"已融合你的补充逻辑：{request.custom_logic.strip()}")

    return {
        "status": "ok",
        "domain_slug": request.domain_slug,
        "account_id": str(request.account_id or "").strip() or None,
        "recommended_form": {
            "channel": request.channel or "xiaohongshu",
            "content_type": "post",
            "topic": topic,
            "track": inferred_track,
            "audience_tag": inferred_audience,
            "publish_selector": "button:has-text('发布')",
        },
        "weights": {
            "official": 8.0,
            "xhs_query": 4.0,
            "xhs_hotspot": 2.4,
            "history": 1.1,
        },
        "evidence": {
            "official_keywords": official_keywords[:12],
            "top_xhs_queries": top_queries,
            "top_weighted_keywords": top_weighted,
            "history_sample_titles": sample_titles,
            "intel_sample_count": len(rows),
        },
        "suggestions": suggestions,
        "source_paths": source_paths[:12],
    }


@router.post("/analysis-feedback")
def submit_analysis_feedback(
    request: OpsAnalysisFeedbackRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    feedback_entry: Dict[str, Any] = {
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor.user_id,
        "judgement": request.judgement.strip(),
        "notes": (request.notes or "").strip(),
        "confidence": request.confidence,
        "decision": (request.decision or "").strip(),
    }

    if not pipeline_schema_ready(client):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pipeline_schema_missing")

    row_res = client.table("pipeline_tasks").select("id,status,review_jsonb").eq("id", request.pipeline_task_id).limit(1).execute()
    if not row_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pipeline task not found")

    row = row_res.data[0]
    review_jsonb = dict(row.get("review_jsonb") or {})
    history = review_jsonb.get("human_feedback_history")
    if not isinstance(history, list):
        history = []
    history.append(feedback_entry)
    review_jsonb["human_feedback_history"] = history[-30:]
    review_jsonb["latest_human_feedback"] = feedback_entry

    updated = (
        client.table("pipeline_tasks")
        .update({"review_jsonb": review_jsonb, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", request.pipeline_task_id)
        .execute()
    )

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="pipeline.analysis_feedback_submitted",
            target_type="pipeline_task",
            target_id=request.pipeline_task_id,
            diff_jsonb={
                "decision": feedback_entry["decision"],
                "confidence": feedback_entry["confidence"],
            },
        ),
    )

    return {
        "status": "ok",
        "mode": "pipeline",
        "pipeline_task_id": request.pipeline_task_id,
        "saved_feedback": feedback_entry,
        "review_jsonb": (updated.data or [row])[0].get("review_jsonb"),
    }


@router.get("/mcp-readonly/status")
async def get_mcp_readonly_status(
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/mcp/readonly/status"
    async with httpx.AsyncClient(timeout=20.0) as http_client:
        try:
            response = await http_client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/mcp-readonly/search")
async def run_mcp_readonly_search(
    request: McpReadonlySearchRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/mcp/readonly/search"
    async with httpx.AsyncClient(timeout=120.0) as http_client:
        try:
            response = await http_client.post(url, json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/mcp-readonly/collect-intel")
async def run_mcp_readonly_collect_intel(
    request: McpReadonlyCollectIntelRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/mcp/readonly/collect-intel"
    async with httpx.AsyncClient(timeout=120.0) as http_client:
        try:
            response = await http_client.post(url, json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/mcp-readonly/custom-call")
async def run_mcp_readonly_custom_call(
    request: McpReadonlyCustomCallRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/mcp/readonly/custom-call"
    async with httpx.AsyncClient(timeout=120.0) as http_client:
        try:
            response = await http_client.post(url, json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/mcp-readonly/sync-metrics")
async def run_mcp_readonly_sync_metrics(
    request: McpReadonlySyncMetricsRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/mcp/readonly/sync-metrics"
    async with httpx.AsyncClient(timeout=300.0) as http_client:
        try:
            response = await http_client.post(url, json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.get("/mcp-write/status")
async def get_mcp_write_status(
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/mcp/write/status"
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        try:
            response = await http_client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/mcp-write/custom-call")
async def run_mcp_write_custom_call(
    request: McpWriteCustomCallRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/mcp/write/custom-call"
    async with httpx.AsyncClient(timeout=180.0) as http_client:
        try:
            response = await http_client.post(url, json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/mcp-write/publish")
async def run_mcp_write_publish(
    request: McpWritePublishRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/mcp/write/publish"
    async with httpx.AsyncClient(timeout=240.0) as http_client:
        try:
            response = await http_client.post(url, json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/analyze-hot-posts")
async def analyze_hot_posts_with_llm(
    request: HotPostAnalyzeRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/analysis/hot-posts"
    async with httpx.AsyncClient(timeout=240.0) as http_client:
        try:
            response = await http_client.post(url, json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/rebuild-xhs-content")
async def rebuild_xhs_content_via_llm(
    request: RebuildXhsContentRequest,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    url = f"{settings.agent_service_url}/content/rebuild-xhs"
    async with httpx.AsyncClient(timeout=300.0) as http_client:
        try:
            response = await http_client.post(url, json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/chief-evolution")
async def run_chief_evolution(
    request: ChiefEvolutionRequest,
    settings: Settings = Depends(get_settings),
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    scoped_account_id = str(request.account_id or "").strip()
    domain = _resolve_domain(client, request.domain_slug)
    competitor_dataset = _load_competitor_dataset(
        client,
        domain_id=str(domain["id"]),
        limit=request.competitor_limit,
        account_id=scoped_account_id,
    )
    our_history = _load_our_history_dataset(
        client,
        domain_id=str(domain["id"]),
        limit=request.performance_limit,
        account_id=scoped_account_id,
    )
    current_system = _load_current_system_version(client, domain=domain, account_id=scoped_account_id)

    payload = {
        "competitor_dataset": competitor_dataset,
        "our_history": our_history,
        "current_system_version": current_system,
        "milestone_goal": request.milestone_goal or "",
    }

    url = f"{settings.agent_service_url}/analysis/chief-evolution"
    async with httpx.AsyncClient(timeout=300.0) as http_client:
        try:
            response = await http_client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    raw_result = response.json()
    result = raw_result if isinstance(raw_result, dict) else {}

    action_decision = str(result.get("action_decision") or "HOLD").upper()
    version_control = result.get("version_control") if isinstance(result.get("version_control"), dict) else {}
    analysis_report = result.get("analysis_report") if isinstance(result.get("analysis_report"), dict) else {}
    applied: Dict[str, Any] = {"status": "skipped", "reason": "apply_false_or_no_change"}
    control_version: str | None = None

    if request.apply and scoped_account_id:
        target_agent = str(version_control.get("target_agent") or "ALL").strip().lower()
        new_prompt = version_control.get("new_system_prompt")
        rollback_version = str(version_control.get("rollback_target_version") or "").strip()
        agent_names: List[str] = []
        if target_agent in {"agent 2", "agent2", "all"}:
            agent_names.append("rebuild_strategy")
        if target_agent in {"agent 3", "agent3", "all"}:
            agent_names.append("rebuild_copy")
        if not agent_names:
            agent_names = ["rebuild_strategy", "rebuild_copy"]

        if action_decision == "UPGRADE" and isinstance(new_prompt, str) and new_prompt.strip():
            applied_rows: List[Dict[str, Any]] = []
            for agent_name in agent_names:
                agent_rows = list_prompt_versions(
                    client,
                    scoped_account_id,
                    domain_slug=request.domain_slug,
                    agent_name=agent_name,
                    limit=50,
                )
                created_row = create_prompt_version(
                    client,
                    scoped_account_id,
                    PromptVersionCreateRequest(
                        domain_slug=request.domain_slug,
                        agent_name=agent_name,
                        version=_next_account_prompt_version(agent_rows),
                        system_prompt=new_prompt.strip(),
                        status="active",
                        source="chief_evolution",
                        reason=str(version_control.get("action_reason") or "chief evolution upgrade"),
                        evidence_jsonb={
                            "milestone_goal": request.milestone_goal,
                            "action_decision": action_decision,
                            "target_agent": target_agent,
                        },
                    ),
                    actor,
                )
                applied_rows.append(created_row)
            applied = {
                "status": "applied",
                "type": "UPGRADE",
                "scope": "account",
                "account_id": scoped_account_id,
                "prompt_versions": [
                    {
                        "id": row.get("id"),
                        "agent_name": row.get("agent_name"),
                        "version": row.get("version"),
                        "status": row.get("status"),
                    }
                    for row in applied_rows
                ],
            }
        elif action_decision == "ROLLBACK" and rollback_version:
            rolled_back_rows: List[Dict[str, Any]] = []
            missing_agents: List[str] = []
            for agent_name in agent_names:
                target_row = get_prompt_version_by_version(
                    client,
                    scoped_account_id,
                    domain_slug=request.domain_slug,
                    agent_name=agent_name,
                    version=rollback_version,
                )
                if not target_row:
                    missing_agents.append(agent_name)
                    continue
                rolled_back_rows.append(
                    activate_prompt_version(
                        client,
                        scoped_account_id,
                        str(target_row.get("id") or ""),
                        f"chief evolution rollback -> {rollback_version}",
                        actor,
                    )
                )
            if rolled_back_rows:
                applied = {
                    "status": "applied",
                    "type": "ROLLBACK",
                    "scope": "account",
                    "account_id": scoped_account_id,
                    "version": rollback_version,
                    "prompt_versions": [
                        {
                            "id": row.get("id"),
                            "agent_name": row.get("agent_name"),
                            "version": row.get("version"),
                            "status": row.get("status"),
                        }
                        for row in rolled_back_rows
                    ],
                    "missing_agents": missing_agents,
                }
            else:
                applied = {
                    "status": "failed",
                    "type": "ROLLBACK",
                    "scope": "account",
                    "reason": "rollback_target_not_found",
                    "missing_agents": missing_agents,
                }
        else:
            applied = {"status": "hold", "type": action_decision, "scope": "account", "account_id": scoped_account_id}

        write_audit_log(
            client,
            AuditLogEntry(
                actor=actor.user_id,
                action="ops.chief_evolution",
                target_type="account",
                target_id=scoped_account_id,
                diff_jsonb={
                    "domain_slug": request.domain_slug,
                    "milestone_goal": request.milestone_goal,
                    "action_decision": action_decision,
                    "applied": applied,
                },
            ),
        )
    elif request.apply:
        config = dict(domain.get("config_jsonb") or {})
        control = _normalize_orchestrator_control(config.get("orchestrator_control") if isinstance(config.get("orchestrator_control"), dict) else {})
        prompt_overrides = control.get("prompt_overrides") if isinstance(control.get("prompt_overrides"), dict) else {}
        data_input = control.get("data_input") if isinstance(control.get("data_input"), dict) else {}
        versions = config.get("orchestrator_prompt_versions")
        if not isinstance(versions, list):
            versions = []

        if not versions:
            versions.append(
                {
                    "version": "v1.0",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "baseline",
                    "agent2_prompt": str(prompt_overrides.get("rebuild_strategy_prompt") or ""),
                    "agent3_prompt": str(prompt_overrides.get("rebuild_copy_prompt") or ""),
                }
            )
            config["orchestrator_prompt_active_version"] = "v1.0"

        target_agent = str(version_control.get("target_agent") or "ALL").strip().lower()
        new_prompt = version_control.get("new_system_prompt")
        rollback_version = str(version_control.get("rollback_target_version") or "").strip()

        if action_decision == "UPGRADE" and isinstance(new_prompt, str) and new_prompt.strip():
            updated_agent2 = str(prompt_overrides.get("rebuild_strategy_prompt") or "")
            updated_agent3 = str(prompt_overrides.get("rebuild_copy_prompt") or "")
            if target_agent in {"agent 2", "agent2", "all"}:
                updated_agent2 = new_prompt.strip()
            if target_agent in {"agent 3", "agent3", "all"}:
                updated_agent3 = new_prompt.strip()
            prompt_overrides["rebuild_strategy_prompt"] = updated_agent2
            prompt_overrides["rebuild_copy_prompt"] = updated_agent3
            control["prompt_overrides"] = prompt_overrides
            config["orchestrator_control"] = _normalize_orchestrator_control(control)

            next_version = _next_prompt_version(versions)
            versions.append(
                {
                    "version": next_version,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "reason": str(version_control.get("action_reason") or "chief_evolution_upgrade"),
                    "agent2_prompt": updated_agent2,
                    "agent3_prompt": updated_agent3,
                }
            )
            config["orchestrator_prompt_versions"] = versions[-40:]
            config["orchestrator_prompt_active_version"] = next_version
            applied = {"status": "applied", "type": "UPGRADE", "version": next_version}
            control_version = _append_orchestrator_control_version(
                config=config,
                control=config["orchestrator_control"],
                reason=f"chief-evolution upgrade -> {next_version}",
                actor=actor.user_id,
            )
        elif action_decision == "ROLLBACK" and rollback_version:
            target = next((row for row in versions if str(row.get("version") or "") == rollback_version), None)
            if target:
                prompt_overrides["rebuild_strategy_prompt"] = str(target.get("agent2_prompt") or "")
                prompt_overrides["rebuild_copy_prompt"] = str(target.get("agent3_prompt") or "")
                control["prompt_overrides"] = prompt_overrides
                config["orchestrator_control"] = _normalize_orchestrator_control(control)
                config["orchestrator_prompt_versions"] = versions[-40:]
                config["orchestrator_prompt_active_version"] = rollback_version
                applied = {"status": "applied", "type": "ROLLBACK", "version": rollback_version}
                control_version = _append_orchestrator_control_version(
                    config=config,
                    control=config["orchestrator_control"],
                    reason=f"chief-evolution rollback -> {rollback_version}",
                    actor=actor.user_id,
                )
            else:
                applied = {"status": "failed", "type": "ROLLBACK", "reason": "rollback_target_not_found"}
        else:
            config["orchestrator_prompt_versions"] = versions[-40:]
            config["orchestrator_control"] = _normalize_orchestrator_control(control)
            applied = {"status": "hold", "type": action_decision}

        history_patch = _history_driven_data_input_patch(our_history=our_history, control=control)
        if history_patch:
            if "mcp_mode" in history_patch:
                data_input["mcp_mode"] = history_patch["mcp_mode"]
            if "default_limit" in history_patch:
                data_input["default_limit"] = history_patch["default_limit"]
            if "include_detail_metrics" in history_patch:
                data_input["include_detail_metrics"] = history_patch["include_detail_metrics"]
            control["data_input"] = data_input
            config["orchestrator_control"] = _normalize_orchestrator_control(control)
            if isinstance(applied, dict):
                applied["history_patch"] = history_patch
            control_version = _append_orchestrator_control_version(
                config=config,
                control=config["orchestrator_control"],
                reason="chief-evolution history-driven data_input patch",
                actor=actor.user_id,
            )

        client.table("domains").update({"config_jsonb": config, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", domain["id"]).execute()
        write_audit_log(
            client,
            AuditLogEntry(
                actor=actor.user_id,
                action="ops.chief_evolution",
                target_type="domain",
                target_id=str(domain["id"]),
                diff_jsonb={
                    "milestone_goal": request.milestone_goal,
                    "action_decision": action_decision,
                    "applied": applied,
                    "control_version": control_version,
                },
            ),
        )

    if scoped_account_id:
        methodology = str(analysis_report.get("extracted_methodology") or "").strip()
        action_reason = str(version_control.get("action_reason") or "").strip()
        competitor_insights = str(analysis_report.get("competitor_insights") or "").strip()
        our_weakness = str(analysis_report.get("our_weakness") or "").strip()
        target_agent = str(version_control.get("target_agent") or "ALL").strip()
        action_text = f"决策：{action_decision}；对象：{target_agent}；原因：{action_reason or '无'}"
        methodology_note = (
            f"方法论：{methodology or '无'}\n"
            f"同行核心赢面：{competitor_insights or '无'}\n"
            f"我方短板：{our_weakness or '无'}\n"
            f"{action_text}\n"
            f"应用状态：{applied.get('status')}"
        )
        _upsert_pending_memory_item(
            client,
            domain_id=str(domain["id"]),
            account_id=scoped_account_id,
            title="最高反思方法论（待确认）",
            content=methodology_note,
            tags=["chief_evolution", "pending_review", "methodology", action_decision.lower()],
            confidence=0.78,
        )
        _upsert_pending_memory_item(
            client,
            domain_id=str(domain["id"]),
            account_id=scoped_account_id,
            title="最高反思提示词动作（待确认）",
            content=(
                f"{action_text}\n"
                f"建议新提示词：{str(version_control.get('new_system_prompt') or '')[:1500] or '无'}\n"
                f"回滚目标版本：{str(version_control.get('rollback_target_version') or '') or '无'}"
            ),
            tags=["chief_evolution", "pending_review", "prompt_action", target_agent.lower().replace(' ', '_')],
            confidence=0.74,
        )

    return {
        "status": "ok",
        "domain_slug": request.domain_slug,
        "inputs": {
            "competitor_count": len(competitor_dataset),
            "history_count": len(our_history),
            "milestone_goal": request.milestone_goal or "",
            "account_id": scoped_account_id or None,
        },
        "result": result,
        "applied": applied,
        "control_version": control_version,
    }


@router.get("/orchestrator-control")
def get_orchestrator_control(
    domain_slug: str = "japan_immigration",
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, domain_slug)
    config = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
    control_raw = config.get("orchestrator_control") if isinstance(config.get("orchestrator_control"), dict) else {}
    control = _normalize_orchestrator_control(control_raw)
    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "control": control,
    }


@router.get("/orchestrator-control/versions")
def get_orchestrator_control_versions(
    domain_slug: str = "japan_immigration",
    limit: int = 20,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, domain_slug)
    config = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
    versions = config.get("orchestrator_control_versions")
    if not isinstance(versions, list):
        versions = []
    safe_limit = max(1, min(limit, 60))
    rows = versions[-safe_limit:]
    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "active_version": config.get("orchestrator_control_active_version"),
        "items": rows,
    }


@router.post("/orchestrator-control/rollback")
def rollback_orchestrator_control(
    request: OrchestratorControlRollbackRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    config = dict(domain.get("config_jsonb") or {})
    versions = config.get("orchestrator_control_versions")
    if not isinstance(versions, list):
        versions = []
    target = next((row for row in versions if str(row.get("version") or "") == request.version), None)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="orchestrator control version not found")

    raw_control = target.get("control_jsonb") if isinstance(target.get("control_jsonb"), dict) else {}
    restored_control = _normalize_orchestrator_control(raw_control)
    config["orchestrator_control"] = restored_control
    config["orchestrator_control_active_version"] = request.version

    client.table("domains").update({"config_jsonb": config, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", domain["id"]).execute()
    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="ops.orchestrator_control_rollback",
            target_type="domain",
            target_id=str(domain["id"]),
            diff_jsonb={
                "rollback_version": request.version,
                "reason": request.reason,
            },
        ),
    )
    return {
        "status": "ok",
        "domain_slug": request.domain_slug,
        "active_version": request.version,
        "control": restored_control,
    }


@router.put("/orchestrator-control")
def upsert_orchestrator_control(
    request: OrchestratorControlUpsertRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    config = dict(domain.get("config_jsonb") or {})
    control = _normalize_orchestrator_control(request.control_jsonb)
    config["orchestrator_control"] = control
    control_version = _append_orchestrator_control_version(
        config=config,
        control=control,
        reason=request.reason,
        actor=actor.user_id,
    )

    client.table("domains").update({"config_jsonb": config, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", domain["id"]).execute()
    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="ops.orchestrator_control_updated",
            target_type="domain",
            target_id=str(domain["id"]),
            diff_jsonb={"reason": request.reason, "control": control, "control_version": control_version},
        ),
    )
    return {
        "status": "ok",
        "domain_slug": request.domain_slug,
        "control": control,
        "control_version": control_version,
    }


@router.post("/orchestrator-self-upgrade")
def orchestrator_self_upgrade(
    request: OrchestratorSelfUpgradeRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> Dict[str, Any]:
    domain = _resolve_domain(client, request.domain_slug)
    config = dict(domain.get("config_jsonb") or {})
    control_raw = config.get("orchestrator_control") if isinstance(config.get("orchestrator_control"), dict) else {}
    control = _normalize_orchestrator_control(control_raw)

    result = _build_orchestrator_self_upgrade(client, domain_id=str(domain["id"]), control=control)
    now_iso = datetime.now(timezone.utc).isoformat()
    self_upgrade = control.get("self_upgrade") if isinstance(control.get("self_upgrade"), dict) else {}
    self_upgrade["last_suggested_at"] = now_iso
    self_upgrade["last_note"] = request.note
    self_upgrade["suggestions"] = result.get("suggestions") or []
    control["self_upgrade"] = self_upgrade

    applied_patch: Dict[str, Any] = {}
    control_version: str | None = None
    if request.apply:
        patch = result.get("patch") if isinstance(result.get("patch"), dict) else {}
        data_input = control.get("data_input") if isinstance(control.get("data_input"), dict) else {}
        if "default_limit" in patch:
            data_input["default_limit"] = patch["default_limit"]
            applied_patch["data_input.default_limit"] = patch["default_limit"]
        if "include_detail_metrics" in patch:
            data_input["include_detail_metrics"] = patch["include_detail_metrics"]
            applied_patch["data_input.include_detail_metrics"] = patch["include_detail_metrics"]
        if "mcp_mode" in patch:
            data_input["mcp_mode"] = patch["mcp_mode"]
            applied_patch["data_input.mcp_mode"] = patch["mcp_mode"]
        control["data_input"] = data_input
        self_upgrade["last_applied_at"] = now_iso
        if applied_patch:
            control_version = _append_orchestrator_control_version(
                config=config,
                control=_normalize_orchestrator_control(control),
                reason=f"self-upgrade apply: {request.note or 'auto patch'}",
                actor=actor.user_id,
            )

    config["orchestrator_control"] = _normalize_orchestrator_control(control)
    client.table("domains").update({"config_jsonb": config, "updated_at": now_iso}).eq("id", domain["id"]).execute()

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="ops.orchestrator_self_upgrade",
            target_type="domain",
            target_id=str(domain["id"]),
            diff_jsonb={
                "apply": request.apply,
                "note": request.note,
                "sample_summary": result.get("sample_summary"),
                "applied_patch": applied_patch,
                "control_version": control_version,
            },
        ),
    )
    return {
        "status": "ok",
        "domain_slug": request.domain_slug,
        "applied": request.apply,
        "sample_summary": result.get("sample_summary"),
        "suggestions": result.get("suggestions") or [],
        "applied_patch": applied_patch,
        "control": config["orchestrator_control"],
        "control_version": control_version,
    }
