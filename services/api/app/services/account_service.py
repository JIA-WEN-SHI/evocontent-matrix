from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client

from app.models import (
    AccountOnboardingCommitRequest,
    AccountAuthResetResponse,
    AccountLoginBootstrapRequest,
    AccountLoginBootstrapResponse,
    Actor,
    AuditLogEntry,
    ChannelAccountCreateRequest,
    ChannelAccountCollectionPlanUpsertRequest,
    ChannelAccountStrategyUpsertRequest,
    ChannelAccountUpdateRequest,
    PromptVersionCreateRequest,
    ReviewActionRequest,
)
from app.services.audit import write_audit_log
from app.services.execution_route_service import resolve_execution_route_view
from app.services.pipeline_service import approve_pipeline_task, get_pipeline_task, reject_pipeline_task

ALLOWED_CHANNELS = {"xiaohongshu", "wechat_mp", "douyin", "video"}
ALLOWED_LOGIN_MODES = {"storage_state", "user_data_dir", "cookies_json", "credential"}
ALLOWED_PROMPT_STATUS = {"draft", "active", "rolled_back", "archived"}
ALLOWED_PROMPT_SOURCE = {"manual", "reflection", "chief_evolution", "system_seed"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _single_or_404(rows: list[Dict[str, Any]], target: str) -> Dict[str, Any]:
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{target} not found")
    return rows[0]


def _public_account_row(row: Dict[str, Any]) -> Dict[str, Any]:
    masked = dict(row or {})
    masked["has_login_password"] = bool(str(masked.get("login_password") or "").strip())
    masked.pop("login_password", None)
    return masked


def _normalize_strategy_profile(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}

    def _to_list(value: Any, max_items: int = 20, max_len: int = 120) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            result.append(text[:max_len])
            if len(result) >= max_items:
                break
        return result

    mode = str(src.get("mcp_mode") or "hotspot").strip().lower()
    if mode not in {"home", "keyword", "hotspot", "profile"}:
        mode = "hotspot"

    def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, parsed))

    try:
        limit = int(src.get("mcp_limit_default") or 12)
    except (TypeError, ValueError):
        limit = 12
    limit = max(1, min(100, limit))

    quality_raw = src.get("quality_gate") if isinstance(src.get("quality_gate"), dict) else {}
    allowed_checks = {
        "title_length",
        "body_length",
        "paragraph_count",
        "keyword_coverage",
        "hashtag_count",
        "cta_presence",
        "forbidden_claims",
        "encoding_clean",
    }
    emphasize_checks = [
        item
        for item in _to_list(quality_raw.get("emphasize_checks"), max_items=8, max_len=40)
        if item in allowed_checks
    ]
    quality_gate = {
        "pass_score": _to_int(quality_raw.get("pass_score"), default=72, minimum=50, maximum=95),
        "min_keyword_hits": _to_int(quality_raw.get("min_keyword_hits"), default=2, minimum=1, maximum=5),
        "hashtag_target": _to_int(quality_raw.get("hashtag_target"), default=3, minimum=1, maximum=8),
        "paragraph_min": _to_int(quality_raw.get("paragraph_min"), default=4, minimum=2, maximum=10),
        "title_min": _to_int(quality_raw.get("title_min"), default=10, minimum=6, maximum=30),
        "title_max": _to_int(quality_raw.get("title_max"), default=28, minimum=12, maximum=40),
        "body_min": _to_int(quality_raw.get("body_min"), default=140, minimum=60, maximum=800),
        "body_max": _to_int(quality_raw.get("body_max"), default=1200, minimum=300, maximum=3000),
        "emphasize_checks": emphasize_checks,
    }

    return {
        "persona_name": str(src.get("persona_name") or "").strip(),
        "ip_positioning": str(src.get("ip_positioning") or "").strip(),
        "tone_style": str(src.get("tone_style") or "").strip(),
        "primary_goal": str(src.get("primary_goal") or "线索转化").strip(),
        "cta_style": str(src.get("cta_style") or "").strip(),
        "audience": _to_list(src.get("audience"), max_items=8),
        "pain_points": _to_list(src.get("pain_points"), max_items=8),
        "content_pillars": _to_list(src.get("content_pillars"), max_items=8),
        "forbidden_claims": _to_list(src.get("forbidden_claims"), max_items=12),
        "publish_constraints": _to_list(src.get("publish_constraints"), max_items=20, max_len=200),
        "focus_keywords": _to_list(src.get("focus_keywords"), max_items=20),
        "hotspot_queries": _to_list(src.get("hotspot_queries"), max_items=20),
        "viewpoint_queries": _to_list(src.get("viewpoint_queries"), max_items=20),
        "mcp_mode": mode,
        "mcp_query_default": str(src.get("mcp_query_default") or "日本移民").strip()[:300],
        "mcp_limit_default": limit,
        "include_detail_metrics": bool(src.get("include_detail_metrics", True)),
        "mcp_feed_tool": str(src.get("mcp_feed_tool") or "").strip()[:120],
        "mcp_search_tool": str(src.get("mcp_search_tool") or "").strip()[:120],
        "mcp_metrics_tool": str(src.get("mcp_metrics_tool") or "").strip()[:120],
        "mcp_profile_tool": str(src.get("mcp_profile_tool") or "").strip()[:120],
        "mcp_profile_id_key": str(src.get("mcp_profile_id_key") or "user_id").strip()[:80] or "user_id",
        "mcp_extra_args": src.get("mcp_extra_args") if isinstance(src.get("mcp_extra_args"), dict) else {},
        "prompt_overrides": src.get("prompt_overrides") if isinstance(src.get("prompt_overrides"), dict) else {},
        "quality_gate": quality_gate,
    }


def _collection_threshold(value: Any) -> int:
    try:
        return max(0, min(300, int(3 if value is None else value)))
    except (TypeError, ValueError, OverflowError):
        return 3


def _normalize_collection_plan(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    mode = str(src.get("mode") or "hybrid").strip().lower()
    if mode not in {"hybrid", "mcp_only", "playwright_only", "browser_ui", "xhs_cli"}:
        mode = "hybrid"

    def _clean_step(item: Any) -> Dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        tool = str(item.get("tool") or "").strip().lower()
        if tool not in {
            "mcp_search",
            "mcp_home",
            "mcp_profile",
            "playwright_account_search",
            "playwright_profile",
            "playwright_post_detail",
            "browser_ui_search",
            "xhs_cli_search",
        }:
            return None
        limit_raw = item.get("limit")
        try:
            limit = max(1, min(100, int(limit_raw if limit_raw is not None else 8)))
        except (TypeError, ValueError):
            limit = 8
        scope = str(item.get("scope") or "").strip().lower()
        if scope not in {"global", "account"}:
            scope = "account" if tool.startswith(("playwright_", "browser_ui_", "xhs_cli_")) else "global"
        step: Dict[str, Any] = {"tool": tool, "limit": limit, "scope": scope}
        for key, max_len in {
            "query": 300,
            "profile_hint": 200,
            "profile_id": 200,
            "detail_url": 1000,
            "reason": 1000,
            "label": 200,
        }.items():
            value = str(item.get(key) or "").strip()
            if value:
                step[key] = value[:max_len]
        fallback_to = str(item.get("fallback_to") or "").strip().lower()
        if fallback_to:
            step["fallback_to"] = fallback_to[:120]
        extra_args = item.get("extra_args")
        if isinstance(extra_args, dict):
            step["extra_args"] = extra_args
        return step

    steps = [_clean_step(item) for item in (src.get("steps") if isinstance(src.get("steps"), list) else [])]
    fallback = [_clean_step(item) for item in (src.get("fallback") if isinstance(src.get("fallback"), list) else [])]

    raw_schedule = src.get("daily_schedule") if isinstance(src.get("daily_schedule"), dict) else {}
    raw_slots = raw_schedule.get("time_slots") if isinstance(raw_schedule.get("time_slots"), list) else []
    time_slots: list[str] = []
    for value in raw_slots[:8]:
        text = str(value or "").strip()
        if not text:
            continue
        time_slots.append(text[:20])
    if not time_slots:
        time_slots = ["10:00", "20:00"]
    try:
        posts_per_day = int(raw_schedule.get("posts_per_day") or 1)
    except (TypeError, ValueError):
        posts_per_day = 1
    posts_per_day = max(1, min(10, posts_per_day))

    raw_gate = src.get("loop_gate") if isinstance(src.get("loop_gate"), dict) else {}
    min_case_per_day = _collection_threshold(raw_gate.get("min_case_per_day"))
    min_asset_per_day = _collection_threshold(raw_gate.get("min_asset_per_day"))

    raw_refresh = src.get("topic_refresh") if isinstance(src.get("topic_refresh"), dict) else {}
    try:
        window_days = int(raw_refresh.get("window_days") or 7)
    except (TypeError, ValueError):
        window_days = 7
    try:
        refresh_every_hours = int(raw_refresh.get("refresh_every_hours") or 24)
    except (TypeError, ValueError):
        refresh_every_hours = 24
    source_priority = raw_refresh.get("source_priority") if isinstance(raw_refresh.get("source_priority"), list) else []
    normalized_priority = []
    for item in source_priority[:6]:
        text = str(item or "").strip().lower()
        if text in {"case", "asset", "user_need", "review"} and text not in normalized_priority:
            normalized_priority.append(text)
    if not normalized_priority:
        normalized_priority = ["case", "asset"]

    return {
        "mode": mode,
        "steps": [item for item in steps if item][:12],
        "fallback": [item for item in fallback if item][:8],
        "notes": str(src.get("notes") or "").strip()[:2000],
        "daily_schedule": {
            "posts_per_day": posts_per_day,
            "time_slots": time_slots,
            "timezone": str(raw_schedule.get("timezone") or "Asia/Shanghai").strip()[:80] or "Asia/Shanghai",
        },
        "loop_gate": {
            "min_case_per_day": max(0, min(300, min_case_per_day)),
            "min_asset_per_day": max(0, min(300, min_asset_per_day)),
        },
        "topic_refresh": {
            "window_days": max(1, min(30, window_days)),
            "refresh_every_hours": max(1, min(168, refresh_every_hours)),
            "source_priority": normalized_priority,
        },
    }


def _normalize_feedback_plan(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    raw_hours = src.get("checkpoints_hours")
    checkpoints: list[int] = []
    if isinstance(raw_hours, list):
        for item in raw_hours:
            try:
                hour = int(item)
            except (TypeError, ValueError):
                continue
            if hour > 0 and hour not in checkpoints:
                checkpoints.append(hour)
    if not checkpoints:
        checkpoints = [1, 3, 24]
    raw_gate = src.get("exposure_gate") if isinstance(src.get("exposure_gate"), dict) else {}
    try:
        min_impressions = float(raw_gate.get("min_impressions") or 100)
    except (TypeError, ValueError):
        min_impressions = 100.0
    try:
        min_engagement_rate = float(raw_gate.get("min_engagement_rate") or 0.03)
    except (TypeError, ValueError):
        min_engagement_rate = 0.03
    try:
        review_window_hours = int(raw_gate.get("review_window_hours") or 24)
    except (TypeError, ValueError):
        review_window_hours = 24
    return {
        "checkpoints_hours": checkpoints[:8],
        "require_real_metrics_for_upgrade": bool(src.get("require_real_metrics_for_upgrade", True)),
        "synthetic_preview_enabled": bool(src.get("synthetic_preview_enabled", True)),
        "auto_retro_after_last_checkpoint": bool(src.get("auto_retro_after_last_checkpoint", True)),
        "exposure_gate": {
            "min_impressions": max(0.0, min(10_000_000.0, min_impressions)),
            "min_engagement_rate": max(0.0, min(1.0, min_engagement_rate)),
            "review_window_hours": max(1, min(168, review_window_hours)),
        },
    }


def _normalize_channel(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in ALLOWED_CHANNELS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unsupported channel: {value}")
    return normalized


def _normalize_login_mode(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in ALLOWED_LOGIN_MODES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unsupported login mode: {value}")
    return normalized


def _extract_error_message(exc: APIError) -> str:
    try:
        payload = exc.args[0] if exc.args else {}
        if isinstance(payload, dict):
            return str(payload.get("message") or "")
        return str(exc)
    except Exception:  # noqa: BLE001
        return str(exc)


def _is_missing_table(exc: APIError) -> bool:
    raw_text = str(exc).lower()
    message = _extract_error_message(exc).lower()
    code = ""
    try:
        payload = exc.args[0] if exc.args else {}
        if isinstance(payload, dict):
            code = str(payload.get("code") or "")
    except Exception:  # noqa: BLE001
        code = ""

    if "channel_accounts" not in raw_text and "channel_accounts" not in message:
        return False
    if code == "PGRST205":
        return True
    return "could not find the table" in raw_text or "schema cache" in raw_text


def _is_missing_credential_columns(exc: APIError) -> bool:
    raw_text = str(exc).lower()
    message = _extract_error_message(exc).lower()
    if "channel_accounts" not in raw_text and "channel_accounts" not in message:
        return False
    has_login_column = (
        "login_username" in raw_text
        or "login_password" in raw_text
        or "login_username" in message
        or "login_password" in message
    )
    if not has_login_column:
        return False
    return "column" in raw_text or "schema cache" in raw_text or "column" in message


def accounts_schema_ready(client: Client) -> bool:
    try:
        client.table("channel_accounts").select("id").limit(1).execute()
        return True
    except APIError as exc:
        if _is_missing_table(exc):
            return False
        raise


def _ensure_schema_ready(client: Client) -> None:
    if not accounts_schema_ready(client):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="channel_accounts table not found, please run migration 006_channel_accounts.sql",
        )


def list_channel_accounts(client: Client, *, channel: Optional[str] = None, limit: int = 100) -> list[Dict[str, Any]]:
    _ensure_schema_ready(client)
    query = client.table("channel_accounts").select("*").order("updated_at", desc=True).limit(limit)
    if channel:
        query = query.eq("channel", _normalize_channel(channel))
    res = query.execute()
    return [_public_account_row(item) for item in (res.data or [])]


def _get_channel_account_raw(client: Client, account_id: str) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    res = client.table("channel_accounts").select("*").eq("id", account_id).limit(1).execute()
    return _single_or_404(res.data or [], "account")


def get_channel_account(client: Client, account_id: str) -> Dict[str, Any]:
    return _public_account_row(_get_channel_account_raw(client, account_id))


def create_channel_account(client: Client, request: ChannelAccountCreateRequest, actor: Actor) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    channel = _normalize_channel(request.channel)
    login_mode = _normalize_login_mode(request.login_mode)
    login_username = (request.login_username or "").strip() or None
    login_password = (request.login_password or "").strip() or None
    if login_mode == "credential":
        if not login_username:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="credential mode requires login_username")
        if not login_password:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="credential mode requires login_password")

    payload = {
        "channel": channel,
        "account_name": request.account_name.strip(),
        "account_handle": (request.account_handle or "").strip() or None,
        "login_mode": login_mode,
        "storage_state_path": (request.storage_state_path or "").strip() or None,
        "user_data_dir": (request.user_data_dir or "").strip() or None,
        "cookies_json": (request.cookies_json or "").strip() or None,
        "login_username": login_username,
        "login_password": login_password,
        "publish_selector": (request.publish_selector or "").strip(),
        "is_active": bool(request.is_active),
        "tags": [str(x).strip() for x in (request.tags or []) if str(x).strip()],
        "config_jsonb": request.config_jsonb or {},
        "notes": (request.notes or "").strip() or None,
        "updated_at": _now_iso(),
    }
    if login_username is not None:
        payload["login_username"] = login_username
    if login_password is not None:
        payload["login_password"] = login_password
    try:
        inserted = client.table("channel_accounts").insert(payload).execute()
    except APIError as exc:
        if login_mode == "credential" and _is_missing_credential_columns(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="credential mode requires migration 007_channel_accounts_credentials.sql",
            ) from exc
        raise
    row = _single_or_404(inserted.data or [], "created account")

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.created",
            target_type="channel_account",
            target_id=row["id"],
            diff_jsonb={"channel": row["channel"], "account_name": row["account_name"]},
        ),
    )
    return _public_account_row(row)


