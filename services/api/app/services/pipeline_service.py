from datetime import datetime, timezone
from copy import deepcopy
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client

from app.models import Actor, AuditLogEntry, PipelineTaskCreateRequest
from app.services.audit import write_audit_log
from app.services.content_branches import branch_selection, project_reference_ready

ALLOWED_CHANNELS = {"xiaohongshu", "wechat_mp", "douyin", "video"}
ALLOWED_QUALITY_CHECKS = {
    "title_length",
    "body_length",
    "paragraph_count",
    "keyword_coverage",
    "hashtag_count",
    "cta_presence",
    "forbidden_claims",
    "encoding_clean",
}
REJECTION_REASON_HINTS = [
    (("标题", "题目", "title"), "title_length"),
    (("正文", "内容", "太短", "太长", "篇幅", "body", "length"), "body_length"),
    (("分段", "段落", "paragraph"), "paragraph_count"),
    (("关键词", "相关词", "定位词", "keyword"), "keyword_coverage"),
    (("标签", "话题", "#", "hashtag", "tag"), "hashtag_count"),
    (("互动", "引导", "评论区", "私信", "收藏", "cta", "call to action"), "cta_presence"),
    (("违规", "广告", "夸大", "承诺", "违禁", "compliance", "forbidden"), "forbidden_claims"),
    (("乱码", "错别字", "编码", "encoding", "mojibake"), "encoding_clean"),
]


def _repair_mojibake_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        repaired = text.encode("latin-1").decode("utf-8").strip()
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired or text


def _normalize_xhs_publish_tab(payload: Dict[str, Any]) -> str:
    raw = _repair_mojibake_text(str(payload.get("xhs_publish_tab") or "").strip()).replace(" ", "")
    images = payload.get("images") or payload.get("image_paths") or payload.get("local_images")
    image_count = len(images) if isinstance(images, list) else 0
    video = str(payload.get("video") or payload.get("video_path") or "").strip()
    if raw in {"写长文", "长文", "图文长文"}:
        return "写长文"
    if raw in {"上传图文", "图文", "发图文"}:
        return "上传图文"
    if raw in {"上传视频", "视频", "发视频"}:
        return "上传视频"
    if video:
        return "上传视频"
    if image_count > 0:
        return "上传图文"
    return "写长文"


def _normalize_pipeline_payload(channel: str, payload_jsonb: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload_jsonb or {})
    for key in ("title", "body", "cta"):
        if key in payload:
            payload[key] = _repair_mojibake_text(str(payload.get(key) or "").strip())
    hashtags = payload.get("hashtags") or payload.get("tags")
    if isinstance(hashtags, list):
        payload["hashtags"] = [_repair_mojibake_text(str(tag or "").strip()) for tag in hashtags if str(tag or "").strip()]
    if channel == "xiaohongshu":
        payload["xhs_publish_tab"] = _normalize_xhs_publish_tab(payload)
    return payload


def _single_or_404(rows: List[Dict[str, Any]], target: str) -> Dict[str, Any]:
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{target} not found")
    return rows[0]


def _find_domain_by_slug(client: Client, slug: str) -> Dict[str, Any]:
    res = client.table("domains").select("id,slug,name").eq("slug", slug).limit(1).execute()
    return _single_or_404(res.data or [], "domain")


def _ensure_channel(channel: str) -> str:
    normalized = channel.strip().lower()
    if normalized not in ALLOWED_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported channel '{channel}'",
        )
    return normalized


def _to_utc_iso(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _resolve_channel_account_id_for_create(
    client: Client,
    *,
    channel: str,
    payload_jsonb: Dict[str, Any],
) -> Dict[str, Any]:
    payload = dict(payload_jsonb or {})
    if channel != "xiaohongshu":
        return payload

    requested_account_id = str(payload.get("channel_account_id") or "").strip()
    try:
        active_accounts = (
            client.table("channel_accounts")
            .select("id,channel,is_active")
            .eq("channel", "xiaohongshu")
            .eq("is_active", True)
            .order("updated_at", desc=True)
            .limit(20)
            .execute()
        ).data or []
    except APIError:
        # If account table not migrated yet, keep backward compatibility.
        return payload

    if requested_account_id:
        matched = [row for row in active_accounts if str(row.get("id") or "") == requested_account_id]
        if not matched:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="所选发布账号不存在或未启用，请在账号页启用后重试。",
            )
        payload["channel_account_id"] = requested_account_id
        return payload

    if len(active_accounts) == 1:
        payload["channel_account_id"] = str(active_accounts[0].get("id") or "")
        return payload

    if len(active_accounts) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未配置可用的小红书发布账号，请先在账号页新增并启用账号。",
        )

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="检测到多个启用中的小红书账号，请先在任务里明确选择发布账号。",
    )


