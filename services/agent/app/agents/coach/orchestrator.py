from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from threading import Lock
import time
from typing import Any, Dict, List
from uuid import uuid4

from supabase import Client

from app.agents.subagents import run_analysis_agent, run_collector_agent, run_copy_agent, run_review_agent
from app.governance.execution_control import build_policy_snapshot, is_action_allowed, resolve_actor_audience, resolve_tool_policies
from app.governance.execution_ledger import record_action_run
from app.orchestration.subagent_queue import submit_subagent_job
from app.orchestration.publish_feedback import _extract_observation, _parse_iso as _parse_evidence_time

_CN_TZ = timezone(timedelta(hours=8))
_ACTION_TTL_SECONDS = 3600
_ACTION_STORE: Dict[str, Dict[str, Any]] = {}
_ACTION_LOCK = Lock()
_NO_FALLBACK = object()
_COACH_ACTION_TERMINAL_STATES = {"completed", "failed", "canceled", "expired"}
_COACH_ACTION_BUSY_STATES = {"confirmed", "running"}
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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_local() -> datetime:
    return datetime.now(_CN_TZ)


def _parse_iso(value: Any) -> datetime | None:
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


def _is_transient_db_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(token in text for token in _TRANSIENT_DB_TOKENS)


def _run_with_db_retry(func, *, retries: int = 2, fallback: Any = _NO_FALLBACK) -> Any:
    last_exc: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            return func()
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