def update_channel_account(
    client: Client,
    account_id: str,
    request: ChannelAccountUpdateRequest,
    actor: Actor,
) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    current = _get_channel_account_raw(client, account_id)

    patch: Dict[str, Any] = {"updated_at": _now_iso()}
    if request.account_name is not None:
        patch["account_name"] = request.account_name.strip()
    if request.account_handle is not None:
        patch["account_handle"] = request.account_handle.strip() or None
    if request.login_mode is not None:
        patch["login_mode"] = _normalize_login_mode(request.login_mode)
    if request.storage_state_path is not None:
        patch["storage_state_path"] = request.storage_state_path.strip() or None
    if request.user_data_dir is not None:
        patch["user_data_dir"] = request.user_data_dir.strip() or None
    if request.cookies_json is not None:
        patch["cookies_json"] = request.cookies_json.strip() or None
    if request.login_username is not None:
        patch["login_username"] = request.login_username.strip() or None
    if request.login_password is not None:
        patch["login_password"] = request.login_password.strip() or None
    if request.publish_selector is not None:
        patch["publish_selector"] = request.publish_selector.strip()
    if request.is_active is not None:
        patch["is_active"] = bool(request.is_active)
    if request.tags is not None:
        patch["tags"] = [str(x).strip() for x in request.tags if str(x).strip()]
    if request.config_jsonb is not None:
        patch["config_jsonb"] = request.config_jsonb
    if request.notes is not None:
        patch["notes"] = request.notes.strip() or None

    next_login_mode = str(patch.get("login_mode") or current.get("login_mode") or "").strip()
    next_login_username = str(patch.get("login_username") or current.get("login_username") or "").strip()
    next_login_password = str(patch.get("login_password") or current.get("login_password") or "").strip()
    if next_login_mode == "credential":
        if not next_login_username:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="credential mode requires login_username")
        if not next_login_password:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="credential mode requires login_password")

    try:
        updated = client.table("channel_accounts").update(patch).eq("id", account_id).execute()
    except APIError as exc:
        if next_login_mode == "credential" and _is_missing_credential_columns(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="credential mode requires migration 007_channel_accounts_credentials.sql",
            ) from exc
        raise
    row = _single_or_404(updated.data or [], "updated account")

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.updated",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={
                "from": {
                    "account_name": current.get("account_name"),
                    "login_mode": current.get("login_mode"),
                    "is_active": current.get("is_active"),
                },
                "to": {
                    "account_name": row.get("account_name"),
                    "login_mode": row.get("login_mode"),
                    "is_active": row.get("is_active"),
                },
            },
        ),
    )
    return _public_account_row(row)


def delete_channel_account(client: Client, account_id: str, actor: Actor) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    current = _get_channel_account_raw(client, account_id)

    _scrub_pipeline_tasks_for_deleted_account(client, account_id)

    client.table("channel_accounts").delete().eq("id", account_id).execute()

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.deleted",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={"account_name": current.get("account_name"), "channel": current.get("channel")},
        ),
    )
    return {"status": "ok", "deleted_id": account_id}


def _scrub_pipeline_tasks_for_deleted_account(client: Client, account_id: str) -> None:
    try:
        rows = (
            client.table("pipeline_tasks")
            .select("id,payload_jsonb,account_id")
            .or_(f"account_id.eq.{account_id},payload_jsonb->>channel_account_id.eq.{account_id}")
            .limit(500)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return

    now = _now_iso()
    for row in rows:
        task_id = str(row.get("id") or "").strip()
        if not task_id:
            continue
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        next_payload = dict(payload)
        deleted_channel_id = str(next_payload.pop("channel_account_id", "") or "").strip() or account_id
        next_payload["deleted_channel_account_id"] = deleted_channel_id
        next_payload["account_binding_status"] = "deleted_account_scrubbed"
        next_payload["account_binding_scrubbed_at"] = now
        try:
            client.table("pipeline_tasks").update(
                {
                    "account_id": None,
                    "payload_jsonb": next_payload,
                    "updated_at": now,
                }
            ).eq("id", task_id).execute()
        except Exception:  # noqa: BLE001
            continue


def _resolve_local_path(raw_path: str) -> Path:
    candidate = Path(raw_path.strip()).expanduser()
    if candidate.is_absolute():
        return candidate
    return (Path.cwd() / candidate).resolve()


def _verify_storage_state(path_text: str, channel: str) -> Dict[str, Any]:
    path = _resolve_local_path(path_text)
    if not path.exists():
        return {"status": "failed", "message": f"文件不存在: {path}"}
    if not path.is_file():
        return {"status": "failed", "message": f"不是文件: {path}"}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "message": f"JSON 解析失败: {exc}", "path": str(path)}

    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    origins = payload.get("origins") if isinstance(payload, dict) else None
    cookie_count = len(cookies) if isinstance(cookies, list) else 0
    origin_count = len(origins) if isinstance(origins, list) else 0
    domain_hint_map = {
        "xiaohongshu": "xiaohongshu.com",
        "wechat_mp": "weixin.qq.com",
        "douyin": "douyin.com",
    }
    domain_hint = domain_hint_map.get(channel, "")
    matched = 0
    if isinstance(cookies, list) and domain_hint:
        matched = len([item for item in cookies if domain_hint in str((item or {}).get("domain") or "")])

    if cookie_count == 0 and origin_count == 0:
        return {
            "status": "warning",
            "message": "文件可读，但 cookies/origins 为空，可能未登录成功。",
            "path": str(path),
            "cookie_count": cookie_count,
            "origin_count": origin_count,
            "matched_domain_cookies": matched,
        }

    return {
        "status": "ok",
        "message": "storage state 可用",
        "path": str(path),
        "cookie_count": cookie_count,
        "origin_count": origin_count,
        "matched_domain_cookies": matched,
    }


def _verify_user_data_dir(path_text: str) -> Dict[str, Any]:
    path = _resolve_local_path(path_text)
    if not path.exists():
        return {"status": "failed", "message": f"目录不存在: {path}"}
    if not path.is_dir():
        return {"status": "failed", "message": f"不是目录: {path}"}

    child_count = len(list(path.iterdir()))
    if child_count == 0:
        return {"status": "warning", "message": "目录存在但为空", "path": str(path), "child_count": child_count}
    return {"status": "ok", "message": "user data dir 可用", "path": str(path), "child_count": child_count}


def _verify_cookies_json(raw_text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw_text)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "message": f"cookies_json 解析失败: {exc}"}

    if not isinstance(payload, list):
        return {"status": "failed", "message": "cookies_json 必须是 JSON 数组"}

    return {"status": "ok", "message": "cookies_json 可用", "cookie_count": len(payload)}


def _verify_credential_login(username: str, password: str, channel: str) -> Dict[str, Any]:
    if not username:
        return {"status": "failed", "message": "login_username 为空"}
    if not password:
        return {"status": "failed", "message": "login_password 为空"}
    if channel != "xiaohongshu":
        return {
            "status": "warning",
            "message": "账号密码模式当前主要用于小红书，其他渠道可能仍需 storage state。",
        }
    return {
        "status": "ok",
        "message": "账号密码已配置；发布时会尝试密码登录（如触发验证码需人工处理）。",
        "username": username,
    }


def verify_channel_account_login_state(client: Client, account_id: str, actor: Actor) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    row = _get_channel_account_raw(client, account_id)

    login_mode = str(row.get("login_mode") or "")
    if login_mode == "storage_state":
        check = _verify_storage_state(str(row.get("storage_state_path") or ""), str(row.get("channel") or ""))
    elif login_mode == "user_data_dir":
        check = _verify_user_data_dir(str(row.get("user_data_dir") or ""))
    elif login_mode == "cookies_json":
        check = _verify_cookies_json(str(row.get("cookies_json") or ""))
    elif login_mode == "credential":
        check = _verify_credential_login(
            str(row.get("login_username") or "").strip(),
            str(row.get("login_password") or ""),
            str(row.get("channel") or ""),
        )
    else:
        check = {"status": "failed", "message": f"未知登录模式: {login_mode}"}

    check_status = str(check.get("status") or "failed")
    check_message = str(check.get("message") or "")

    updated = (
        client.table("channel_accounts")
        .update(
            {
                "last_login_check_at": _now_iso(),
                "last_login_check_status": check_status,
                "last_login_check_message": check_message,
                "updated_at": _now_iso(),
            }
        )
        .eq("id", account_id)
        .execute()
    )
    updated_row = _single_or_404(updated.data or [], "updated account")

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.login_state_checked",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={"status": check_status, "message": check_message},
        ),
    )

    return {
        "status": "ok",
        "check": check,
        "account": _public_account_row(updated_row),
    }