def _extract_error_message(exc: APIError) -> str:
    try:
        payload = exc.args[0] if exc.args else {}
        if isinstance(payload, dict):
            return str(payload.get("message") or "")
        return str(exc)
    except Exception:  # noqa: BLE001
        return str(exc)


def _is_missing_pipeline_table(exc: APIError) -> bool:
    raw_text = str(exc).lower()
    message = _extract_error_message(exc).lower()
    code = ""
    try:
        payload = exc.args[0] if exc.args else {}
        if isinstance(payload, dict):
            code = str(payload.get("code") or "")
        else:
            code = ""
    except Exception:  # noqa: BLE001
        code = ""
    if "pipeline_tasks" not in raw_text and "pipeline_tasks" not in message:
        return False
    if code == "PGRST205":
        return True
    return "could not find the table" in raw_text or "schema cache" in raw_text


def pipeline_schema_ready(client: Client) -> bool:
    for attempt in range(3):
        try:
            client.table("pipeline_tasks").select("id").limit(1).execute()
            return True
        except APIError as exc:
            if _is_missing_pipeline_table(exc):
                return False
            message = str(exc).lower()
            retryable = any(
                token in message
                for token in (
                    "server disconnected",
                    "remoteprotocolerror",
                    "connection reset",
                    "timed out",
                    "timeout",
                )
            )
            if retryable and attempt < 2:
                time.sleep(0.15 * (attempt + 1))
                continue
            return False
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}".lower()
            retryable = any(
                token in message
                for token in (
                    "server disconnected",
                    "remoteprotocolerror",
                    "connection reset",
                    "timed out",
                    "timeout",
                    "temporarily unavailable",
                )
            )
            if retryable and attempt < 2:
                time.sleep(0.15 * (attempt + 1))
                continue
            return False
    return False


def _pipeline_task_account_column_ready(client: Client) -> bool:
    try:
        client.table("pipeline_tasks").select("account_id").limit(1).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def _table_ready(client: Client, table_name: str, column: str = "id") -> bool:
    try:
        client.table(table_name).select(column).limit(1).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def _dedup_limited(items: List[str], *, max_items: int = 8) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for raw in items:
        token = str(raw or "").strip()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
        if len(result) >= max_items:
            break
    return result