def _table_ready(client: Client, table: str) -> bool:
    try:
        _run_with_db_retry(lambda: client.table(table).select("id").limit(1).execute(), retries=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def _resolve_domain(client: Client, domain_slug: str) -> Dict[str, Any]:
    rows = _run_with_db_retry(
        lambda: (
            client.table("domains")
            .select("id,slug,name,config_jsonb")
            .eq("slug", domain_slug)
            .limit(1)
            .execute()
            .data
            or []
        ),
        fallback=[],
    )
    if not rows:
        raise ValueError(f"domain not found: {domain_slug}")
    return rows[0]


def _resolve_account(client: Client, account_id: str) -> Dict[str, Any] | None:
    normalized = str(account_id or "").strip()
    if normalized:
        rows = _run_with_db_retry(
            lambda: (
                client.table("channel_accounts")
                .select("id,channel,account_name,config_jsonb,is_active")
                .eq("id", normalized)
                .limit(1)
                .execute()
                .data
                or []
            ),
            fallback=[],
        )
        if rows:
            return rows[0]

    rows = _run_with_db_retry(
        lambda: (
            client.table("channel_accounts")
            .select("id,channel,account_name,config_jsonb,is_active")
            .eq("channel", "xiaohongshu")
            .eq("is_active", True)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        ),
        fallback=[],
    )
    return rows[0] if rows else None


def _safe_select_rows(
    client: Client,
    *,
    table: str,
    select_fields: str,
    domain_id: str,
    account_id: str = "",
    order_field: str = "updated_at",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    if not _table_ready(client, table):
        return []
    normalized_account_id = str(account_id or "").strip()
    query = (
        client.table(table)
        .select(select_fields)
        .eq("domain_id", domain_id)
        .order(order_field, desc=True)
        .limit(max(1, min(limit, 500)))
    )
    if normalized_account_id:
        try:
            query = query.eq("account_id", normalized_account_id)
        except Exception:  # noqa: BLE001
            pass
    return _run_with_db_retry(lambda: query.execute().data or [], fallback=[])


def _safe_count(
    client: Client,
    *,
    table: str,
    domain_id: str,
    account_id: str = "",
    extra_eq: tuple[str, Any] | None = None,
) -> int:
    if not _table_ready(client, table):
        return 0
    query = client.table(table).select("id", count="exact").eq("domain_id", domain_id).limit(1)
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        try:
            query = query.eq("account_id", normalized_account_id)
        except Exception:  # noqa: BLE001
            pass
    if extra_eq:
        key, value = extra_eq
        try:
            query = query.eq(key, value)
        except Exception:  # noqa: BLE001
            pass
    result = _run_with_db_retry(lambda: query.execute(), fallback=None)
    if result is None:
        return 0
    return int(result.count or 0)


def _in_yesterday_local(ts: datetime | None) -> bool:
    if ts is None:
        return False
    local = ts.astimezone(_CN_TZ)
    today = _now_local().date()
    yesterday = today - timedelta(days=1)
    return local.date() == yesterday


def _metric_value(metrics: Dict[str, Any], keys: List[str]) -> float | None:
    for key in keys:
        raw = metrics.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= 0:
            return value
    return None


def _build_context(client: Client, *, domain_slug: str, account_id: str) -> Dict[str, Any]:
    domain = _resolve_domain(client, domain_slug)
    account = _resolve_account(client, account_id)
    resolved_account_id = str((account or {}).get("id") or "").strip()
    domain_id = str(domain["id"])

    pipeline_rows = _safe_select_rows(
        client,
        table="pipeline_tasks",
        select_fields="id,status,stage,published_at,updated_at,metrics_jsonb,review_jsonb,payload_jsonb,account_id",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="updated_at",
        limit=240,
    )
    intel_rows = _safe_select_rows(
        client,
        table="intelligence_items",
        select_fields="id,account_id,source_type,source_url,captured_at,raw_text,meta_jsonb",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="captured_at",
        limit=300,
    )
    case_rows = _safe_select_rows(
        client,
        table="cases",
        select_fields="id,title,content,metrics,source_type,source_ref,updated_at",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="updated_at",
        limit=200,
    )
    asset_rows = _safe_select_rows(
        client,
        table="assets",
        select_fields="id,type,summary,content,source_type,source_ref,updated_at",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="updated_at",
        limit=200,
    )
    review_rows = _safe_select_rows(
        client,
        table="reviews",
        select_fields="id,summary_text,success_points,failure_points,improvement,metrics,updated_at",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="updated_at",
        limit=120,
    )
    topic_rows = _safe_select_rows(
        client,
        table="topics",
        select_fields="id,title,status,reason,updated_at",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="updated_at",
        limit=120,
    )
    memory_rows = _safe_select_rows(
        client,
        table="memory_items",
        select_fields="id,title,content,tags,status,confidence,updated_at",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="updated_at",
        limit=120,
    )
    ingestion_rows = _safe_select_rows(
        client,
        table="ingestion_logs",
        select_fields="id,entity_type,status,success_count,failed_count,updated_at,source",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="updated_at",
        limit=120,
    )
    rpa_rows = _safe_select_rows(
        client,
        table="rpa_task_runs",
        select_fields="id,task_type,entity_type,status,accepted_count,rejected_count,updated_at",
        domain_id=domain_id,
        account_id=resolved_account_id,
        order_field="updated_at",
        limit=120,
    )

    prompt_rows: List[Dict[str, Any]] = []
    if resolved_account_id and _table_ready(client, "prompt_versions"):
        prompt_rows = _run_with_db_retry(
            lambda: (
                client.table("prompt_versions")
                .select("id,agent_name,version,status,reason,updated_at")
                .eq("account_id", resolved_account_id)
                .order("updated_at", desc=True)
                .limit(80)
                .execute()
                .data
                or []
            ),
            fallback=[],
        )

    return {
        "domain": domain,
        "account": account or {},
        "account_id": resolved_account_id,
        "sop_current_version": int(
            (
                ((account or {}).get("config_jsonb") or {}).get("sop_latest_version")
                if isinstance((account or {}).get("config_jsonb"), dict)
                else 0
            )
            or 0
        ),
        "pipeline_rows": pipeline_rows,
        "intel_rows": intel_rows,
        "case_rows": case_rows,
        "asset_rows": asset_rows,
        "review_rows": review_rows,
        "topic_rows": topic_rows,
        "memory_rows": memory_rows,
        "ingestion_rows": ingestion_rows,
        "rpa_rows": rpa_rows,
        "prompt_rows": prompt_rows,
        "counts": {
            "cases": _safe_count(client, table="cases", domain_id=domain_id, account_id=resolved_account_id),
            "assets": _safe_count(client, table="assets", domain_id=domain_id, account_id=resolved_account_id),
            "reviews": _safe_count(client, table="reviews", domain_id=domain_id, account_id=resolved_account_id),
            "topics": _safe_count(client, table="topics", domain_id=domain_id, account_id=resolved_account_id),
            "memory_items": _safe_count(client, table="memory_items", domain_id=domain_id, account_id=resolved_account_id),
        },
    }


def _build_yesterday_summary(context: Dict[str, Any]) -> Dict[str, Any]:
    observed_metrics: List[Dict[str, float]] = []
    approved = 0
    rejected = 0
    reason_counter: Dict[str, int] = {}

    for row in context["pipeline_rows"]:
        metrics = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
        published_at = _parse_iso(row.get("published_at"))
        review = row.get("review_jsonb") if isinstance(row.get("review_jsonb"), dict) else {}

        observation = _extract_observation(metrics)
        observed_at = _parse_evidence_time(observation.get("observed_at"))
        if (published_at is not None and observed_at is not None
                and published_at <= observed_at and _in_yesterday_local(observed_at)
                and not str(metrics.get("metrics_mode") or "").startswith("synthetic")):
            observed_metrics.append(observation["values"])

        approved_at = _parse_evidence_time(review.get("approved_at"))
        rejected_at = _parse_evidence_time(review.get("rejected_at"))
        if _in_yesterday_local(approved_at):
            approved += 1
        if _in_yesterday_local(rejected_at):
            rejected += 1
            reason = str(review.get("rejection_reason") or "").strip()
            if reason:
                reason_counter[reason] = reason_counter.get(reason, 0) + 1

    # Aggregate only complete counters in the same actual-observation cohort.
    impressions = None
    interactions = None
    if observed_metrics and all("views" in values for values in observed_metrics):
        impressions = sum(values["views"] for values in observed_metrics)
    interaction_fields = ("likes", "collects", "comments", "shares")
    if observed_metrics and all(all(key in values for key in interaction_fields) for values in observed_metrics):
        interactions = sum(values[key] for values in observed_metrics for key in interaction_fields)

    pass_rate = None
    if approved + rejected > 0:
        pass_rate = approved / float(approved + rejected)
    engagement_rate = None
    if impressions is not None and impressions > 0 and interactions is not None:
        engagement_rate = interactions / impressions

    top_reasons = [
        {"reason": key, "count": value}
        for key, value in sorted(reason_counter.items(), key=lambda item: item[1], reverse=True)[:5]
    ]
    return {
        "date_local": str((_now_local().date() - timedelta(days=1))),
        "impressions": round(impressions, 2) if impressions is not None else None,
        "interactions": round(interactions, 2) if interactions is not None else None,
        "engagement_rate": round(engagement_rate, 6) if engagement_rate is not None else None,
        "review_pass_rate": round(pass_rate, 6) if pass_rate is not None else None,
        "approved_count": approved if approved + rejected else None,
        "rejected_count": rejected if approved + rejected else None,
        "top_fail_reasons": top_reasons,
    }


def _evidence(source_type: str, source_ref: str, timestamp: str) -> Dict[str, Any]:
    return {
        "source_type": source_type,
        "source_ref": source_ref,
        "timestamp": timestamp,
    }


def _build_actions(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    intel_rows = context.get("intel_rows") if isinstance(context.get("intel_rows"), list) else []
    topic_rows = context.get("topic_rows") if isinstance(context.get("topic_rows"), list) else []
    pipeline_rows = context.get("pipeline_rows") if isinstance(context.get("pipeline_rows"), list) else []
    review_rows = context.get("review_rows") if isinstance(context.get("review_rows"), list) else []

    now = _now_utc()
    last_intel = _parse_iso((intel_rows[0] or {}).get("captured_at")) if intel_rows else None
    stale_intel = not last_intel or (now - last_intel) > timedelta(hours=24)
    pending_review_count = sum(1 for row in pipeline_rows if str((row or {}).get("status") or "") == "pending_review")
    todo_topics = sum(1 for row in topic_rows if str((row or {}).get("status") or "") in {"todo", "drafted"})
    low_review_signal = len(review_rows) < 3

    latest_intel_ref = str((intel_rows[0] or {}).get("id") or "none") if intel_rows else "none"
    latest_intel_ts = str((intel_rows[0] or {}).get("captured_at") or "") if intel_rows else ""

    actions: List[Dict[str, Any]] = []
    actions.append(
        {
            "id": str(uuid4()),
            "title": "补齐今日采集输入",
            "description": "先拉取一轮热点和观点输入，确保今天选题和文案有新鲜依据。",
            "reason": "采集时效不足会导致内容滞后，先补数再决策。",
            "acceptance": "新增至少 8 条有效情报，并能看到 24 小时内采集时间。",
            "risk": "若账号登录异常或采集失败，会影响后续选题质量。",
            "evidence_tags": ["KB+FB" if stale_intel else "FB"],
            "evidence_refs": [_evidence("intelligence_items", latest_intel_ref, latest_intel_ts)],
            "requires_confirmation": True,
            "execution_supported": True,
            "execution": {"action_type": "collect_intel", "source_kind": "hotspot"},
            "state": "suggested",
        }
    )

    actions.append(
        {
            "id": str(uuid4()),
            "title": "刷新今日选题池",
            "description": "基于近 14 天反馈和今日热点重排选题优先级。",
            "reason": f"当前待推进选题 {todo_topics} 条，需先排序再出稿。",
            "acceptance": "至少形成 5 条候选选题，并标注优先级与证据。",
            "risk": "如果选题证据不足，可能导致出稿方向偏差。",
            "evidence_tags": ["KB+FB"],
            "evidence_refs": [
                _evidence("topics", str((topic_rows[0] or {}).get("id") or "none") if topic_rows else "none", str((topic_rows[0] or {}).get("updated_at") or "") if topic_rows else ""),
            ],
            "requires_confirmation": True,
            "execution_supported": False,
            "execution": {"action_type": "manual_topic_planning"},
            "state": "suggested",
        }
    )

    actions.append(
        {
            "id": str(uuid4()),
            "title": "推进待审核内容",
            "description": "优先清理待审核队列，把可发布内容推进到下一步。",
            "reason": f"当前待审核 {pending_review_count} 条，积压会拖慢反馈闭环。",
            "acceptance": "待审核队列至少减少 1 条，并补充审核理由。",
            "risk": "审核标准不一致会引入后续返工。",
            "evidence_tags": ["FB"],
            "evidence_refs": [
                _evidence("pipeline_tasks", str((pipeline_rows[0] or {}).get("id") or "none") if pipeline_rows else "none", str((pipeline_rows[0] or {}).get("updated_at") or "") if pipeline_rows else ""),
            ],
            "requires_confirmation": True,
            "execution_supported": False,
            "execution": {"action_type": "manual_review_gate"},
            "state": "suggested",
        }
    )

    actions.append(
        {
            "id": str(uuid4()),
            "title": "回收发布反馈并复盘",
            "description": "拉取近 24h 发布反馈，更新失败原因与优化建议。",
            "reason": "没有反馈复盘就无法稳定优化 SOP。",
            "acceptance": "输出最新反馈汇总并形成 1 条可执行优化动作。",
            "risk": "反馈样本过少时，结论可能不稳定。",
            "evidence_tags": ["FB"],
            "evidence_refs": [
                _evidence("reviews", str((review_rows[0] or {}).get("id") or "none") if review_rows else "none", str((review_rows[0] or {}).get("updated_at") or "") if review_rows else ""),
            ],
            "requires_confirmation": True,
            "execution_supported": True,
            "execution": {"action_type": "reconcile_feedback", "limit": 20},
            "state": "suggested",
        }
    )

    actions.append(
        {
            "id": str(uuid4()),
            "title": "校准提示词版本",
            "description": "根据昨日表现检查当前提示词是否需要微调。",
            "reason": "策略不更新会导致内容质量停滞。",
            "acceptance": "形成 1 条提示词优化提案并进入待确认池。",
            "risk": "过度改写可能破坏现有稳定表现。",
            "evidence_tags": ["KB+FB" if low_review_signal else "FB"],
            "evidence_refs": [
                _evidence("prompt_versions", str((context.get("prompt_rows") or [{}])[0].get("id") or "none") if context.get("prompt_rows") else "none", str((context.get("prompt_rows") or [{}])[0].get("updated_at") or "") if context.get("prompt_rows") else ""),
            ],
            "requires_confirmation": True,
            "execution_supported": False,
            "execution": {"action_type": "manual_prompt_upgrade"},
            "state": "suggested",
        }
    )

    return actions[:5]


def _build_decision_gates(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    pipeline_rows = context.get("pipeline_rows") if isinstance(context.get("pipeline_rows"), list) else []
    topic_rows = context.get("topic_rows") if isinstance(context.get("topic_rows"), list) else []
    review_rows = context.get("review_rows") if isinstance(context.get("review_rows"), list) else []

    pending_review_count = sum(1 for row in pipeline_rows if str((row or {}).get("status") or "") == "pending_review")
    todo_topics = sum(1 for row in topic_rows if str((row or {}).get("status") or "") in {"todo", "drafted"})

    return [
        {
            "gate": "topic_confirm",
            "title": "选题确认门",
            "pending": todo_topics > 0,
            "reason": f"当前有 {todo_topics} 条待推进选题。" if todo_topics > 0 else "选题池状态正常。",
        },
        {
            "gate": "review_decision",
            "title": "审核决策门",
            "pending": pending_review_count > 0,
            "reason": f"当前有 {pending_review_count} 条待审核任务。" if pending_review_count > 0 else "当前无待审核积压。",
        },
        {
            "gate": "retro_confirm",
            "title": "复盘确认门",
            "pending": len(review_rows) > 0,
            "reason": "已有反馈样本，建议确认是否写回 SOP。" if len(review_rows) > 0 else "反馈样本不足，建议先补采。",
        },
    ]


def _build_prompt_suggestions(summary: Dict[str, Any], context: Dict[str, Any]) -> List[Dict[str, Any]]:
    impressions = _metric_value(summary, ["impressions"])
    engagement_rate = _metric_value(summary, ["engagement_rate"])
    review_pass_rate = _metric_value(summary, ["review_pass_rate"])

    suggestions: List[Dict[str, Any]] = []

    if impressions is not None and impressions <= 0:
        suggestions.append(
            {
                "agent_name": "draft_writer",
                "title": "增强开头钩子与关键词密度",
                "reason": "昨日曝光接近 0，优先优化前 2 句和标题关键词。",
                "evidence": {"source": "FB", "metric": "impressions", "value": impressions},
            }
        )

    if engagement_rate is not None and engagement_rate < 0.02:
        suggestions.append(
            {
                "agent_name": "rebuild_copy",
                "title": "补强互动引导句式",
                "reason": "互动率偏低，建议增加问题句和评论引导。",
                "evidence": {"source": "FB", "metric": "engagement_rate", "value": engagement_rate},
            }
        )

    if review_pass_rate is not None and review_pass_rate < 0.6:
        suggestions.append(
            {
                "agent_name": "rebuild_strategy",
                "title": "收紧合规表达模板",
                "reason": "审核通过率偏低，需要降低高风险表达。",
                "evidence": {"source": "FB", "metric": "review_pass_rate", "value": review_pass_rate},
            }
        )

    if not suggestions and all(value is not None for value in (impressions, engagement_rate, review_pass_rate)):
        suggestions.append(
            {
                "agent_name": "draft_writer",
                "title": "维持当前提示词，做小步 AB",
                "reason": "核心指标稳定，建议保持主策略，仅做微调实验。",
                "evidence": {"source": "KB+FB", "metric": "stability", "value": 1},
            }
        )

    return suggestions[:3]
def _cleanup_action_store() -> None:
    now_ts = _now_utc().timestamp()
    with _ACTION_LOCK:
        expired = [
            key
            for key, value in _ACTION_STORE.items()
            if (now_ts - float(value.get("created_ts") or 0.0)) > _ACTION_TTL_SECONDS
        ]
        for key in expired:
            _ACTION_STORE.pop(key, None)


def _store_action(action: Dict[str, Any], *, domain_slug: str, account_id: str, triggered_by: str) -> None:
    _cleanup_action_store()
    with _ACTION_LOCK:
        _ACTION_STORE[action["id"]] = {
            "action": action,
            "domain_slug": domain_slug,
            "account_id": account_id,
            "triggered_by": triggered_by,
            "created_ts": _now_utc().timestamp(),
        }


def _coach_actions_table_ready(client: Client) -> bool:
    return _table_ready(client, "coach_actions")


def _persist_coach_action(
    client: Client,
    *,
    action: Dict[str, Any],
    domain_id: str,
    domain_slug: str,
    account_id: str,
    message: str,
    brief: Dict[str, Any],
    conversation_history: List[Dict[str, Any]] | None,
    triggered_by: str,
) -> None:
    if not _coach_actions_table_ready(client):
        return
    action_id = str(action.get("id") or "").strip()
    if not action_id:
        return
    payload = {
        "action_id": action_id,
        "domain_id": domain_id or None,
        "domain_slug": domain_slug,
        "account_id": account_id or None,
        "triggered_by": triggered_by,
        "message": str(message or "").strip(),
        "action_jsonb": action,
        "brief_jsonb": brief if isinstance(brief, dict) else {},
        "context_jsonb": {
            "conversation_history": (conversation_history or [])[-20:],
            "history_used_turns": len(conversation_history or []),
        },
        "status": "suggested",
    }
    try:
        existing = (
            client.table("coach_actions")
            .select("id")
            .eq("action_id", action_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing:
            client.table("coach_actions").update(payload).eq("action_id", action_id).execute()
        else:
            client.table("coach_actions").insert(payload).execute()
    except Exception:  # noqa: BLE001
        return


def _load_persisted_coach_action(client: Client, *, action_id: str) -> Dict[str, Any] | None:
    if not _coach_actions_table_ready(client):
        return None
    try:
        rows = (
            client.table("coach_actions")
            .select("*")
            .eq("action_id", action_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        return None
    return rows[0] if rows else None


def _update_persisted_coach_action(
    client: Client,
    *,
    action_id: str,
    status: str,
    confirmed_by: str = "",
    result: Dict[str, Any] | None = None,
    error_code: str = "",
    error_message: str = "",
) -> None:
    if not _coach_actions_table_ready(client):
        return
    payload: Dict[str, Any] = {
        "status": status,
        "updated_at": _now_utc().isoformat(),
    }
    if confirmed_by:
        payload["confirmed_by"] = confirmed_by
        payload["confirmed_at"] = _now_utc().isoformat()
    if status in {"completed", "retry_later", "failed", "canceled", "expired"}:
        payload["executed_at"] = _now_utc().isoformat()
    if isinstance(result, dict):
        payload["result_jsonb"] = result
    if error_code:
        payload["error_code"] = error_code
    if error_message:
        payload["error_message"] = error_message[:2000]
    try:
        client.table("coach_actions").update(payload).eq("action_id", action_id).execute()
    except Exception:  # noqa: BLE001
        return


def _normalize_action_status(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _terminal_status_http(status_text: str) -> str:
    normalized = _normalize_action_status(status_text)
    if normalized in {"completed", "canceled"}:
        return "ok"
    if normalized == "retry_later":
        return "degraded"
    if normalized in {"failed", "expired"}:
        return "error"
    return "ok"


def _build_coach_reply(summary: Dict[str, Any], actions: List[Dict[str, Any]]) -> str:
    top_reason = ""
    top_fail_reasons = summary.get("top_fail_reasons") if isinstance(summary.get("top_fail_reasons"), list) else []
    if top_fail_reasons:
        top_reason = str((top_fail_reasons[0] or {}).get("reason") or "").strip()
    metric_text = {
        key: summary[key] if _metric_value(summary, [key]) is not None else "未知"
        for key in ("impressions", "engagement_rate", "review_pass_rate")
    }
    lines = [
        "我先帮你看完昨天的数据，再给今天的执行动作。",
        f"- 昨日曝光：{metric_text['impressions']}",
        f"- 昨日互动率：{metric_text['engagement_rate']}",
        f"- 审核通过率：{metric_text['review_pass_rate']}",
    ]
    if top_reason:
        lines.append(f"- 昨日主要问题：{top_reason}")
    lines.append("")
    lines.append("今天建议先做这 5 步（先建议，确认后执行）：")
    for idx, action in enumerate(actions[:5], start=1):
        lines.append(f"{idx}. {action['title']}：{action['reason']}")
    lines.append("")
    lines.append("你可以直接点“确认执行”来推进我建议的步骤。")
    return "\n".join(lines)


def run_coach_brief(
    client: Client,
    *,
    domain_slug: str,
    account_id: str = "",
) -> Dict[str, Any]:
    try:
        context = _build_context(client, domain_slug=domain_slug, account_id=account_id)
        summary = _build_yesterday_summary(context)
        actions = _build_actions(context)
        decision_gates = _build_decision_gates(context)
        prompt_suggestions = _build_prompt_suggestions(summary, context)
        account = context.get("account") if isinstance(context.get("account"), dict) else {}
        return {
            "status": "ok",
            "domain_slug": domain_slug,
            "account_id": context.get("account_id") or "",
            "account_name": str(account.get("account_name") or ""),
            "window_days": 14,
            "yesterday_summary": summary,
            "today_actions": actions,
            "decision_gates": decision_gates,
            "prompt_upgrade_suggestions": prompt_suggestions,
        "context_snapshot": {
            "domain_id": str((context.get("domain") or {}).get("id") or ""),
            "counts": context.get("counts") or {},
            "intel_count": len(context.get("intel_rows") or []),
            "pipeline_count": len(context.get("pipeline_rows") or []),
            "kb_count": len(context.get("case_rows") or []) + len(context.get("asset_rows") or []),
            "sop_current_version": int(context.get("sop_current_version") or 0),
        },
    }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "degraded",
            "domain_slug": domain_slug,
            "account_id": account_id or "",
            "account_name": "",
            "window_days": 14,
            "yesterday_summary": {},
            "today_actions": [],
            "decision_gates": [],
            "prompt_upgrade_suggestions": [],
            "context_snapshot": {"domain_id": "", "counts": {}, "intel_count": 0, "pipeline_count": 0, "kb_count": 0},
            "message": f"coach brief unavailable: {exc}",
        }


def run_coach_chat(
    client: Client,
    *,
    message: str,
    domain_slug: str,
    account_id: str = "",
    conversation_history: List[Dict[str, Any]] | None = None,
    triggered_by: str = "api:coach",
) -> Dict[str, Any]:
    brief = run_coach_brief(client, domain_slug=domain_slug, account_id=account_id)
    if str(brief.get("status") or "") != "ok":
        return {
            "status": "degraded",
            "domain_slug": domain_slug,
            "account_id": brief.get("account_id") or account_id or "",
            "coach_reply": "教练服务短暂波动，我先保留你的问题。请 20 秒后重试。",
            "requires_confirmation": False,
            "brief": brief,
            "pending_actions": [],
            "decision_gates": [],
            "prompt_upgrade_suggestions": [],
            "next_questions": [],
            "context_info": {
                "history_used_turns": len(conversation_history or []),
                "agent_topology": ["coach_orchestrator"],
                "run_ids": [],
                "sop_current_version": int(((brief.get("context_snapshot") or {}).get("sop_current_version") or 0)),
            },
        }

    actions = brief.get("today_actions") if isinstance(brief.get("today_actions"), list) else []
    domain_id = str((brief.get("context_snapshot") or {}).get("domain_id") or "")
    if not domain_id:
        try:
            domain = _resolve_domain(client, domain_slug)
            domain_id = str(domain.get("id") or "")
        except Exception:  # noqa: BLE001
            domain_id = ""
    resolved_account_id = str(brief.get("account_id") or "")
    for action in actions:
        if not isinstance(action, dict):
            continue
        _store_action(action, domain_slug=domain_slug, account_id=str(brief.get("account_id") or ""), triggered_by=triggered_by)
        _persist_coach_action(
            client,
            action=action,
            domain_id=domain_id,
            domain_slug=domain_slug,
            account_id=resolved_account_id,
            message=message,
            brief=brief,
            conversation_history=conversation_history,
            triggered_by=triggered_by,
        )

    normalized_message = str(message or "").strip()
    reply = _build_coach_reply(brief.get("yesterday_summary") or {}, actions if isinstance(actions, list) else [])
    if normalized_message:
        reply = f"收到，你刚刚说的是：{normalized_message}\n\n{reply}"

    next_questions = [
        "今天你更想先推进“选题”还是先清理“待审核”？",
        "这轮建议里，哪一步你希望我先帮你执行？",
    ]
    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "account_id": brief.get("account_id") or "",
        "coach_reply": reply,
        "requires_confirmation": True,
        "brief": brief,
        "pending_actions": actions,
        "decision_gates": brief.get("decision_gates") or [],
        "prompt_upgrade_suggestions": brief.get("prompt_upgrade_suggestions") or [],
        "next_questions": next_questions[:2],
        "context_info": {
            "history_used_turns": len(conversation_history or []),
            "agent_topology": ["coach_orchestrator", "collector_agent", "analysis_agent", "copy_agent", "review_agent"],
            "run_ids": [],
            "sop_current_version": int(((brief.get("context_snapshot") or {}).get("sop_current_version") or 0)),
        },
    }


def _dispatch_subagent_action(
    client: Client,
    *,
    action_type: str,
    action_id: str,
    domain_id: str,
    domain_slug: str,
    account_id: str,
    execution: Dict[str, Any],
    triggered_by: str,
) -> Dict[str, Any]:
    trace_id = f"{action_id}:{action_type}"

    if action_type == "collect_intel":
        dispatched = submit_subagent_job(
            agent_name="collector_agent",
            payload={"source_kind": str(execution.get("source_kind") or "hotspot"),
                     "execution_method": str(execution.get("execution_method") or ""),
                     "rpa_instruction": execution.get("rpa_instruction")},
            handler=lambda payload: run_collector_agent(
                client,
                domain_slug=domain_slug,
                account_id=account_id,
                payload=payload,
                triggered_by=triggered_by,
                trace_id=trace_id,
            ),
        )
        return {
            "agent_name": "collector_agent",
            "run_id": str(dispatched.get("run_id") or ""),
            "envelope": dispatched.get("result") if isinstance(dispatched.get("result"), dict) else {},
        }

    if action_type == "reconcile_feedback":
        dispatched = submit_subagent_job(
            agent_name="review_agent",
            payload={"limit": max(1, min(50, int(execution.get("limit") or 20)))},
            handler=lambda payload: run_review_agent(
                client,
                domain_slug=domain_slug,
                account_id=account_id,
                payload=payload,
                triggered_by=triggered_by,
                trace_id=trace_id,
            ),
        )
        return {
            "agent_name": "review_agent",
            "run_id": str(dispatched.get("run_id") or ""),
            "envelope": dispatched.get("result") if isinstance(dispatched.get("result"), dict) else {},
        }

    if action_type == "analysis_preview":
        dispatched = submit_subagent_job(
            agent_name="analysis_agent",
            payload={},
            handler=lambda payload: run_analysis_agent(
                client,
                domain_slug=domain_slug,
                domain_id=domain_id,
                account_id=account_id,
                payload=payload,
                triggered_by=triggered_by,
                trace_id=trace_id,
            ),
        )
        return {
            "agent_name": "analysis_agent",
            "run_id": str(dispatched.get("run_id") or ""),
            "envelope": dispatched.get("result") if isinstance(dispatched.get("result"), dict) else {},
        }

    if action_type == "copy_preview":
        dispatched = submit_subagent_job(
            agent_name="copy_agent",
            payload={"topic": str(execution.get("topic") or "日本移民")},
            handler=lambda payload: run_copy_agent(
                client,
                domain_slug=domain_slug,
                account_id=account_id,
                payload=payload,
                triggered_by=triggered_by,
                trace_id=trace_id,
            ),
        )
        return {
            "agent_name": "copy_agent",
            "run_id": str(dispatched.get("run_id") or ""),
            "envelope": dispatched.get("result") if isinstance(dispatched.get("result"), dict) else {},
        }

    return {
        "agent_name": "",
        "run_id": "",
        "envelope": {
            "status": "failed",
            "code": "unsupported_action_type",
            "retryable": False,
            "message": f"unsupported action: {action_type}",
            "trace_id": trace_id,
        },
    }


def _status_from_subagent_envelope(envelope: Dict[str, Any]) -> str:
    status_text = str(envelope.get("status") or "").strip().lower()
    if status_text == "success":
        return "completed"
    if status_text == "retry_later":
        return "retry_later"
    if status_text == "blocked":
        return "failed"
    return "failed"


def run_coach_confirm_action(
    client: Client,
    *,
    action_id: str,
    confirm: bool,
    domain_slug: str,
    account_id: str = "",
    force_reexecute: bool = False,
    triggered_by: str = "api:coach_confirm",
) -> Dict[str, Any]:
    _cleanup_action_store()
    with _ACTION_LOCK:
        stored = _ACTION_STORE.get(action_id)
    persisted = None
    if not stored:
        persisted = _load_persisted_coach_action(client, action_id=action_id)
        if isinstance(persisted, dict):
            stored = {
                "action": persisted.get("action_jsonb") if isinstance(persisted.get("action_jsonb"), dict) else {},
                "domain_slug": str(persisted.get("domain_slug") or domain_slug or "japan_immigration"),
                "account_id": str(persisted.get("account_id") or account_id or ""),
                "triggered_by": str(persisted.get("triggered_by") or triggered_by),
                "created_ts": _now_utc().timestamp(),
            }
    if not stored:
        return {
            "status": "error",
            "error_code": "action_not_found_or_expired",
            "message": "该动作不存在或已过期，请重新向教练发起一次会话。",
        }

    action = stored.get("action") if isinstance(stored.get("action"), dict) else {}
    persisted_status = _normalize_action_status((persisted or {}).get("status")) if isinstance(persisted, dict) else ""
    persisted_result = (persisted or {}).get("result_jsonb") if isinstance((persisted or {}).get("result_jsonb"), dict) else {}
    persisted_executed_at = str((persisted or {}).get("executed_at") or (persisted or {}).get("updated_at") or "").strip() if isinstance(persisted, dict) else ""
    resolved_domain_slug_preview = str(stored.get("domain_slug") or domain_slug or "japan_immigration").strip()
    resolved_account_id_preview = str(stored.get("account_id") or account_id or "").strip()

    if not force_reexecute and persisted_status:
        if confirm and persisted_status in _COACH_ACTION_TERMINAL_STATES:
            next_brief = run_coach_brief(client, domain_slug=resolved_domain_slug_preview, account_id=resolved_account_id_preview)
            return {
                "status": _terminal_status_http(persisted_status),
                "action_id": action_id,
                "state": persisted_status,
                "message": (
                    f"该动作已在 {persisted_executed_at or '之前'} 处理完成，默认不重复执行。"
                    " 如需重跑，请传 force_reexecute=true。"
                ),
                "action": action,
                "result": persisted_result if isinstance(persisted_result, dict) else {},
                "next_brief": next_brief,
                "idempotent": True,
            }
        if confirm and persisted_status in _COACH_ACTION_BUSY_STATES:
            return {
                "status": "degraded",
                "action_id": action_id,
                "state": persisted_status,
                "message": "该动作正在处理中，请稍后刷新结果；如确需重跑，请传 force_reexecute=true。",
                "action": action,
                "result": persisted_result if isinstance(persisted_result, dict) else {"status": "running"},
                "idempotent": True,
            }
        if (not confirm) and persisted_status == "canceled":
            return {
                "status": "ok",
                "action_id": action_id,
                "state": "canceled",
                "message": "该动作已是取消状态。",
                "action": action,
                "result": persisted_result if isinstance(persisted_result, dict) else {"status": "canceled"},
                "idempotent": True,
            }

    if not confirm:
        with _ACTION_LOCK:
            _ACTION_STORE.pop(action_id, None)
        _update_persisted_coach_action(
            client,
            action_id=action_id,
            status="canceled",
            confirmed_by=triggered_by,
            result={"status": "canceled"},
        )
        if isinstance(persisted, dict) and str(persisted.get("domain_id") or "").strip():
            record_action_run(
                client,
                domain_id=str(persisted.get("domain_id") or ""),
                domain_slug=str(persisted.get("domain_slug") or domain_slug),
                account_id=str(persisted.get("account_id") or account_id or ""),
                actor=f"coach:{triggered_by}",
                source="coach.confirm",
                source_ref=action_id,
                action_type=str((action.get("execution") or {}).get("action_type") or "manual_gate"),
                input_jsonb={"action_id": action_id, "confirm": False, "force_reexecute": bool(force_reexecute)},
                result_jsonb={"status": "canceled"},
                status="canceled",
                idempotency_key=action_id,
                trace_id=action_id,
            )
        return {
            "status": "ok",
            "action_id": action_id,
            "state": "canceled",
            "message": "已取消该动作，不会执行。",
            "action": action,
        }

    execution = action.get("execution") if isinstance(action.get("execution"), dict) else {}
    action_type = str(execution.get("action_type") or "").strip()
    resolved_domain_slug = str(stored.get("domain_slug") or domain_slug or "japan_immigration").strip()
    resolved_account_id = str(stored.get("account_id") or account_id or "").strip()
    domain_id = str(persisted.get("domain_id") or "") if isinstance(persisted, dict) else ""
    if not domain_id:
        try:
            domain = _resolve_domain(client, resolved_domain_slug)
            domain_id = str(domain.get("id") or "")
        except Exception:  # noqa: BLE001
            domain_id = ""

    _update_persisted_coach_action(
        client,
        action_id=action_id,
        status="confirmed",
        confirmed_by=triggered_by,
    )

    audience = resolve_actor_audience(triggered_by)
    policies = resolve_tool_policies(client, domain_id=domain_id) if domain_id else {}
    allowed = True
    if policies:
        allowed = is_action_allowed(
            policies=policies,
            audience=audience,
            action_type=action_type,
        )
    policy_snapshot = build_policy_snapshot(
        policies=policies if policies else {"default_action": "allow", "audiences": {}},
        audience=audience,
        action_type=action_type,
        allowed=allowed,
        source="coach.confirm",
    )

    result: Dict[str, Any] = {}
    run_ids: List[str] = []
    state = "completed"
    message = "动作已确认并执行完成。"
    if not allowed:
        state = "failed"
        message = "该动作被策略权限阻止，未执行。"
        result = {"status": "blocked", "error_code": "tool_blocked_by_policy", "action_type": action_type}
    elif not bool(action.get("execution_supported")):
        result = {"status": "acknowledged", "reason": "manual_gate_required"}
        state = "acknowledged"
        message = "该动作需要人工决策，已记录为确认，请继续下一步。"
    elif action_type in {"collect_intel", "reconcile_feedback", "analysis_preview", "copy_preview"}:
        _update_persisted_coach_action(client, action_id=action_id, status="running")
        dispatched = _dispatch_subagent_action(
            client,
            action_type=action_type,
            action_id=action_id,
            domain_id=domain_id,
            domain_slug=resolved_domain_slug,
            account_id=resolved_account_id,
            execution=execution,
            triggered_by=triggered_by,
        )
        run_id = str(dispatched.get("run_id") or "").strip()
        if run_id:
            run_ids.append(run_id)
        envelope = dispatched.get("envelope") if isinstance(dispatched.get("envelope"), dict) else {}
        state = _status_from_subagent_envelope(envelope)
        result = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope
        if state == "retry_later":
            message = "子Agent执行暂时失败，已标记可重试。"
        elif state == "failed":
            message = str(envelope.get("message") or f"子Agent执行失败：{action_type}")[:300]
    else:
        state = "failed"
        message = f"暂不支持的执行动作：{action_type}"
        result = {"status": "error", "error_code": "unsupported_action_type", "action_type": action_type}

    final_status = "completed" if state in {"completed", "acknowledged"} else ("retry_later" if state == "retry_later" else state)
    _update_persisted_coach_action(
        client,
        action_id=action_id,
        status=final_status,
        confirmed_by=triggered_by,
        result=result if isinstance(result, dict) else {"status": "error", "raw": result},
        error_code=str(result.get("error_code") or "").strip() if isinstance(result, dict) else "",
        error_message=str(result.get("error") or result.get("reason") or "").strip() if isinstance(result, dict) else "",
    )

    if domain_id:
        status_for_ledger = "success" if state in {"completed", "acknowledged"} else ("retry_later" if state == "retry_later" else "failed")
        record_action_run(
            client,
            domain_id=domain_id,
            domain_slug=resolved_domain_slug,
            account_id=resolved_account_id,
            actor=f"coach:{triggered_by}",
            source="coach.confirm",
            source_ref=action_id,
            action_type=action_type,
            input_jsonb={"action_id": action_id, "confirm": bool(confirm), "force_reexecute": bool(force_reexecute)},
            result_jsonb=result if isinstance(result, dict) else {"status": "error", "raw": result},
            policy_snapshot=policy_snapshot,
            status=status_for_ledger,
            retryable=state == "retry_later",
            error_code=str(result.get("error_code") or "").strip() if isinstance(result, dict) else "",
            error_message=str(result.get("error") or result.get("reason") or "").strip() if isinstance(result, dict) else "",
            idempotency_key=action_id,
            trace_id=action_id,
        )
        for sub_run_id in run_ids:
            record_action_run(
                client,
                domain_id=domain_id,
                domain_slug=resolved_domain_slug,
                account_id=resolved_account_id,
                actor=f"subagent:{triggered_by}",
                source=f"subagent.{action_type}",
                source_ref=action_id,
                action_type=action_type,
                input_jsonb={"action_id": action_id, "run_id": sub_run_id},
                result_jsonb=result if isinstance(result, dict) else {"status": "error", "raw": result},
                policy_snapshot=policy_snapshot,
                status=status_for_ledger,
                retryable=state == "retry_later",
                error_code=str(result.get("error_code") or "").strip() if isinstance(result, dict) else "",
                error_message=str(result.get("error") or result.get("reason") or "").strip() if isinstance(result, dict) else "",
                idempotency_key=f"{action_id}:{sub_run_id}",
                trace_id=sub_run_id,
            )

    with _ACTION_LOCK:
        _ACTION_STORE.pop(action_id, None)

    next_brief = run_coach_brief(client, domain_slug=resolved_domain_slug, account_id=resolved_account_id)
    return {
        "status": "ok" if state not in {"failed", "retry_later"} else ("degraded" if state == "retry_later" else "error"),
        "action_id": action_id,
        "state": state,
        "message": message,
        "action": action,
        "result": result,
        "run_ids": run_ids,
        "next_brief": next_brief,
        "context_info": {
            "agent_topology": ["coach_orchestrator", "collector_agent", "analysis_agent", "copy_agent", "review_agent"],
            "run_ids": run_ids,
            "sop_current_version": int(((next_brief.get("context_snapshot") or {}).get("sop_current_version") or 0)),
        },
    }