def get_channel_account_strategy(client: Client, account_id: str) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    row = _get_channel_account_raw(client, account_id)
    config = row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
    raw_profile = config.get("strategy_profile") if isinstance(config.get("strategy_profile"), dict) else {}
    return {
        "status": "ok",
        "account_id": account_id,
        "account_name": str(row.get("account_name") or ""),
        "strategy_profile": _normalize_strategy_profile(raw_profile),
        "collection_plan": _normalize_collection_plan(config.get("collection_plan") if isinstance(config.get("collection_plan"), dict) else {}),
        "feedback_plan": _normalize_feedback_plan(config.get("feedback_plan") if isinstance(config.get("feedback_plan"), dict) else {}),
    }


def update_channel_account_strategy(
    client: Client,
    account_id: str,
    request: ChannelAccountStrategyUpsertRequest,
    actor: Actor,
) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    current = _get_channel_account_raw(client, account_id)
    current_config = current.get("config_jsonb") if isinstance(current.get("config_jsonb"), dict) else {}
    previous_profile = (
        current_config.get("strategy_profile")
        if isinstance(current_config.get("strategy_profile"), dict)
        else {}
    )
    previous_feedback_plan = (
        current_config.get("feedback_plan")
        if isinstance(current_config.get("feedback_plan"), dict)
        else {}
    )
    next_profile = _normalize_strategy_profile(request.strategy_profile)
    next_feedback_plan = _normalize_feedback_plan(request.feedback_plan)
    next_config = {
        **current_config,
        "strategy_profile": next_profile,
        "feedback_plan": next_feedback_plan,
    }

    updated = (
        client.table("channel_accounts")
        .update({"config_jsonb": next_config, "updated_at": _now_iso()})
        .eq("id", account_id)
        .execute()
    )
    updated_row = _single_or_404(updated.data or [], "updated account")

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.strategy_profile_updated",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={
                "reason": request.reason,
                "from": {
                    "strategy_profile": _normalize_strategy_profile(previous_profile),
                    "feedback_plan": _normalize_feedback_plan(previous_feedback_plan),
                },
                "to": {
                    "strategy_profile": next_profile,
                    "feedback_plan": next_feedback_plan,
                },
            },
        ),
    )
    return {
        "status": "ok",
        "account": _public_account_row(updated_row),
        "strategy_profile": next_profile,
        "feedback_plan": next_feedback_plan,
    }


def update_channel_account_collection_plan(
    client: Client,
    account_id: str,
    request: ChannelAccountCollectionPlanUpsertRequest,
    actor: Actor,
) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    current = _get_channel_account_raw(client, account_id)
    current_config = current.get("config_jsonb") if isinstance(current.get("config_jsonb"), dict) else {}
    previous_plan = current_config.get("collection_plan") if isinstance(current_config.get("collection_plan"), dict) else {}
    next_plan = _normalize_collection_plan(request.collection_plan)
    next_config = {**current_config, "collection_plan": next_plan}
    updated = (
        client.table("channel_accounts")
        .update({"config_jsonb": next_config, "updated_at": _now_iso()})
        .eq("id", account_id)
        .execute()
    )
    updated_row = _single_or_404(updated.data or [], "updated account")
    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.collection_plan_updated",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={"reason": request.reason, "from": _normalize_collection_plan(previous_plan), "to": next_plan},
        ),
    )
    return {"status": "ok", "account": _public_account_row(updated_row), "collection_plan": next_plan}


def _table_ready(client: Client, table_name: str, column: str = "id") -> bool:
    try:
        client.table(table_name).select(column).limit(1).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, parsed)


def _clean_text_list(values: Any, *, max_items: int = 20, max_len: int = 200) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        result.append(text[:max_len])
        if len(result) >= max_items:
            break
    return result


def _default_collection_steps(queries: list[str]) -> list[Dict[str, Any]]:
    selected = [str(item or "").strip() for item in queries if str(item or "").strip()]
    selected = selected[:3]
    if not selected:
        selected = ["日本移民"]
    steps: list[Dict[str, Any]] = []
    for query in selected:
        steps.append(
            {
                "tool": "mcp_search",
                "limit": 12,
                "scope": "global",
                "query": query,
                "reason": "onboarding seeded hotspot query",
                "label": f"seed:{query}",
            }
        )
    return steps


def _build_onboarding_checklist(
    strategy_profile: Dict[str, Any],
    collection_plan: Dict[str, Any],
    feedback_plan: Dict[str, Any],
) -> Dict[str, bool]:
    focus_keywords = strategy_profile.get("focus_keywords") if isinstance(strategy_profile.get("focus_keywords"), list) else []
    hotspot_queries = strategy_profile.get("hotspot_queries") if isinstance(strategy_profile.get("hotspot_queries"), list) else []
    content_pillars = strategy_profile.get("content_pillars") if isinstance(strategy_profile.get("content_pillars"), list) else []
    schedule = collection_plan.get("daily_schedule") if isinstance(collection_plan.get("daily_schedule"), dict) else {}
    slots = schedule.get("time_slots") if isinstance(schedule.get("time_slots"), list) else []
    checkpoints = feedback_plan.get("checkpoints_hours") if isinstance(feedback_plan.get("checkpoints_hours"), list) else []
    return {
        "goal_set": bool(str(strategy_profile.get("primary_goal") or "").strip()),
        "persona_set": bool(str(strategy_profile.get("persona_name") or "").strip()),
        "positioning_set": bool(str(strategy_profile.get("ip_positioning") or "").strip()),
        "pillar_set": len(content_pillars) > 0,
        "keyword_set": len(focus_keywords) > 0 or len(hotspot_queries) > 0,
        "schedule_set": len(slots) > 0,
        "feedback_set": len(checkpoints) > 0,
    }