def _normalize_quality_checks(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    filtered = [str(item or "").strip() for item in raw if str(item or "").strip() in ALLOWED_QUALITY_CHECKS]
    return _dedup_limited(filtered, max_items=8)


def _infer_quality_checks_from_reason(reason: str) -> List[str]:
    text = str(reason or "").strip()
    if not text:
        return []
    lowered = text.lower()
    inferred: List[str] = []
    for keywords, check_name in REJECTION_REASON_HINTS:
        if any(keyword in text or keyword in lowered for keyword in keywords):
            inferred.append(check_name)
    return _dedup_limited(inferred, max_items=8)


def _extract_failed_checks_from_task(task: Dict[str, Any]) -> List[str]:
    payload = task.get("payload_jsonb") if isinstance(task.get("payload_jsonb"), dict) else {}
    analysis = payload.get("analysis_jsonb") if isinstance(payload.get("analysis_jsonb"), dict) else {}
    quality_gate = analysis.get("quality_gate") if isinstance(analysis.get("quality_gate"), dict) else {}
    final_report = quality_gate.get("final") if isinstance(quality_gate.get("final"), dict) else {}
    return _normalize_quality_checks(final_report.get("failed_checks"))


def _resolve_task_account_id(task: Dict[str, Any]) -> str:
    direct = str(task.get("account_id") or "").strip()
    if direct:
        return direct
    payload = task.get("payload_jsonb") if isinstance(task.get("payload_jsonb"), dict) else {}
    return str(payload.get("channel_account_id") or "").strip()


def _learn_quality_gate_from_rejection(
    client: Client,
    *,
    task: Dict[str, Any],
    reason: str,
    actor: str,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "applied": False,
        "account_id": "",
        "failed_checks": _extract_failed_checks_from_task(task),
        "reason_checks": _infer_quality_checks_from_reason(reason),
        "emphasize_checks": [],
    }
    account_id = _resolve_task_account_id(task)
    result["account_id"] = account_id
    if not account_id or not _table_ready(client, "channel_accounts"):
        return result

    try:
        account_rows = (
            client.table("channel_accounts")
            .select("id,config_jsonb")
            .eq("id", account_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        return result
    if not account_rows:
        return result

    account = account_rows[0]
    config = account.get("config_jsonb") if isinstance(account.get("config_jsonb"), dict) else {}
    strategy_profile = config.get("strategy_profile") if isinstance(config.get("strategy_profile"), dict) else {}
    quality_gate = strategy_profile.get("quality_gate") if isinstance(strategy_profile.get("quality_gate"), dict) else {}
    current_emphasize = _normalize_quality_checks(quality_gate.get("emphasize_checks"))
    next_emphasize = _dedup_limited(
        [*current_emphasize, *result["failed_checks"], *result["reason_checks"]],
        max_items=8,
    )
    result["emphasize_checks"] = next_emphasize

    now_iso = _to_utc_iso(datetime.now(timezone.utc))
    quality_gate["emphasize_checks"] = next_emphasize
    quality_gate["last_reject_reason"] = str(reason or "").strip()[:800]
    quality_gate["last_reject_at"] = now_iso
    strategy_profile["quality_gate"] = quality_gate
    config["strategy_profile"] = strategy_profile

    try:
        client.table("channel_accounts").update({"config_jsonb": config, "updated_at": now_iso}).eq("id", account_id).execute()
        result["applied"] = True
    except Exception:  # noqa: BLE001
        return result

    if _table_ready(client, "memory_items"):
        domain_id = str(task.get("domain_id") or "").strip()
        if domain_id:
            content = (
                f"驳回原因：{str(reason or '').strip()[:800]}\n"
                f"自动识别失败项：{', '.join(result['failed_checks']) or '无'}\n"
                f"强化检查项：{', '.join(next_emphasize) or '无'}"
            )
            try:
                inserted = (
                    client.table("memory_items")
                    .insert(
                        {
                            "domain_id": domain_id,
                            "account_id": account_id,
                            "type": "strategy_rule",
                            "title": "审核驳回-质量门强化",
                            "content": content[:4000],
                            "tags": ["review_reject", "quality_gate", "auto_feedback"],
                            "status": "active",
                            "confidence": 0.9,
                            "created_by": actor,
                            "updated_at": now_iso,
                        }
                    )
                    .execute()
                    .data
                    or []
                )
                if inserted:
                    result["memory_item_id"] = str(inserted[0].get("id") or "")
            except Exception:  # noqa: BLE001
                pass
    return result


def list_pipeline_tasks(
    client: Client,
    status_value: Optional[str] = None,
    channel: Optional[str] = None,
    domain_slug: Optional[str] = None,
    account_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    normalized_account_id = str(account_id or "").strip()
    has_account_column = _pipeline_task_account_column_ready(client)
    try:
        query = client.table("pipeline_tasks").select("*").order("created_at", desc=True).limit(limit)
        if status_value:
            query = query.eq("status", status_value)
        if channel:
            query = query.eq("channel", _ensure_channel(channel))
        if domain_slug:
            domain = _find_domain_by_slug(client, domain_slug)
            query = query.eq("domain_id", domain["id"])
        if normalized_account_id:
            if has_account_column:
                query = query.eq("account_id", normalized_account_id)
            else:
                query = query.contains("payload_jsonb", {"channel_account_id": normalized_account_id})
        res = query.execute()
        return res.data or []
    except APIError as exc:
        if _is_missing_pipeline_table(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pipeline_tasks table is missing. Please apply migration 003_matrix_pipeline.sql.",
            )
        if normalized_account_id:
            # Some PostgREST versions do not support JSON contains in current schema cache.
            # Fallback to in-memory filtering to keep account isolation available.
            query = client.table("pipeline_tasks").select("*").order("created_at", desc=True).limit(max(limit * 5, 200))
            if status_value:
                query = query.eq("status", status_value)
            if channel:
                query = query.eq("channel", _ensure_channel(channel))
            if domain_slug:
                domain = _find_domain_by_slug(client, domain_slug)
                query = query.eq("domain_id", domain["id"])
            rows = query.execute().data or []
            rows = [
                row
                for row in rows
                if (
                    str(row.get("account_id") or "").strip() == normalized_account_id
                    or str(((row.get("payload_jsonb") or {}) if isinstance(row.get("payload_jsonb"), dict) else {}).get("channel_account_id") or "").strip()
                    == normalized_account_id
                )
            ][:limit]
            return rows
        raise


def get_pipeline_task(client: Client, pipeline_task_id: str) -> Dict[str, Any]:
    try:
        res = client.table("pipeline_tasks").select("*").eq("id", pipeline_task_id).limit(1).execute()
        return _single_or_404(res.data or [], "pipeline task")
    except APIError as exc:
        if _is_missing_pipeline_table(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pipeline_tasks table is missing. Please apply migration 003_matrix_pipeline.sql.",
            )
        raise


def create_pipeline_task(
    client: Client,
    request: PipelineTaskCreateRequest,
    actor: Actor,
) -> Dict[str, Any]:
    try:
        domain = _find_domain_by_slug(client, request.domain_slug)
        channel = _ensure_channel(request.channel)

        normalized_payload_jsonb = _normalize_pipeline_payload(channel, request.payload_jsonb or {})
        if request.account_id:
            normalized_payload_jsonb["channel_account_id"] = request.account_id.strip()
        resolved_payload_jsonb = _resolve_channel_account_id_for_create(
            client,
            channel=channel,
            payload_jsonb=normalized_payload_jsonb,
        )
        resolved_account_id = str(request.account_id or resolved_payload_jsonb.get("channel_account_id") or "").strip() or None
        intent = deepcopy(request.intent_jsonb)
        account_overrides = {}
        if resolved_account_id:
            accounts = client.table("channel_accounts").select("config_jsonb").eq("id", resolved_account_id).limit(1).execute().data or []
            if accounts:
                account_overrides = ((accounts[0].get("config_jsonb") or {}).get("strategy_profile") or {}).get("prompt_overrides") or {}
        branch = branch_selection({"content_branch": intent.get("content_branch", account_overrides.get("content_branch", "tutorial"))})
        intent["content_branch"] = branch
        if branch == "project_observer":
            project = deepcopy(intent.get("project_reference", account_overrides.get("project_reference")) or {})
            if domain["slug"] != "ai_content" or not project_reference_ready(project):
                raise HTTPException(422, "AI 项目观察分支需要 AI 内容赛道和完整的项目来源资料。")
            intent["project_reference"] = project

        payload = {
            "domain_id": domain["id"],
            "account_id": resolved_account_id,
            "channel": channel,
            "content_type": request.content_type,
            "status": "queued",
            "stage": "intake",
            "intent_jsonb": intent,
            "payload_jsonb": resolved_payload_jsonb,
            "scheduled_at": _to_utc_iso(request.scheduled_at),
            "created_by": actor.user_id,
        }
        inserted = client.table("pipeline_tasks").insert(payload).execute()
        row = _single_or_404(inserted.data or [], "created pipeline task")

        write_audit_log(
            client,
            AuditLogEntry(
                actor=actor.user_id,
                action="pipeline.task_created",
                target_type="pipeline_task",
                target_id=row["id"],
                diff_jsonb={
                    "domain_slug": domain["slug"],
                    "account_id": resolved_account_id,
                    "channel": row["channel"],
                    "content_type": row["content_type"],
                },
            ),
        )
        return row
    except APIError as exc:
        if _is_missing_pipeline_table(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pipeline_tasks table is missing. Please apply migration 003_matrix_pipeline.sql.",
            )
        raise


def update_pipeline_task(
    client: Client,
    pipeline_task_id: str,
    actor: Actor,
    title: str,
    body: str,
) -> Dict[str, Any]:
    task = get_pipeline_task(client, pipeline_task_id)
    editable = {"queued", "intel_ready", "pending_review", "review_rejected", "approved", "publish_failed"}
    if task["status"] not in editable:
        raise HTTPException(status_code=409, detail="当前内容正在执行或已经发布，不能修改。")
    payload = _normalize_pipeline_payload(task["channel"], {
        **(task.get("payload_jsonb") or {}), "title": title.strip(), "body": body.strip(),
    })
    if "full_body" in payload:
        payload["full_body"] = payload["body"]
    previous = task.get("payload_jsonb") or {}
    if payload["title"] != previous.get("title") or payload["body"] != previous.get("body"):
        # Scores, paragraph evidence and cover suggestions describe the old text.
        analysis = dict(payload.get("analysis_jsonb") or {})
        for key in ("quality_gate", "evidence_map", "style_samples"):
            analysis.pop(key, None)
        analysis["review_required_after_edit"] = True
        payload["analysis_jsonb"] = analysis
        payload.pop("image_prompt", None)
    changes = {"payload_jsonb": payload, "updated_at": _to_utc_iso(datetime.now(timezone.utc))}
    if task["status"] in {"approved", "publish_failed", "review_rejected"}:
        changes.update({"status": "pending_review", "stage": "review"})
    query = client.table("pipeline_tasks").update(changes).eq("id", pipeline_task_id).eq("status", task["status"])
    if task.get("updated_at"):
        query = query.eq("updated_at", task["updated_at"])
    rows = query.execute().data or []
    if not rows:
        raise HTTPException(status_code=409, detail="内容已被其他操作更新，请刷新后重试。")
    write_audit_log(client, AuditLogEntry(
        actor=actor.user_id, action="pipeline.task_edited", target_type="pipeline_task",
        target_id=pipeline_task_id, diff_jsonb={
            "from": task["status"], "to": rows[0]["status"],
            "edited_fields": ["title", "body"],
        },
    ))
    return rows[0]


def approve_pipeline_task(
    client: Client,
    pipeline_task_id: str,
    actor: Actor,
    edited_title: Optional[str] = None,
    edited_body: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        task = get_pipeline_task(client, pipeline_task_id)
        if task["status"] != "pending_review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Pipeline task in status {task['status']} cannot be approved",
            )

        payload_jsonb = _normalize_pipeline_payload(str(task.get("channel") or "").strip(), dict(task.get("payload_jsonb") or {}))
        edited_fields: Dict[str, Any] = {}
        if edited_title is not None:
            payload_jsonb["title"] = edited_title.strip()
            edited_fields["title"] = {"from": (task.get("payload_jsonb") or {}).get("title"), "to": payload_jsonb["title"]}
        if edited_body is not None:
            payload_jsonb["body"] = edited_body.strip()
            if "full_body" in payload_jsonb:
                payload_jsonb["full_body"] = payload_jsonb["body"]
            edited_fields["body"] = {"from": (task.get("payload_jsonb") or {}).get("body"), "to": payload_jsonb["body"]}
        payload_jsonb = _normalize_pipeline_payload(str(task.get("channel") or "").strip(), payload_jsonb)

        query = client.table("pipeline_tasks").update(
            {
                "status": "approved",
                "stage": "approved",
                "payload_jsonb": payload_jsonb,
                "review_jsonb": {
                    **(task.get("review_jsonb") or {}),
                    "approved_by": actor.user_id,
                    "approved_at": _to_utc_iso(datetime.now(timezone.utc)),
                },
                "updated_at": _to_utc_iso(datetime.now(timezone.utc)),
            }
        ).eq("id", pipeline_task_id).eq("status", "pending_review")
        if task.get("updated_at"):
            query = query.eq("updated_at", task["updated_at"])
        updated = query.execute()
        if not updated.data:
            raise HTTPException(status_code=409, detail="正文或配图已更新，请刷新后重新审核。")
        row = updated.data[0]

        diff_jsonb: Dict[str, Any] = {"from": task["status"], "to": "approved"}
        if edited_fields:
            diff_jsonb["edited_fields"] = edited_fields

        write_audit_log(
            client,
            AuditLogEntry(
                actor=actor.user_id,
                action="pipeline.task_approved",
                target_type="pipeline_task",
                target_id=pipeline_task_id,
                diff_jsonb=diff_jsonb,
            ),
        )
        return row
    except APIError as exc:
        if _is_missing_pipeline_table(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pipeline_tasks table is missing. Please apply migration 003_matrix_pipeline.sql.",
            )
        raise


def reject_pipeline_task(
    client: Client,
    pipeline_task_id: str,
    reason: str,
    actor: Actor,
) -> Dict[str, Any]:
    try:
        task = get_pipeline_task(client, pipeline_task_id)
        if task["status"] != "pending_review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Pipeline task in status {task['status']} cannot be rejected",
            )

        review_jsonb = {
            **(task.get("review_jsonb") or {}),
            "rejected_by": actor.user_id,
            "rejected_at": _to_utc_iso(datetime.now(timezone.utc)),
            "rejection_reason": reason,
        }
        updated = client.table("pipeline_tasks").update(
            {
                "status": "review_rejected",
                "stage": "copywriting",
                "review_jsonb": review_jsonb,
                "updated_at": _to_utc_iso(datetime.now(timezone.utc)),
            }
        ).eq("id", pipeline_task_id).execute()
        row = _single_or_404(updated.data or [], "updated pipeline task")
        quality_feedback = _learn_quality_gate_from_rejection(
            client,
            task=task,
            reason=reason,
            actor=actor.user_id,
        )

        write_audit_log(
            client,
            AuditLogEntry(
                actor=actor.user_id,
                action="pipeline.task_rejected",
                target_type="pipeline_task",
                target_id=pipeline_task_id,
                diff_jsonb={
                    "from": task["status"],
                    "to": "review_rejected",
                    "reason": reason,
                    "quality_feedback": quality_feedback,
                },
            ),
        )
        return row
    except APIError as exc:
        if _is_missing_pipeline_table(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pipeline_tasks table is missing. Please apply migration 003_matrix_pipeline.sql.",
            )
        raise
