from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from supabase import Client

from app.model_router import get_router
from app.orchestration.publish_feedback import (
    METRIC_FIELDS, _compute_ces, _extract_observation, _feedback_hours, _parse_iso,
    _normalize_feedback_plan, _pending_review_content, _safe_dict, _upsert_pending_memory_item,
)


def _load_domain(client: Client, domain_slug: str) -> Dict[str, Any] | None:
    res = client.table("domains").select("*").eq("slug", domain_slug).limit(1).execute()
    return res.data[0] if res.data else None


def _load_active_prompt(client: Client, domain: Dict[str, Any]) -> Dict[str, Any]:
    version_id = domain.get("active_strategy_version_id")
    if not version_id:
        return {}
    res = client.table("strategy_versions").select("*").eq("id", version_id).limit(1).execute()
    if not res.data:
        return {}
    return res.data[0].get("prompt_jsonb") or {}


def _load_recent_published(client: Client, domain_id: str) -> List[Dict[str, Any]]:
    start_time = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    res = (
        client.table("pipeline_tasks")
        .select("id,account_id,channel,published_at,payload_jsonb,metrics_jsonb,publish_jsonb")
        .eq("domain_id", domain_id)
        .in_("status", ["published", "metrics_ready", "done", "reflecting", "reflection_failed"])
        .gte("published_at", start_time)
        .order("published_at", desc=True)
        .limit(100)
        .execute()
    )
    rows = res.data or []
    scored: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        observation = _extract_observation(_safe_dict(row.get("metrics_jsonb")))
        metrics = observation["values"]
        published = _parse_iso(row.get("published_at"))
        observed = _parse_iso(observation["observed_at"])
        missing = [name for name in METRIC_FIELDS if name not in metrics]
        publish = _safe_dict(row.get("publish_jsonb"))
        schedule = _normalize_feedback_plan({"checkpoints_hours": publish.get("feedback_schedule_hours")})["checkpoints_hours"]
        comparable = bool(published and observed and published <= observed <= datetime.now(timezone.utc)
                          and not missing and metrics["views"] > 0
                          and (observed - published).total_seconds() >= schedule[-1] * 3600
                          and set(schedule).issubset(_feedback_hours(publish, "feedback_completed_hours"))
                          and not _feedback_hours(publish, "feedback_missed_hours"))
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        score = _compute_ces(metrics)["score"] if comparable else None
        scored.append(
            {
                "id": row.get("id"),
                "title": title,
                "body": body,
                "account_id": row.get("account_id"),
                "metrics": metrics,
                "missing_fields": missing,
                "observation": observation,
                "channel": row.get("channel"),
                "published_at": row.get("published_at"),
                "_score": score,
            }
        )
    scored.sort(key=lambda item: item["_score"] if item["_score"] is not None else -1, reverse=True)
    return scored


def _split_samples(rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if len(rows) < 4:
        return [], []
    half = max(1, len(rows) // 2)
    return rows[:half], rows[-half:]


def reflect_and_upgrade(client: Client, domain_slug: str) -> Dict[str, Any]:
    domain = _load_domain(client, domain_slug)
    if not domain:
        return {"status": "skipped", "reason": "domain_not_found"}

    rows = _load_recent_published(client, domain["id"])
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in rows:
        if row.get("account_id") and row.get("channel") and row["_score"] is not None:
            groups.setdefault((row["account_id"], row["channel"]), []).append(row)
    eligible = [(key, samples) for key, samples in groups.items() if len(samples) >= 4]
    if not eligible:
        return {"status": "hold", "reason": "insufficient_evidence", "sample_count": len(rows),
                "confirmation_required": True, "auto_apply": False}

    current_prompt = _load_active_prompt(client, domain)
    router = get_router()
    suggestions = []
    for (account_id, channel), samples in eligible:
        high_samples, low_samples = _split_samples(samples)
        proposed = router.reflect_strategy(high_samples, low_samples, current_prompt)
        evidence_refs = [{"source_type": "pipeline_tasks.metrics_jsonb", "source_ref": row["id"],
                          "task_id": row["id"], "account_id": account_id, "domain_id": domain["id"],
                          "timestamp": row["observation"]["observed_at"], "observation": row["observation"]}
                         for row in samples]
        accounts = client.table("channel_accounts").select("config_jsonb").eq("id", account_id).limit(1).execute().data or []
        config = _safe_dict(accounts[0].get("config_jsonb")) if accounts else {}
        content = _pending_review_content(config, evidence_refs, proposed, _safe_dict(proposed))
        suggestion = {"account_id": account_id, "channel": channel, "proposed_prompt": proposed,
                      "evidence_refs": evidence_refs, "confirmation_required": True,
                      "auto_apply": False, "causal_claim": False}
        _upsert_pending_memory_item(
            client, domain_id=domain["id"], account_id=account_id,
            title=f"Reflection pending:{account_id}:{channel}",
            content=content,
            tags=["feedback", "reflection", "pending_confirmation"], created_by="system:reflector",
        )
        suggestions.append(suggestion)
    return {"status": "pending_confirmation", "domain_slug": domain_slug,
            "suggestions": suggestions, "confirmation_required": True, "auto_apply": False}