def _onboarding_missing_fields(strategy_profile: Dict[str, Any], collection_plan: Dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not str(strategy_profile.get("primary_goal") or "").strip():
        missing.append("primary_goal")
    if not str(strategy_profile.get("persona_name") or "").strip():
        missing.append("persona_name")
    if not str(strategy_profile.get("ip_positioning") or "").strip():
        missing.append("ip_positioning")
    content_pillars = strategy_profile.get("content_pillars") if isinstance(strategy_profile.get("content_pillars"), list) else []
    if not content_pillars:
        missing.append("content_pillars")
    focus_keywords = strategy_profile.get("focus_keywords") if isinstance(strategy_profile.get("focus_keywords"), list) else []
    hotspot_queries = strategy_profile.get("hotspot_queries") if isinstance(strategy_profile.get("hotspot_queries"), list) else []
    if not focus_keywords and not hotspot_queries:
        missing.append("focus_keywords_or_hotspot_queries")
    steps = collection_plan.get("steps") if isinstance(collection_plan.get("steps"), list) else []
    if not steps:
        missing.append("collection_plan.steps")
    return missing


def _latest_sop_snapshot(config_jsonb: Dict[str, Any]) -> Dict[str, Any] | None:
    snapshots = config_jsonb.get("sop_snapshots") if isinstance(config_jsonb.get("sop_snapshots"), list) else []
    if not snapshots:
        return None
    latest = snapshots[-1]
    return latest if isinstance(latest, dict) else None


def _append_sop_snapshot(
    config_jsonb: Dict[str, Any],
    *,
    reason: str,
    changed_fields: list[str],
    actor: str,
    context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    snapshots = config_jsonb.get("sop_snapshots") if isinstance(config_jsonb.get("sop_snapshots"), list) else []
    sanitized_snapshots = [item for item in snapshots if isinstance(item, dict)]
    previous_version = 0
    if sanitized_snapshots:
        previous_version = int(sanitized_snapshots[-1].get("version") or 0)
    version = previous_version + 1
    snapshot = {
        "version": version,
        "captured_at": _now_iso(),
        "reason": str(reason or "checkpoint").strip()[:400],
        "changed_fields": [str(item or "").strip()[:120] for item in changed_fields if str(item or "").strip()][:20],
        "actor": str(actor or "system").strip()[:200],
        "context": context if isinstance(context, dict) else {},
        "strategy_profile": _normalize_strategy_profile(
            config_jsonb.get("strategy_profile") if isinstance(config_jsonb.get("strategy_profile"), dict) else {}
        ),
        "collection_plan": _normalize_collection_plan(
            config_jsonb.get("collection_plan") if isinstance(config_jsonb.get("collection_plan"), dict) else {}
        ),
        "feedback_plan": _normalize_feedback_plan(
            config_jsonb.get("feedback_plan") if isinstance(config_jsonb.get("feedback_plan"), dict) else {}
        ),
        "publish_preferences": deepcopy(config_jsonb.get("publish_preferences") or {}),
    }
    sanitized_snapshots.append(snapshot)
    config_jsonb["sop_snapshots"] = sanitized_snapshots[-30:]
    config_jsonb["sop_latest_version"] = version
    config_jsonb["sop_latest_at"] = snapshot["captured_at"]
    return snapshot


def _collection_day_bounds(collection_plan: Dict[str, Any]) -> tuple[datetime, datetime]:
    schedule = collection_plan.get("daily_schedule")
    zone_name = schedule.get("timezone") if isinstance(schedule, dict) else None
    try:
        local_zone = ZoneInfo(zone_name or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        local_zone = timezone(timedelta(hours=8))
    start = datetime.now(local_zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _current_cli_items(client, *, domain_id: str, account_id: str, collection_plan: dict) -> list[dict]:
    start, end = _collection_day_bounds(collection_plan)
    rows = (client.table('intelligence_items').select('*').eq('domain_id', domain_id).eq('account_id', account_id)
            .gte('captured_at', start.astimezone(timezone.utc).isoformat())
            .lt('captured_at', end.astimezone(timezone.utc).isoformat())
            .order('captured_at', desc=True).limit(1000).execute().data or [])
    now = datetime.now(timezone.utc)
    valid = []
    for row in rows:
        try:
            captured = datetime.fromisoformat(str(row.get('captured_at') or '').replace('Z', '+00:00'))
            if (row.get('deleted_at') or captured.tzinfo is None or captured > now
                    or not start <= captured < end):
                continue
        except (ValueError, TypeError):
            continue
        valid.append(row)
    return valid


def _capture_source_url(capture: dict) -> str | None:
    if not isinstance(capture, dict) or not str(capture.get('body_text') or '').strip():
        return None
    try:
        parts = urlsplit(str(capture.get('source_url') or ''))
        match = re.fullmatch(r'/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?', parts.path)
        if (parts.scheme != 'https' or parts.hostname not in {'www.xiaohongshu.com', 'xiaohongshu.com'}
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.port not in {None, 443} or not match):
            return None
        return 'https://www.xiaohongshu.com/explore/' + match.group(1).lower()
    except ValueError:
        return None


def _cli_evidence_refs(client, *, domain_id: str, account_id: str, collection_plan: dict) -> list[dict]:
    candidates = []
    ids = {'cases': set(), 'assets': set()}
    for item in _current_cli_items(client, domain_id=domain_id, account_id=account_id, collection_plan=collection_plan):
        meta = item.get('meta_jsonb') if isinstance(item.get('meta_jsonb'), dict) else {}
        source = _capture_source_url(meta.get('browser_capture'))
        links = meta.get('knowledge_links') if isinstance(meta.get('knowledge_links'), dict) else {}
        linked_source = _capture_source_url({'body_text': 'linked reference', 'source_url': links.get('source_url')})
        if not source or links.get('status') != 'ok' or linked_source != source:
            continue
        for table, field in (('cases', 'case_ids'), ('assets', 'asset_ids')):
            if isinstance(links.get(field), list):
                ids[table].update(value for value in links[field] if isinstance(value, str))
        candidates.append((item, source, links))
    persisted = {}
    for table in ('cases', 'assets'):
        rows = (client.table(table).select('*').eq('domain_id', domain_id).eq('account_id', account_id)
                .in_('id', sorted(ids[table])).is_('deleted_at', 'null').execute().data or []) if ids[table] else []
        persisted[table] = {row['id']: row for row in rows}
    refs = {}
    for item, source, links in candidates:
        ref = refs.setdefault(source, {'item_id': item['id'], 'source_url': source, 'case_ids': [], 'asset_ids': []})
        for table, field, source_field in (('cases', 'case_ids', 'url'), ('assets', 'asset_ids', 'source')):
            for record_id in links.get(field) if isinstance(links.get(field), list) else []:
                row = persisted[table].get(record_id) if isinstance(record_id, str) else None
                if row and not ref[field] and _capture_source_url({'body_text': 'reference', 'source_url': row.get(source_field)}) == source:
                    ref[field].append(record_id)
    return [ref for ref in refs.values() if ref['case_ids'] or ref['asset_ids']]


def _compute_collection_health(
    client: Client,
    *,
    domain_id: str,
    account_id: str,
    collection_plan: Dict[str, Any],
) -> Dict[str, Any]:
    gate = collection_plan.get("loop_gate") if isinstance(collection_plan.get("loop_gate"), dict) else {}
    min_case_per_day = _collection_threshold(gate.get("min_case_per_day"))
    min_asset_per_day = _collection_threshold(gate.get("min_asset_per_day"))
    start, end = _collection_day_bounds(collection_plan)
    since = start.astimezone(timezone.utc).isoformat()
    until = end.astimezone(timezone.utc).isoformat()

    if collection_plan.get('mode') == 'xhs_cli':
        refs = _cli_evidence_refs(client, domain_id=domain_id, account_id=account_id, collection_plan=collection_plan)
        case_count = len({record_id for ref in refs for record_id in ref['case_ids']})
        asset_count = len({record_id for ref in refs for record_id in ref['asset_ids']})
        passed = case_count >= min_case_per_day and asset_count >= min_asset_per_day
        return {'passed': passed, 'reason': '' if passed else f'collection_insufficient(case={case_count}/{min_case_per_day}, asset={asset_count}/{min_asset_per_day})',
            'since': since, 'counts': {'case': case_count, 'asset': asset_count},
            'thresholds': {'min_case_per_day': min_case_per_day, 'min_asset_per_day': min_asset_per_day}}

    case_count = 0
    asset_count = 0
    if _table_ready(client, "ingestion_logs"):
        query = (
            client.table("ingestion_logs")
            .select("entity_type,status,success_count,normalized_count")
            .eq("domain_id", domain_id)
            .gte("created_at", since)
            .lt("created_at", until)
            .in_("entity_type", ["case", "asset"])
        )
        if account_id:
            query = query.eq("account_id", account_id)
        rows = query.execute().data or []
        for row in rows:
            entity_type = str(row.get("entity_type") or "").strip()
            status_value = str(row.get("status") or "").strip().lower()
            if status_value not in {"success", "partial_failed", "processing", "received"}:
                continue
            success_count = int(row.get("success_count") or 0)
            normalized_count = int(row.get("normalized_count") or 0)
            value = max(success_count, normalized_count)
            if entity_type == "case":
                case_count += value
            elif entity_type == "asset":
                asset_count += value

    if _table_ready(client, "assets"):
        asset_query = (
            client.table("assets")
            .select("id", count="exact")
            .eq("domain_id", domain_id)
            .eq("source_type", "auto_case_asset")
            .gte("created_at", since)
            .lt("created_at", until)
        )
        if account_id:
            asset_query = asset_query.eq("account_id", account_id)
        auto_assets = int(asset_query.execute().count or 0)
        asset_count = max(asset_count, auto_assets)

    passed = case_count >= min_case_per_day and asset_count >= min_asset_per_day
    reason = ""
    if not passed:
        reason = f"collection_insufficient(case={case_count}/{min_case_per_day}, asset={asset_count}/{min_asset_per_day})"

    return {
        "passed": passed,
        "reason": reason,
        "since": since,
        "counts": {"case": case_count, "asset": asset_count},
        "thresholds": {"min_case_per_day": min_case_per_day, "min_asset_per_day": min_asset_per_day},
    }


def _latest_pipeline_task_for_account(client: Client, *, domain_id: str, account_id: str) -> Dict[str, Any] | None:
    if not _table_ready(client, "pipeline_tasks"):
        return None
    rows = (
        client.table("pipeline_tasks")
        .select("*")
        .eq("domain_id", domain_id)
        .eq("account_id", account_id)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _latest_published_task_for_account(client: Client, *, domain_id: str, account_id: str) -> Dict[str, Any] | None:
    if not _table_ready(client, "pipeline_tasks"):
        return None
    rows = (
        client.table("pipeline_tasks")
        .select("*")
        .eq("domain_id", domain_id)
        .eq("account_id", account_id)
        .in_("status", ["published", "metrics_ready", "reflecting", "done", "reflection_failed"])
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _latest_run_for_account(client: Client, *, domain_id: str, account_id: str) -> Dict[str, Any] | None:
    if not _table_ready(client, "runs_daily"):
        return None
    rows = (
        client.table("runs_daily")
        .select("id,run_key,status,flow,result_jsonb,started_at,finished_at,updated_at")
        .eq("domain_id", domain_id)
        .eq("account_id", account_id)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _latest_review_for_account(client: Client, *, domain_id: str, account_id: str) -> Dict[str, Any] | None:
    if not _table_ready(client, "reviews"):
        return None
    rows = (
        client.table("reviews")
        .select("*")
        .eq("domain_id", domain_id)
        .eq("account_id", account_id)
        .is_("deleted_at", "null")
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _compute_exposure_kpi_gate(
    feedback_plan: Dict[str, Any],
    published_task: Dict[str, Any] | None,
) -> Dict[str, Any]:
    gate_cfg = feedback_plan.get("exposure_gate") if isinstance(feedback_plan.get("exposure_gate"), dict) else {}
    min_impressions = _safe_float(gate_cfg.get("min_impressions") or 100.0)
    min_engagement_rate = _safe_float(gate_cfg.get("min_engagement_rate") or 0.03)
    thresholds = {"min_impressions": min_impressions, "min_engagement_rate": min_engagement_rate}

    if not isinstance(published_task, dict):
        return {
            "status": "pending",
            "passed": False,
            "reason": "no_published_task",
            "metric_source": "",
            "impressions": 0.0,
            "interactions": 0.0,
            "engagement_rate": 0.0,
            "thresholds": thresholds,
        }

    metrics = published_task.get("metrics_jsonb") if isinstance(published_task.get("metrics_jsonb"), dict) else {}
    impressions = _safe_float(metrics.get("impressions") or metrics.get("views") or metrics.get("exposure"))
    likes = _safe_float(metrics.get("likes"))
    collects = _safe_float(metrics.get("collects"))
    comments = _safe_float(metrics.get("comments_count") or metrics.get("comments"))
    shares = _safe_float(metrics.get("shares"))
    interactions = likes + collects + comments + shares
    if impressions <= 0:
        return {
            "status": "no_metrics",
            "passed": False,
            "reason": "missing_impressions_metrics",
            "metric_source": "pipeline_tasks.metrics_jsonb",
            "impressions": 0.0,
            "interactions": interactions,
            "engagement_rate": 0.0,
            "thresholds": thresholds,
        }
    engagement_rate = interactions / impressions if impressions > 0 else 0.0
    passed = impressions >= min_impressions and engagement_rate >= min_engagement_rate
    return {
        "status": "ok",
        "passed": passed,
        "reason": "" if passed else "below_exposure_gate",
        "metric_source": "pipeline_tasks.metrics_jsonb",
        "impressions": round(impressions, 4),
        "interactions": round(interactions, 4),
        "engagement_rate": round(engagement_rate, 6),
        "thresholds": thresholds,
    }


def _iso_to_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _map_task_stage(status_value: str) -> str:
    status_normalized = str(status_value or "").strip().lower()
    if status_normalized in {"queued", "intel_ready", "drafting", "review_rejected"}:
        return "drafting"
    if status_normalized == "pending_review":
        return "pending_review"
    if status_normalized in {"approved", "publishing"}:
        return "published"
    if status_normalized in {"published", "metrics_ready", "reflecting", "done", "reflection_failed"}:
        return "feedback_ready"
    return "collecting"


def _upsert_memory_item(
    client: Client,
    *,
    domain_id: str,
    account_id: str,
    title: str,
    content: str,
    tags: list[str],
    created_by: str,
) -> str | None:
    if not _table_ready(client, "memory_items"):
        return None
    normalized_title = str(title or "").strip()[:200]
    normalized_content = str(content or "").strip()[:4000]
    if not normalized_title or not normalized_content:
        return None
    existing = (
        client.table("memory_items")
        .select("id,content")
        .eq("domain_id", domain_id)
        .eq("account_id", account_id)
        .eq("type", "strategy_rule")
        .eq("title", normalized_title)
        .eq("status", "active")
        .limit(1)
        .execute()
        .data
        or []
    )
    payload = {
        "domain_id": domain_id,
        "account_id": account_id,
        "type": "strategy_rule",
        "title": normalized_title,
        "content": normalized_content,
        "tags": [str(item or "").strip()[:80] for item in tags if str(item or "").strip()][:20],
        "status": "active",
        "confidence": 0.9,
        "created_by": created_by,
        "updated_at": _now_iso(),
    }
    if existing:
        row_id = str(existing[0].get("id") or "")
        client.table("memory_items").update(payload).eq("id", row_id).execute()
        return row_id or None
    inserted = client.table("memory_items").insert(payload).execute().data or []
    if not inserted:
        return None
    return str(inserted[0].get("id") or "") or None


def _create_review_record_for_task(
    client: Client,
    *,
    domain_id: str,
    account_id: str,
    pipeline_task_id: str,
    action: str,
    reason: str,
    created_by: str,
) -> str | None:
    if not _table_ready(client, "reviews"):
        return None
    task = get_pipeline_task(client, pipeline_task_id)
    payload_jsonb = task.get("payload_jsonb") if isinstance(task.get("payload_jsonb"), dict) else {}
    title = str(payload_jsonb.get("title") or "").strip()
    content_ref = str(payload_jsonb.get("url") or "").strip()
    if not content_ref:
        content_ref = title[:300]
    metrics = task.get("metrics_jsonb") if isinstance(task.get("metrics_jsonb"), dict) else {}
    success_points = "人工审核通过" if action == "approve" else ""
    failure_points = reason if action == "reject" else ""
    improvement = reason if action == "reject" else ""
    summary_text = f"review_action:{action}"
    pipeline_task_ref = pipeline_task_id if _table_ready(client, "pipeline_tasks") else None
    created = (
        client.table("reviews")
        .insert(
            {
                "domain_id": domain_id,
                "account_id": account_id,
                "pipeline_task_id": pipeline_task_ref,
                "content_item_ref": content_ref,
                "platform": str(task.get("channel") or "xiaohongshu"),
                "metrics": metrics,
                "success_points": success_points,
                "failure_points": failure_points,
                "improvement": improvement,
                "summary_text": summary_text,
                "source_type": "system_review_action",
                "source_ref": f"review_action:{pipeline_task_id}:{int(time.time())}",
                "created_by": created_by,
            }
        )
        .execute()
        .data
        or []
    )
    if not created:
        return None
    return str(created[0].get("id") or "") or None


def commit_account_onboarding(
    client: Client,
    account_id: str,
    request: AccountOnboardingCommitRequest,
    actor: Actor,
) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    current = _get_channel_account_raw(client, account_id)
    current_config = current.get("config_jsonb") if isinstance(current.get("config_jsonb"), dict) else {}
    current_strategy = (
        current_config.get("strategy_profile") if isinstance(current_config.get("strategy_profile"), dict) else {}
    )
    current_collection = (
        current_config.get("collection_plan") if isinstance(current_config.get("collection_plan"), dict) else {}
    )
    current_feedback = (
        current_config.get("feedback_plan") if isinstance(current_config.get("feedback_plan"), dict) else {}
    )

    strategy_seed: Dict[str, Any] = {
        **current_strategy,
        "primary_goal": request.primary_goal,
        "persona_name": request.persona_name,
        "ip_positioning": request.ip_positioning,
        "tone_style": request.tone_style or current_strategy.get("tone_style") or "",
        "cta_style": request.cta_style or current_strategy.get("cta_style") or "",
    }
    if request.audience:
        strategy_seed["audience"] = _clean_text_list(request.audience, max_items=8, max_len=120)
    if request.pain_points:
        strategy_seed["pain_points"] = _clean_text_list(request.pain_points, max_items=8, max_len=120)
    if request.content_pillars:
        strategy_seed["content_pillars"] = _clean_text_list(request.content_pillars, max_items=8, max_len=120)
    if request.forbidden_claims:
        strategy_seed["forbidden_claims"] = _clean_text_list(request.forbidden_claims, max_items=16, max_len=120)
    if request.publish_constraints:
        strategy_seed["publish_constraints"] = _clean_text_list(request.publish_constraints, max_items=20, max_len=200)

    focus_keywords = _clean_text_list(request.focus_keywords, max_items=20, max_len=120)
    hotspot_queries = _clean_text_list(request.hotspot_queries, max_items=20, max_len=120)
    viewpoint_queries = _clean_text_list(request.viewpoint_queries, max_items=20, max_len=120)
    if focus_keywords:
        strategy_seed["focus_keywords"] = focus_keywords
    if hotspot_queries:
        strategy_seed["hotspot_queries"] = hotspot_queries
    if viewpoint_queries:
        strategy_seed["viewpoint_queries"] = viewpoint_queries
    if not strategy_seed.get("hotspot_queries"):
        strategy_seed["hotspot_queries"] = strategy_seed.get("focus_keywords") or ["日本移民"]
    if not strategy_seed.get("viewpoint_queries"):
        strategy_seed["viewpoint_queries"] = strategy_seed.get("focus_keywords") or ["日本移民争议"]

    next_strategy_profile = _normalize_strategy_profile(strategy_seed)
    hotspot_seed = (
        next_strategy_profile.get("hotspot_queries")
        if isinstance(next_strategy_profile.get("hotspot_queries"), list)
        else []
    )
    generated_steps = _default_collection_steps(hotspot_seed)
    first_focus_keyword = (
        (next_strategy_profile.get("focus_keywords") or ["日本移民"])[0]
        if isinstance(next_strategy_profile.get("focus_keywords"), list)
        else "日本移民"
    )
    next_collection_plan = _normalize_collection_plan(
        {
            **current_collection,
            "mode": "hybrid",
            "steps": generated_steps,
            "fallback": [
                {
                    "tool": "playwright_account_search",
                    "limit": 8,
                    "scope": "account",
                    "query": first_focus_keyword,
                    "reason": "fallback when mcp is unavailable",
                }
            ],
            "daily_schedule": {
                "posts_per_day": request.posts_per_day,
                "time_slots": _clean_text_list(request.publish_time_slots, max_items=8, max_len=20) or ["10:00", "20:00"],
                "timezone": "Asia/Shanghai",
            },
            "loop_gate": {
                "min_case_per_day": request.min_case_per_day,
                "min_asset_per_day": request.min_asset_per_day,
            },
            "topic_refresh": {
                "window_days": 7,
                "refresh_every_hours": 24,
                "source_priority": ["case", "asset"],
            },
            "notes": "single-account onboarding generated plan",
        }
    )
    next_feedback_plan = _normalize_feedback_plan(
        {
            **current_feedback,
            "checkpoints_hours": request.checkpoints_hours,
        }
    )
    checklist = _build_onboarding_checklist(next_strategy_profile, next_collection_plan, next_feedback_plan)
    onboarding_ready = all(bool(value) for value in checklist.values())
    onboarding = {
        "ready": onboarding_ready,
        "locked": bool(request.lock_onboarding),
        "domain_slug": request.domain_slug,
        "committed_at": _now_iso(),
        "committed_by": actor.user_id,
        "reason": request.reason,
        "checklist": checklist,
    }

    next_config = {
        **current_config,
        "strategy_profile": next_strategy_profile,
        "collection_plan": next_collection_plan,
        "feedback_plan": next_feedback_plan,
        "onboarding": onboarding,
        "single_account_mode": True,
    }
    snapshot = _append_sop_snapshot(
        next_config,
        reason="onboarding_commit",
        changed_fields=["strategy_profile", "collection_plan", "feedback_plan", "onboarding"],
        actor=actor.user_id,
        context={"domain_slug": request.domain_slug, "reason": request.reason},
    )
    updated = (
        client.table("channel_accounts")
        .update({"config_jsonb": next_config, "updated_at": _now_iso()})
        .eq("id", account_id)
        .execute()
    )
    row = _single_or_404(updated.data or [], "updated account")

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.onboarding_committed",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={
                "reason": request.reason,
                "domain_slug": request.domain_slug,
                "onboarding_ready": onboarding_ready,
                "snapshot_version": snapshot.get("version"),
            },
        ),
    )
    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": request.domain_slug,
        "onboarding_ready": onboarding_ready,
        "strategy_profile": next_strategy_profile,
        "collection_plan": next_collection_plan,
        "feedback_plan": next_feedback_plan,
        "sop_snapshot": snapshot,
        "account": _public_account_row(row),
    }


def get_account_loop_status(client: Client, *, account_id: str, domain_slug: str) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    account = _get_channel_account_raw(client, account_id)
    config = account.get("config_jsonb") if isinstance(account.get("config_jsonb"), dict) else {}
    strategy_profile = _normalize_strategy_profile(
        config.get("strategy_profile") if isinstance(config.get("strategy_profile"), dict) else {}
    )
    collection_plan = _normalize_collection_plan(
        config.get("collection_plan") if isinstance(config.get("collection_plan"), dict) else {}
    )
    feedback_plan = _normalize_feedback_plan(
        config.get("feedback_plan") if isinstance(config.get("feedback_plan"), dict) else {}
    )
    onboarding_cfg = config.get("onboarding") if isinstance(config.get("onboarding"), dict) else {}
    missing_fields = _onboarding_missing_fields(strategy_profile, collection_plan)
    onboarding_ready = bool(onboarding_cfg.get("ready")) and not missing_fields

    domain_id = _resolve_domain_id(client, domain_slug)
    collection_health = _compute_collection_health(
        client,
        domain_id=domain_id,
        account_id=account_id,
        collection_plan=collection_plan,
    )
    latest_task = _latest_pipeline_task_for_account(client, domain_id=domain_id, account_id=account_id)
    latest_run = _latest_run_for_account(client, domain_id=domain_id, account_id=account_id)
    latest_published = _latest_published_task_for_account(client, domain_id=domain_id, account_id=account_id)
    latest_review = _latest_review_for_account(client, domain_id=domain_id, account_id=account_id)
    kpi_gate = _compute_exposure_kpi_gate(feedback_plan, latest_published)
    latest_snapshot = _latest_sop_snapshot(config)

    stage = "collecting"
    blockers: list[str] = []
    if not onboarding_ready:
        stage = "onboarding_ready"
        if missing_fields:
            blockers.append(f"onboarding_missing:{','.join(missing_fields)}")
    elif not collection_health.get("passed"):
        stage = "collecting"
        blockers.append(str(collection_health.get("reason") or "collection_insufficient"))
    elif latest_task:
        stage = _map_task_stage(str(latest_task.get("status") or ""))
    elif latest_run and str(latest_run.get("status") or "") in {"CREATED", "COLLECTING", "COLLECTED", "EVIDENCE_READY", "PLANNING"}:
        stage = "collecting"

    if stage == "feedback_ready":
        if kpi_gate.get("status") != "ok":
            stage = "published"
        else:
            review_dt = _iso_to_dt((latest_review or {}).get("updated_at")) if latest_review else None
            snapshot_dt = _iso_to_dt((latest_snapshot or {}).get("captured_at")) if latest_snapshot else None
            if review_dt and snapshot_dt and snapshot_dt >= review_dt:
                stage = "sop_updated"
            elif review_dt:
                stage = "feedback_ready"
            else:
                stage = "feedback_ready"
    if stage == "sop_updated" and collection_health.get("passed"):
        stage = "next_cycle_ready"

    progress = {
        "onboarding_ready": onboarding_ready,
        "collecting": bool(collection_health.get("passed")),
        "drafting": bool(latest_task and str(latest_task.get("status") or "") in {"queued", "intel_ready", "drafting", "review_rejected"}),
        "pending_review": bool(latest_task and str(latest_task.get("status") or "") == "pending_review"),
        "published": bool(latest_published),
        "feedback_ready": bool(kpi_gate.get("status") == "ok"),
        "sop_updated": stage in {"sop_updated", "next_cycle_ready"},
        "next_cycle_ready": stage == "next_cycle_ready",
    }
    completion_score = round((sum(1 for value in progress.values() if value) / max(1, len(progress))) * 100.0, 2)
    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "loop": {
            "stage": stage,
            "onboarding_ready": onboarding_ready,
            "blockers": blockers,
            "progress": progress,
            "collection_health": collection_health,
            "kpi_gate": kpi_gate,
            "latest": {
                "task": latest_task,
                "run": latest_run,
                "review": latest_review,
            },
            "completion_score": completion_score,
        },
        "sop_latest": latest_snapshot,
    }


def _list_sop_snapshots(config_jsonb: Dict[str, Any], *, limit: int = 10) -> list[Dict[str, Any]]:
    raw = config_jsonb.get("sop_snapshots") if isinstance(config_jsonb.get("sop_snapshots"), list) else []
    sanitized: list[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        changed = item.get("changed_fields") if isinstance(item.get("changed_fields"), list) else []
        sanitized.append(
            {
                "version": int(item.get("version") or 0),
                "captured_at": str(item.get("captured_at") or ""),
                "reason": str(item.get("reason") or ""),
                "changed_fields": [str(field or "").strip() for field in changed if str(field or "").strip()],
                "actor": str(item.get("actor") or ""),
                "context": item.get("context") if isinstance(item.get("context"), dict) else {},
                "strategy_profile": _normalize_strategy_profile(
                    item.get("strategy_profile") if isinstance(item.get("strategy_profile"), dict) else {}
                ),
                "collection_plan": _normalize_collection_plan(
                    item.get("collection_plan") if isinstance(item.get("collection_plan"), dict) else {}
                ),
                "feedback_plan": _normalize_feedback_plan(
                    item.get("feedback_plan") if isinstance(item.get("feedback_plan"), dict) else {}
                ),
                **({"publish_preferences": deepcopy(item["publish_preferences"])} if "publish_preferences" in item else {}),
            }
        )
    sanitized.sort(key=lambda row: int(row.get("version") or 0), reverse=True)
    safe_limit = max(1, min(limit, 50))
    return sanitized[:safe_limit]


def get_account_sop_snapshots(client: Client, *, account_id: str, domain_slug: str, limit: int = 10) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    row = _get_channel_account_raw(client, account_id)
    _resolve_domain_id(client, domain_slug)
    config = row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
    snapshots = _list_sop_snapshots(config, limit=limit)
    current_version = int(config.get("sop_latest_version") or (snapshots[0].get("version") if snapshots else 0) or 0)
    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "current_version": current_version,
        "items": snapshots,
    }


def get_account_sop_current(client: Client, *, account_id: str, domain_slug: str) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    row = _get_channel_account_raw(client, account_id)
    _resolve_domain_id(client, domain_slug)
    config = row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
    snapshots = _list_sop_snapshots(config, limit=1)
    latest = snapshots[0] if snapshots else None
    prompt_rows = list_prompt_versions(client, account_id, domain_slug=domain_slug, limit=80)
    active_prompt_rows = [item for item in prompt_rows if str(item.get("status") or "").strip().lower() == "active"]
    prompt_summary = [
        {
            "id": str(item.get("id") or ""),
            "agent_name": str(item.get("agent_name") or ""),
            "version": str(item.get("version") or ""),
            "status": str(item.get("status") or ""),
            "updated_at": str(item.get("updated_at") or ""),
        }
        for item in active_prompt_rows
    ]
    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "sop_latest": latest,
        "prompt_versions": prompt_summary,
    }


def _load_kb_rows(
    client: Client,
    *,
    table: str,
    domain_id: str,
    account_id: str,
    select_fields: str,
    limit: int = 80,
) -> list[Dict[str, Any]]:
    if not _table_ready(client, table):
        return []
    query = client.table(table).select(select_fields).eq("domain_id", domain_id).eq("status", "active").limit(limit)
    rows = query.execute().data or []
    account_specific = [row for row in rows if str(row.get("account_id") or "").strip() == account_id]
    domain_level = [row for row in rows if not str(row.get("account_id") or "").strip()]
    return account_specific + domain_level


def _safe_str_list(value: Any, *, prefix_hash: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if prefix_hash and not text.startswith("#"):
            text = f"#{text}"
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _active_prompt_map(client: Client, *, account_id: str, domain_slug: str) -> dict[str, Dict[str, Any]]:
    prompt_rows = list_prompt_versions(client, account_id, domain_slug=domain_slug, limit=80)
    return {
        str(row.get("agent_name") or "").strip(): row
        for row in prompt_rows
        if str(row.get("status") or "").strip().lower() == "active"
    }


def _prompt_meta(prompt_row: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(prompt_row, dict):
        return {
            "model_name": "",
            "temperature": None,
            "prompt_source": "未单独配置",
            "prompt_version": "",
            "prompt_preview": "",
        }
    evidence = prompt_row.get("evidence_jsonb") if isinstance(prompt_row.get("evidence_jsonb"), dict) else {}
    model_name = str(evidence.get("model") or "").strip()
    temperature_raw = evidence.get("temperature")
    try:
        temperature = float(temperature_raw) if temperature_raw is not None else None
    except (TypeError, ValueError):
        temperature = None
    system_prompt = str(prompt_row.get("system_prompt") or "").strip()
    return {
        "model_name": model_name,
        "temperature": temperature,
        "prompt_source": str(prompt_row.get("source") or "manual").strip() or "manual",
        "prompt_version": str(prompt_row.get("version") or "").strip(),
        "prompt_preview": system_prompt[:600],
    }


def get_account_agent_config_view(client: Client, *, account_id: str, domain_slug: str) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    row = _get_channel_account_raw(client, account_id)
    domain_id = _resolve_domain_id(client, domain_slug)
    config = row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
    strategy_profile = _normalize_strategy_profile(config.get("strategy_profile") if isinstance(config.get("strategy_profile"), dict) else {})
    collection_plan = _normalize_collection_plan(config.get("collection_plan") if isinstance(config.get("collection_plan"), dict) else {})
    prompt_map = _active_prompt_map(client, account_id=account_id, domain_slug=domain_slug)

    kb_playbooks = _load_kb_rows(
        client,
        table="kb_playbooks",
        domain_id=domain_id,
        account_id=account_id,
        select_fields="account_id,playbook_code,playbook_name,stage,objective,method_steps",
    )
    kb_rulebooks = _load_kb_rows(
        client,
        table="kb_rulebooks",
        domain_id=domain_id,
        account_id=account_id,
        select_fields="account_id,rule_code,rule_name,rule_type,applies_to",
    )
    kb_io_rules = _load_kb_rows(
        client,
        table="kb_io_rules",
        domain_id=domain_id,
        account_id=account_id,
        select_fields="account_id,rule_code,direction,entity_type,target_agent",
    )

    method_names_by_stage: dict[str, list[str]] = {}
    for item in kb_playbooks:
        stage = str(item.get("stage") or "").strip()
        name = str(item.get("playbook_name") or item.get("playbook_code") or "").strip()
        if stage and name:
            method_names_by_stage.setdefault(stage, [])
            if name not in method_names_by_stage[stage]:
                method_names_by_stage[stage].append(name)

    rule_names_by_apply: dict[str, list[str]] = {}
    for item in kb_rulebooks:
        applies_to = _safe_str_list(item.get("applies_to"))
        name = str(item.get("rule_name") or item.get("rule_code") or "").strip()
        for key in applies_to:
            rule_names_by_apply.setdefault(key, [])
            if name and name not in rule_names_by_apply[key]:
                rule_names_by_apply[key].append(name)

    tools_by_agent: dict[str, list[str]] = {
        "collector_agent": [],
        "analysis_agent": [],
        "copy_agent": [],
        "review_agent": [],
        "coach_orchestrator": ["用户目标理解", "动作确认门控", "记忆回写"],
    }
    for step in collection_plan.get("steps") if isinstance(collection_plan.get("steps"), list) else []:
        tool = str((step or {}).get("tool") or "").strip()
        if not tool:
            continue
        if tool == "mcp_search":
            label = "公开热点检索"
        elif tool == "mcp_home":
            label = "公开首页观察"
        elif tool == "mcp_profile":
            label = "主页只读补采"
        elif tool == "playwright_account_search":
            label = "账号侧浏览器采集"
        elif tool == "playwright_profile":
            label = "账号主页采集"
        elif tool == "playwright_post_detail":
            label = "帖子详情读取"
        else:
            label = tool
        if label not in tools_by_agent["collector_agent"]:
            tools_by_agent["collector_agent"].append(label)
    if "热点样本拆解" not in tools_by_agent["analysis_agent"]:
        tools_by_agent["analysis_agent"] = ["样本结构拆解", "方法卡提炼", "证据对齐"]
    if "完整帖子生成" not in tools_by_agent["copy_agent"]:
        tools_by_agent["copy_agent"] = ["完整帖子生成", "标题候选生成", "标签与CTA编写"]
    if "反馈回收" not in tools_by_agent["review_agent"]:
        tools_by_agent["review_agent"] = ["反馈回收", "对标复盘", "策略建议生成"]

    for item in kb_io_rules:
        target = str(item.get("target_agent") or "").strip()
        if not target:
            continue
        direction = str(item.get("direction") or "").strip()
        entity_type = str(item.get("entity_type") or "").strip()
        label = f"{'入库' if direction == 'ingest' else '出库'} {entity_type}"
        tools_by_agent.setdefault(target, [])
        if label not in tools_by_agent[target]:
            tools_by_agent[target].append(label)

    focus_keywords = _safe_str_list(strategy_profile.get("focus_keywords"))
    hotspot_queries = _safe_str_list(strategy_profile.get("hotspot_queries"))
    forbidden_claims = _safe_str_list(strategy_profile.get("forbidden_claims"))

    nodes = []
    coach_prompt = _prompt_meta(prompt_map.get("coach_orchestrator"))
    nodes.append(
        {
            "agent_key": "coach_orchestrator",
            "label": "总教练",
            "role_summary": "理解用户目标、拆成动作、调度 4 个子 Agent，并在关键门控点请求确认。",
            **coach_prompt,
            "upstream_inputs": ["用户对话", "账号定位", "当前闭环状态", "上一轮复盘结论"],
            "downstream_outputs": ["今日动作", "确认请求", "对子 Agent 的调度指令"],
            "allowed_tools": tools_by_agent.get("coach_orchestrator", []),
            "knowledge_sources": ["账号策略", "SOP 快照", "记忆库"],
            "notes": ["用户只面对这一个入口。", "不直接静默修改策略。"],
        }
    )

    analysis_prompt = _prompt_meta(prompt_map.get("analysis_agent") or prompt_map.get("hot_post_analysis"))
    nodes.append(
        {
            "agent_key": "analysis_agent",
            "label": "分析子 Agent",
            "role_summary": "拆解真实样本，提取标题钩子、结构节奏、风险边界和可复用方法。",
            **analysis_prompt,
            "upstream_inputs": ["采集样本", "互动指标", "规则库", "分析方法库"],
            "downstream_outputs": ["分析摘要", "方法卡", "选题角度", "风险清单"],
            "allowed_tools": tools_by_agent.get("analysis_agent", []),
            "knowledge_sources": method_names_by_stage.get("analysis", []) + rule_names_by_apply.get("analysis", []),
            "notes": [
                f"当前关注关键词：{' / '.join(focus_keywords[:4]) or '待补充'}",
                "不直接写稿，只给证据化拆解结果。",
            ],
        }
    )

    collector_prompt = _prompt_meta(prompt_map.get("collector_agent"))
    nodes.append(
        {
            "agent_key": "collector_agent",
            "label": "采集子 Agent",
            "role_summary": "获取真实输入，不做策略判断，优先保证样本可追溯和可复查。",
            **collector_prompt,
            "upstream_inputs": ["账号采集计划", "热点关键词", "补采触发动作"],
            "downstream_outputs": ["公共样本", "账号样本", "采集日志", "待分析输入"],
            "allowed_tools": tools_by_agent.get("collector_agent", []),
            "knowledge_sources": method_names_by_stage.get("collect", []),
            "notes": [
                f"热点查询：{' / '.join(hotspot_queries[:4]) or '待补充'}",
                f"采集模式：{str(collection_plan.get('mode') or 'hybrid')}",
            ],
        }
    )

    copy_prompt = _prompt_meta(prompt_map.get("copy_agent") or prompt_map.get("draft_writer") or prompt_map.get("rebuild_copy"))
    nodes.append(
        {
            "agent_key": "copy_agent",
            "label": "文案子 Agent",
            "role_summary": "根据分析结果产出可直接发布的完整帖子，并说明为什么这么写。",
            **copy_prompt,
            "upstream_inputs": ["分析摘要", "账号定位", "正文结构方法", "标题模板"],
            "downstream_outputs": ["标题", "正文", "标签", "CTA", "写作说明"],
            "allowed_tools": tools_by_agent.get("copy_agent", []),
            "knowledge_sources": method_names_by_stage.get("copy", []) + rule_names_by_apply.get("copy", []),
            "notes": [
                f"禁用表达：{' / '.join(forbidden_claims[:4]) or '待补充'}",
                "必须输出完整帖子，不只给提纲。",
            ],
        }
    )

    review_prompt = _prompt_meta(prompt_map.get("review_agent") or prompt_map.get("reflection"))
    nodes.append(
        {
            "agent_key": "review_agent",
            "label": "复盘子 Agent",
            "role_summary": "基于真实可见指标做复盘，对比对标样本，形成下一轮可执行调整建议。",
            **review_prompt,
            "upstream_inputs": ["发布后指标", "历史中位值", "对标样本", "当前 SOP"],
            "downstream_outputs": ["复盘结论", "保留动作", "调整动作", "废弃动作"],
            "allowed_tools": tools_by_agent.get("review_agent", []),
            "knowledge_sources": method_names_by_stage.get("review", []) + rule_names_by_apply.get("review", []),
            "notes": [
                "主看点赞、收藏、评论、转发。",
                "曝光/播放只做辅助解释，不单独判定优劣。",
            ],
        }
    )

    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "primary_entry": "coach",
        "coordinator": "coach_orchestrator",
        "nodes": nodes,
    }


def get_account_loop_execution_view(client: Client, *, account_id: str, domain_slug: str) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    row = _get_channel_account_raw(client, account_id)
    domain_id = _resolve_domain_id(client, domain_slug)
    loop_status = get_account_loop_status(client, account_id=account_id, domain_slug=domain_slug)
    loop = loop_status.get("loop") if isinstance(loop_status.get("loop"), dict) else {}
    config = row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
    strategy_profile = _normalize_strategy_profile(config.get("strategy_profile") if isinstance(config.get("strategy_profile"), dict) else {})
    collection_plan = _normalize_collection_plan(config.get("collection_plan") if isinstance(config.get("collection_plan"), dict) else {})
    feedback_plan = _normalize_feedback_plan(config.get("feedback_plan") if isinstance(config.get("feedback_plan"), dict) else {})

    counts = loop.get("collection_health", {}).get("counts") if isinstance(loop.get("collection_health"), dict) else {}
    thresholds = loop.get("collection_health", {}).get("thresholds") if isinstance(loop.get("collection_health"), dict) else {}
    latest_task = loop.get("latest", {}).get("task") if isinstance(loop.get("latest"), dict) else {}
    latest_review = loop.get("latest", {}).get("review") if isinstance(loop.get("latest"), dict) else {}
    latest_snapshot = loop_status.get("sop_latest") if isinstance(loop_status.get("sop_latest"), dict) else {}

    pending_review_count = 0
    if _table_ready(client, "pipeline_tasks"):
        try:
            pending_review_count = int(
                client.table("pipeline_tasks")
                .select("id", count="exact")
                .eq("domain_id", domain_id)
                .eq("account_id", account_id)
                .eq("status", "pending_review")
                .limit(1)
                .execute()
                .count
                or 0
            )
        except Exception:
            pending_review_count = 0

    steps = [
        {
            "step_key": "collect",
            "label": "采集",
            "status": "done" if bool(loop.get("collection_health", {}).get("passed")) else "running",
            "summary": "先补齐真实输入，再进入分析。",
            "method_summary": [
                f"公开样本 {int(counts.get('case') or 0)} / 阈值 {int(thresholds.get('case') or 0)}",
                f"方法卡素材 {int(counts.get('asset') or 0)} / 阈值 {int(thresholds.get('asset') or 0)}",
            ],
            "evidence_summary": [
                f"热点关键词：{' / '.join(_safe_str_list(strategy_profile.get('hotspot_queries'))[:3]) or '待补充'}",
                f"采集计划：{str(collection_plan.get('mode') or 'hybrid')}",
            ],
            "result_summary": [
                "采集结果进入案例库和方法卡输入池。",
            ],
        },
        {
            "step_key": "analysis",
            "label": "分析",
            "status": "done" if int(counts.get("asset") or 0) > 0 else "waiting",
            "summary": "把样本拆成可复用方法，而不是直接写稿。",
            "method_summary": [
                "拆标题钩子、开头句型、结构节奏、互动引导。",
                "先给证据，再给判断。",
            ],
            "evidence_summary": [
                f"关注主题：{' / '.join(_safe_str_list(strategy_profile.get('content_pillars'))[:3]) or '待补充'}",
            ],
            "result_summary": [
                f"当前已沉淀方法卡 {int(counts.get('asset') or 0)} 条。",
            ],
        },
        {
            "step_key": "copy",
            "label": "文案生成",
            "status": "done" if str(latest_task.get('status') or '') in {'pending_review', 'approved', 'published', 'done'} else "waiting",
            "summary": "输出完整帖子：标题、正文、标签、CTA、写作说明。",
            "method_summary": [
                "正文结构：结论先行 -> 条件边界 -> 操作步骤 -> 风险提醒 -> 互动引导。",
                "标题优先用结论型/反直觉型/清单型。",
            ],
            "evidence_summary": [
                f"当前主目标：{str(strategy_profile.get('primary_goal') or '待补充')}",
                f"当前语气：{str(strategy_profile.get('tone_style') or '待补充')}",
            ],
            "result_summary": [
                f"最新任务状态：{str(latest_task.get('status') or '暂无任务')}",
            ],
        },
        {
            "step_key": "review",
            "label": "审核与发布前确认",
            "status": "running" if pending_review_count > 0 else "waiting",
            "summary": "人工确认是否可发、需改还是重写。",
            "method_summary": [
                f"待审核数量：{pending_review_count}",
                "驳回时必须写清原因，供下一轮学习。",
            ],
            "evidence_summary": [
                f"最近草稿：{str(latest_task.get('payload_jsonb', {}).get('title') or latest_task.get('id') or '暂无')}" if isinstance(latest_task.get('payload_jsonb'), dict) else "最近草稿：暂无",
            ],
            "result_summary": ["本步骤不自动越过人工门控。"],
        },
        {
            "step_key": "review_feedback",
            "label": "发布后复盘",
            "status": "done" if latest_review else "waiting",
            "summary": "回看真实指标和对标样本，找出下一轮应改哪里。",
            "method_summary": [
                f"回收时间窗：{' / '.join(str(v) for v in (_safe_str_list(feedback_plan.get('checkpoints_hours')) or []))}" if isinstance(feedback_plan.get("checkpoints_hours"), list) else "回收时间窗：1 / 3 / 24",
                "主看点赞、收藏、评论、转发。",
            ],
            "evidence_summary": [
                f"最近复盘：{str(latest_review.get('summary_text') or '暂无')[:60] if latest_review else '暂无复盘'}",
            ],
            "result_summary": [
                f"SOP 最新版本：v{int(latest_snapshot.get('version') or 0) if latest_snapshot else 0}",
            ],
        },
    ]

    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "current_stage": str(loop.get("stage") or ""),
        "completion_score": float(loop.get("completion_score") or 0),
        "steps": steps,
        "latest_summary": {
            "account_name": str(row.get("account_name") or ""),
            "persona_name": str(strategy_profile.get("persona_name") or ""),
            "goal": str(strategy_profile.get("primary_goal") or ""),
            "latest_task_status": str(latest_task.get("status") or ""),
            "latest_review_summary": str(latest_review.get("summary_text") or "") if latest_review else "",
        },
    }


def get_account_content_method_view(client: Client, *, account_id: str, domain_slug: str) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    row = _get_channel_account_raw(client, account_id)
    domain_id = _resolve_domain_id(client, domain_slug)
    config = row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
    strategy_profile = _normalize_strategy_profile(config.get("strategy_profile") if isinstance(config.get("strategy_profile"), dict) else {})
    collection_plan = _normalize_collection_plan(config.get("collection_plan") if isinstance(config.get("collection_plan"), dict) else {})
    feedback_plan = _normalize_feedback_plan(config.get("feedback_plan") if isinstance(config.get("feedback_plan"), dict) else {})
    rulebooks = _load_kb_rows(
        client,
        table="kb_rulebooks",
        domain_id=domain_id,
        account_id=account_id,
        select_fields="account_id,rule_name,rule_type,rule_text",
    )
    playbooks = _load_kb_rows(
        client,
        table="kb_playbooks",
        domain_id=domain_id,
        account_id=account_id,
        select_fields="account_id,playbook_name,stage,objective,method_steps,quality_checks",
    )

    def _playbook(stage: str) -> Dict[str, Any]:
        for item in playbooks:
            if str(item.get("stage") or "").strip() == stage:
                return item
        return {}

    collect_playbook = _playbook("collect")
    analysis_playbook = _playbook("analysis")
    copy_playbook = _playbook("copy")
    review_playbook = _playbook("review")

    quality_rule = next((row for row in rulebooks if str(row.get("rule_type") or "") == "quality_gate"), {})
    narrative_rule = next((row for row in rulebooks if str(row.get("rule_type") or "") == "style"), {})
    safety_rule = next((row for row in rulebooks if str(row.get("rule_type") or "") == "platform_policy"), {})

    sections = [
        {
            "section_key": "collect_sources",
            "title": "采集从哪里拿",
            "summary": "先拿公开热点，再补账号相关样本，所有输入都要可追溯。",
            "bullets": [
                f"热点关键词：{' / '.join(_safe_str_list(strategy_profile.get('hotspot_queries'))[:5]) or '待补充'}",
                f"观点关键词：{' / '.join(_safe_str_list(strategy_profile.get('viewpoint_queries'))[:5]) or '待补充'}",
                f"执行顺序：{' -> '.join(str((step or {}).get('tool') or '') for step in (collection_plan.get('steps') or []) if str((step or {}).get('tool') or '').strip()) or '待补充'}",
                *[str(item) for item in ((collect_playbook.get("method_steps") if isinstance(collect_playbook.get("method_steps"), list) else [])[:3])],
            ],
            "evidence_sources": [str(collect_playbook.get("playbook_name") or "热点案例采集法")],
        },
        {
            "section_key": "analysis_method",
            "title": "分析怎么做",
            "summary": "分析子 Agent 先拆样本结构，再提炼出能复用的方法和禁用模式。",
            "bullets": [
                *[str(item) for item in ((analysis_playbook.get("method_steps") if isinstance(analysis_playbook.get("method_steps"), list) else [])[:4])],
                f"质量门：{str(quality_rule.get('rule_text') or '').strip()[:120] or '待补充'}",
            ],
            "evidence_sources": [str(analysis_playbook.get("playbook_name") or "爆款拆解分析法"), str(quality_rule.get("rule_name") or "账号文案质量门")],
        },
        {
            "section_key": "copy_method",
            "title": "帖子怎么写",
            "summary": "文案子 Agent 不只输出标题，而是输出完整可发正文，并说明为什么这么写。",
            "bullets": [
                *[str(item) for item in ((copy_playbook.get("method_steps") if isinstance(copy_playbook.get("method_steps"), list) else [])[:4])],
                f"叙事结构：{str(narrative_rule.get('rule_text') or '').strip()[:120] or '待补充'}",
                f"风险边界：{str(safety_rule.get('rule_text') or '').strip()[:120] or '待补充'}",
            ],
            "evidence_sources": [str(copy_playbook.get("playbook_name") or "小红书完整帖写作法"), str(narrative_rule.get("rule_name") or "正文叙事结构基线")],
        },
        {
            "section_key": "review_method",
            "title": "复盘怎么看",
            "summary": "复盘看真实可见指标和对标样本，不凭感觉直接改大策略。",
            "bullets": [
                *[str(item) for item in ((review_playbook.get("method_steps") if isinstance(review_playbook.get("method_steps"), list) else [])[:4])],
                f"回收时间窗：{' / '.join(str(item) for item in (feedback_plan.get('checkpoints_hours') or []))}",
                "主看点赞、收藏、评论、转发；曝光/播放仅作辅助。",
            ],
            "evidence_sources": [str(review_playbook.get("playbook_name") or "发布后复盘法")],
        },
        {
            "section_key": "sop_loop",
            "title": "这一轮 SOP 怎么闭环",
            "summary": "总教练负责调度，四个子 Agent 负责采集、分析、写作、复盘，最后再回到策略确认。",
            "bullets": [
                "collect -> analysis -> copy -> review -> strategy_confirm -> prompt_upgrade",
                f"账号定位：{str(strategy_profile.get('persona_name') or '待补充')} / {str(strategy_profile.get('ip_positioning') or '待补充')}",
                f"主目标：{str(strategy_profile.get('primary_goal') or '待补充')}",
                f"内容支柱：{' / '.join(_safe_str_list(strategy_profile.get('content_pillars'))[:4]) or '待补充'}",
            ],
            "evidence_sources": ["当前账号策略", "当前采集计划", "当前反馈计划"],
        },
    ]

    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "sections": sections,
    }


def get_execution_route_status_view(client: Client, *, account_id: str, domain_slug: str) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    row = _get_channel_account_raw(client, account_id)
    config = row.get("config_jsonb") if isinstance(row.get("config_jsonb"), dict) else {}
    collection_plan = _normalize_collection_plan(
        config.get("collection_plan") if isinstance(config.get("collection_plan"), dict) else {}
    )
    domain_id = _resolve_domain_id(client, domain_slug)
    domain_rows = client.table("domains").select("config_jsonb").eq("id", domain_id).limit(1).execute().data or []
    domain_config = (
        domain_rows[0].get("config_jsonb")
        if domain_rows and isinstance(domain_rows[0].get("config_jsonb"), dict)
        else {}
    )
    return resolve_execution_route_view(
        account_config=config,
        domain_config=domain_config,
        collection_mode=str(collection_plan.get("mode") or "hybrid"),
        account_id=account_id,
        domain_slug=domain_slug,
    )


def rollback_account_sop_snapshot(
    client: Client,
    *,
    account_id: str,
    domain_slug: str,
    version: int,
    actor: Actor,
    reason: str,
) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    _resolve_domain_id(client, domain_slug)
    current = _get_channel_account_raw(client, account_id)
    config = current.get("config_jsonb") if isinstance(current.get("config_jsonb"), dict) else {}

    snapshots = _list_sop_snapshots(config, limit=200)
    target = next((row for row in snapshots if int(row.get("version") or 0) == int(version)), None)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sop snapshot version not found")

    config["strategy_profile"] = _normalize_strategy_profile(
        target.get("strategy_profile") if isinstance(target.get("strategy_profile"), dict) else {}
    )
    config["collection_plan"] = _normalize_collection_plan(
        target.get("collection_plan") if isinstance(target.get("collection_plan"), dict) else {}
    )
    config["feedback_plan"] = _normalize_feedback_plan(
        target.get("feedback_plan") if isinstance(target.get("feedback_plan"), dict) else {}
    )
    changed_fields = ["strategy_profile", "collection_plan", "feedback_plan"]
    if "publish_preferences" in target:
        config["publish_preferences"] = deepcopy(target["publish_preferences"])
        changed_fields.append("publish_preferences")
    config["onboarding_ready"] = bool(config.get("onboarding_ready"))
    new_snapshot = _append_sop_snapshot(
        config,
        reason=f"sop_rollback_to_v{version}:{str(reason or '').strip()[:120]}",
        changed_fields=changed_fields,
        actor=actor.user_id,
        context={"rollback_target_version": int(version), "rollback_reason": str(reason or "").strip()},
    )

    client.table("channel_accounts").update({"config_jsonb": config, "updated_at": _now_iso()}).eq("id", account_id).execute()
    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.sop_rollback",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={
                "domain_slug": domain_slug,
                "rollback_target_version": int(version),
                "rollback_reason": str(reason or "").strip(),
                "active_version": int(config.get("sop_latest_version") or 0),
            },
        ),
    )
    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "active_version": int(config.get("sop_latest_version") or 0),
        "sop_latest": new_snapshot,
    }


def append_sop_checkpoint(
    client: Client,
    *,
    account_id: str,
    reason: str,
    actor: str,
    changed_fields: list[str] | None = None,
    context: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    _ensure_schema_ready(client)
    current = _get_channel_account_raw(client, account_id)
    config = current.get("config_jsonb") if isinstance(current.get("config_jsonb"), dict) else {}
    snapshot = _append_sop_snapshot(
        config,
        reason=reason,
        changed_fields=changed_fields or [],
        actor=actor,
        context=context or {},
    )
    client.table("channel_accounts").update({"config_jsonb": config, "updated_at": _now_iso()}).eq("id", account_id).execute()
    return snapshot


def run_review_action(
    client: Client,
    *,
    account_id: str,
    request: ReviewActionRequest,
    actor: Actor,
) -> Dict[str, Any]:
    _ensure_schema_ready(client)
    account = _get_channel_account_raw(client, account_id)
    domain_id = _resolve_domain_id(client, request.domain_slug)
    task = get_pipeline_task(client, request.pipeline_task_id)
    task_domain_id = str(task.get("domain_id") or "").strip()
    if task_domain_id and task_domain_id != domain_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="pipeline_task does not belong to requested domain")
    task_account_id = str(task.get("account_id") or "").strip()
    if task_account_id and task_account_id != account_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="pipeline_task does not belong to requested account")

    action = str(request.action or "").strip().lower()
    if action == "approve":
        updated_task = approve_pipeline_task(
            client,
            request.pipeline_task_id,
            actor=actor,
            edited_title=request.title,
            edited_body=request.body,
        )
    else:
        reason = str(request.reason or "").strip()
        if len(reason) < 3:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="reject action requires reason length >= 3")
        updated_task = reject_pipeline_task(client, request.pipeline_task_id, reason=reason, actor=actor)

    memory_item_id: str | None = None
    if action == "reject" and request.write_memory:
        memory_item_id = _upsert_memory_item(
            client,
            domain_id=domain_id,
            account_id=account_id,
            title="审核驳回约束",
            content=str(request.reason or "").strip(),
            tags=["review_reject", "copy_guardrail"],
            created_by=actor.user_id,
        )

    review_id: str | None = None
    if request.write_review:
        review_id = _create_review_record_for_task(
            client,
            domain_id=domain_id,
            account_id=account_id,
            pipeline_task_id=request.pipeline_task_id,
            action=action,
            reason=str(request.reason or "").strip(),
            created_by=actor.user_id,
        )

    append_sop_checkpoint(
        client,
        account_id=account_id,
        reason=f"review_action_{action}",
        actor=actor.user_id,
        changed_fields=["memory_items"] if action == "reject" else ["review_state"],
        context={"pipeline_task_id": request.pipeline_task_id, "action": action},
    )
    loop_status = get_account_loop_status(client, account_id=account_id, domain_slug=request.domain_slug)

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action=f"account.loop_review_action.{action}",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={
                "pipeline_task_id": request.pipeline_task_id,
                "domain_slug": request.domain_slug,
                "memory_item_id": memory_item_id,
                "review_id": review_id,
            },
        ),
    )
    return {
        "status": "ok",
        "account_id": str(account.get("id") or account_id),
        "pipeline_task_id": request.pipeline_task_id,
        "action": action,
        "loop_stage": str(((loop_status.get("loop") if isinstance(loop_status.get("loop"), dict) else {}).get("stage") or "collecting")),
        "memory_item_id": memory_item_id,
        "review_id": review_id,
        "pipeline_task": updated_task,
    }


def reset_channel_account_auth(client: Client, account_id: str, actor: Actor) -> AccountAuthResetResponse:
    _ensure_schema_ready(client)
    current = _get_channel_account_raw(client, account_id)
    patch = {
        "storage_state_path": None,
        "user_data_dir": None,
        "cookies_json": None,
        "login_username": None,
        "login_password": None,
        "last_login_check_status": None,
        "last_login_check_message": None,
        "last_login_check_at": None,
        "updated_at": _now_iso(),
    }
    client.table("channel_accounts").update(patch).eq("id", account_id).execute()
    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.auth_reset",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={"account_name": current.get("account_name")},
        ),
    )
    return AccountAuthResetResponse(
        status="ok",
        account_id=account_id,
        cleared_fields=[
            "storage_state_path",
            "user_data_dir",
            "cookies_json",
            "login_username",
            "login_password",
            "last_login_check_status",
            "last_login_check_message",
            "last_login_check_at",
        ],
        next_step_hint="重新配置该账号的登录材料并执行一次登录校验。",
    )


def _find_project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in [Path.cwd(), *here.parents]:
        if (parent / "scripts" / "export_xhs_state.py").exists():
            return parent
    return Path.cwd()


def _resolve_storage_state_output(account_id: str, raw_path: str) -> str:
    cleaned = (raw_path or "").strip()
    if cleaned:
        normalized = cleaned.replace("/", "\\").lower()
        if not normalized.endswith("\\xhs_storage_state.json") and not normalized.endswith("/xhs_storage_state.json"):
            return cleaned
    return str(Path(".run") / "account_states" / f"{account_id}.json")


def _resolve_account_user_data_dir(account_id: str, raw_path: str) -> str:
    cleaned = (raw_path or "").strip()
    if cleaned:
        return cleaned
    return str(Path(".run") / "account_user_data" / account_id)


def launch_channel_account_login_bootstrap(
    client: Client,
    account_id: str,
    request: AccountLoginBootstrapRequest,
    actor: Actor,
) -> AccountLoginBootstrapResponse:
    _ensure_schema_ready(client)
    current = _get_channel_account_raw(client, account_id)
    login_mode = str(current.get("login_mode") or "").strip()
    if login_mode != "storage_state":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="login bootstrap only supports storage_state mode",
        )

    project_root = _find_project_root()
    script_path = project_root / "scripts" / "export_xhs_state.py"
    if not script_path.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"login bootstrap script not found: {script_path}",
        )

    output_path = _resolve_storage_state_output(account_id, str(current.get("storage_state_path") or ""))
    user_data_dir = _resolve_account_user_data_dir(account_id, str(current.get("user_data_dir") or ""))
    patch: Dict[str, Any] = {"updated_at": _now_iso()}
    if output_path != str(current.get("storage_state_path") or ""):
        patch["storage_state_path"] = output_path
    if user_data_dir != str(current.get("user_data_dir") or ""):
        patch["user_data_dir"] = user_data_dir
    if len(patch) > 1:
        updated = (
            client.table("channel_accounts")
            .update(patch)
            .eq("id", account_id)
            .execute()
        )
        _single_or_404(updated.data or [], "updated account")

    command = [
        sys.executable,
        str(script_path),
        "--output",
        output_path,
        "--wait-seconds",
        str(request.wait_seconds),
        "--verify-web",
        "--user-data-dir",
        user_data_dir,
    ]
    if request.url and request.url.strip():
        command.extend(["--url", request.url.strip()])

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(project_root),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
    }
    if sys.platform.startswith("win"):
        creationflags = 0
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        popen_kwargs["creationflags"] = creationflags

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to launch login bootstrap: {exc}",
        ) from exc

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="account.login_bootstrap_launched",
            target_type="channel_account",
            target_id=account_id,
            diff_jsonb={
                "output_path": output_path,
                "wait_seconds": request.wait_seconds,
                "url": request.url or "https://creator.xiaohongshu.com",
                "pid": proc.pid,
            },
        ),
    )

    return AccountLoginBootstrapResponse(
        status="ok",
        account_id=account_id,
        login_mode=login_mode,
        output_path=output_path,
        launched=True,
        pid=proc.pid,
        command=command,
        next_step_hint="网页登录窗口已打开。请完成扫码登录，并等待 Creator + Web 两个页面都稳定登录后再返回账号页点击“校验”。",
    )


def _resolve_domain_id(client: Client, domain_slug: str) -> str:
    rows: list[Dict[str, Any]] = []
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            rows = (
                client.table("domains")
                .select("id")
                .eq("slug", domain_slug)
                .limit(1)
                .execute()
                .data
                or []
            )
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            message = f"{type(exc).__name__}: {exc}".lower()
            retryable = any(
                token in message
                for token in ("server disconnected", "remoteprotocolerror", "timeout", "timed out", "connection reset")
            )
            if retryable and attempt < 2:
                time.sleep(0.15 * (attempt + 1))
                continue
            break
    if last_exc and not rows:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"domain lookup unavailable: {last_exc}",
        ) from last_exc
    row = _single_or_404(rows, "domain")
    return str(row.get("id") or "")


def list_prompt_versions(client: Client, account_id: str, *, domain_slug: str | None = None, agent_name: str | None = None, limit: int = 100) -> list[Dict[str, Any]]:
    query = client.table("prompt_versions").select("*").eq("account_id", account_id).order("created_at", desc=True).limit(limit)
    if domain_slug:
        query = query.eq("domain_id", _resolve_domain_id(client, domain_slug))
    if agent_name:
        query = query.eq("agent_name", agent_name)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return query.execute().data or []
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            message = f"{type(exc).__name__}: {exc}".lower()
            retryable = any(
                token in message
                for token in ("server disconnected", "remoteprotocolerror", "timeout", "timed out", "connection reset")
            )
            if retryable and attempt < 2:
                time.sleep(0.2 * (attempt + 1))
                continue
            return []
    if last_exc:
        return []
    return []


def create_prompt_version(client: Client, account_id: str, request: PromptVersionCreateRequest, actor: Actor) -> Dict[str, Any]:
    domain_id = _resolve_domain_id(client, request.domain_slug)
    status_value = str(request.status or "draft").strip().lower()
    source_value = str(request.source or "manual").strip().lower()
    if status_value not in ALLOWED_PROMPT_STATUS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unsupported prompt status: {request.status}")
    if source_value not in ALLOWED_PROMPT_SOURCE:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unsupported prompt source: {request.source}")

    if status_value == "active":
        client.table("prompt_versions").update({"status": "archived", "updated_at": _now_iso()}).eq("account_id", account_id).eq("domain_id", domain_id).eq("agent_name", request.agent_name).eq("status", "active").execute()

    inserted = client.table("prompt_versions").insert(
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "agent_name": request.agent_name,
            "version": request.version,
            "system_prompt": request.system_prompt,
            "status": status_value,
            "source": source_value,
            "reason": request.reason,
            "created_by": actor.user_id,
            "updated_at": _now_iso(),
        }
    ).execute()
    row = _single_or_404(inserted.data or [], "prompt version")
    client.table("prompt_change_logs").insert(
        {
            "prompt_version_id": row["id"],
            "account_id": account_id,
            "change_summary": request.reason,
            "evidence_jsonb": request.evidence_jsonb,
            "approved_by": actor.user_id,
        }
    ).execute()
    return row


def rollback_prompt_version(client: Client, account_id: str, prompt_version_id: str, reason: str, actor: Actor) -> Dict[str, Any]:
    rows = client.table("prompt_versions").select("*").eq("id", prompt_version_id).eq("account_id", account_id).limit(1).execute().data or []
    current = _single_or_404(rows, "prompt version")
    target_agent = str(current.get("agent_name") or "")
    domain_id = str(current.get("domain_id") or "")
    previous_rows = (
        client.table("prompt_versions")
        .select("*")
        .eq("account_id", account_id)
        .eq("domain_id", domain_id)
        .eq("agent_name", target_agent)
        .neq("id", prompt_version_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data or []
    previous = _single_or_404(previous_rows, "rollback target version")
    client.table("prompt_versions").update({"status": "rolled_back", "updated_at": _now_iso()}).eq("id", prompt_version_id).execute()
    activated = (
        client.table("prompt_versions")
        .update({"status": "active", "rolled_back_from": prompt_version_id, "updated_at": _now_iso()})
        .eq("id", previous["id"])
        .execute()
    )
    row = _single_or_404(activated.data or [], "rolled back version")
    client.table("prompt_change_logs").insert(
        {
            "prompt_version_id": row["id"],
            "account_id": account_id,
            "change_summary": reason,
            "evidence_jsonb": {"rolled_back_from": prompt_version_id},
            "approved_by": actor.user_id,
        }
    ).execute()
    return row


def activate_prompt_version(client: Client, account_id: str, prompt_version_id: str, reason: str, actor: Actor) -> Dict[str, Any]:
    rows = client.table("prompt_versions").select("*").eq("id", prompt_version_id).eq("account_id", account_id).limit(1).execute().data or []
    current = _single_or_404(rows, "prompt version")
    domain_id = str(current.get("domain_id") or "")
    target_agent = str(current.get("agent_name") or "")
    current_status = str(current.get("status") or "").strip().lower()
    if current_status == "active":
        return current

    client.table("prompt_versions").update({"status": "archived", "updated_at": _now_iso()}).eq("account_id", account_id).eq("domain_id", domain_id).eq("agent_name", target_agent).eq("status", "active").execute()
    activated = (
        client.table("prompt_versions")
        .update({"status": "active", "updated_at": _now_iso()})
        .eq("id", prompt_version_id)
        .execute()
    )
    row = _single_or_404(activated.data or [], "activated prompt version")
    client.table("prompt_change_logs").insert(
        {
            "prompt_version_id": row["id"],
            "account_id": account_id,
            "change_summary": reason,
            "evidence_jsonb": {"activated_from_status": current_status or "unknown"},
            "approved_by": actor.user_id,
        }
    ).execute()
    return row


def get_prompt_version_by_version(
    client: Client,
    account_id: str,
    *,
    domain_slug: str,
    agent_name: str,
    version: str,
) -> Dict[str, Any] | None:
    domain_id = _resolve_domain_id(client, domain_slug)
    rows = (
        client.table("prompt_versions")
        .select("*")
        .eq("account_id", account_id)
        .eq("domain_id", domain_id)
        .eq("agent_name", agent_name)
        .eq("version", version)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None
