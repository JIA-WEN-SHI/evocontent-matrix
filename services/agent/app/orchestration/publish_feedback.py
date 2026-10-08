from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import math
import json
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse

from supabase import Client

from app.tools.collection.playwright_tools import collect_recent_published_posts
from app.tools.integrations.xhs_mcp_readonly import sync_pipeline_task_metrics_via_mcp
from app.orchestration.review_history import compare_review_history

DEFAULT_FEEDBACK_PLAN = {
    "checkpoints_hours": [1, 3, 24],
    "require_real_metrics_for_upgrade": True,
    "synthetic_preview_enabled": False,
    "auto_retro_after_last_checkpoint": True,
}

FEED_ID_RE = re.compile(r"/explore/([0-9a-zA-Z]+)")
METRIC_FIELDS = ("views", "likes", "collects", "comments", "shares", "follows")
_CANDIDATE_SCAN_OFFSETS: Dict[Tuple[str, str, bool | None], int] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_title(value: Any) -> str:
    text = _text(value)
    return re.sub(r"\s+", "", text).lower()


def _parse_url_identity(url: str) -> Dict[str, str]:
    text = _text(url)
    if not text:
        return {}
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    match = FEED_ID_RE.search(parsed.path)
    result: Dict[str, str] = {}
    if match:
        result["feed_id"] = match.group(1)
        result["remote_post_id"] = match.group(1)
    token = query.get("xsec_token", [""])[0].strip()
    if token:
        result["xsec_token"] = token
    if text:
        result["published_url"] = text
    return result


def _normalize_feedback_plan(raw: Any) -> Dict[str, Any]:
    source = _safe_dict(raw)
    checkpoints: List[int] = []
    for item in _safe_list(source.get("checkpoints_hours")):
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            checkpoints.append(value)
    checkpoints = sorted(set(checkpoints or DEFAULT_FEEDBACK_PLAN["checkpoints_hours"]))
    return {
        "checkpoints_hours": checkpoints,
        "require_real_metrics_for_upgrade": bool(
            source.get("require_real_metrics_for_upgrade", DEFAULT_FEEDBACK_PLAN["require_real_metrics_for_upgrade"])
        ),
        "synthetic_preview_enabled": bool(
            source.get("synthetic_preview_enabled", DEFAULT_FEEDBACK_PLAN["synthetic_preview_enabled"])
        ),
        "auto_retro_after_last_checkpoint": bool(
            source.get("auto_retro_after_last_checkpoint", DEFAULT_FEEDBACK_PLAN["auto_retro_after_last_checkpoint"])
        ),
    }


def _load_domain(client: Client, domain_slug: str) -> Dict[str, Any]:
    rows = (
        client.table("domains")
        .select("id,slug,name")
        .eq("slug", domain_slug)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise ValueError(f"domain_not_found:{domain_slug}")
    return rows[0]


def _load_accounts(client: Client) -> Dict[str, Dict[str, Any]]:
    rows = (
        client.table("channel_accounts")
        .select("id,account_name,account_handle,config_jsonb")
        .limit(500)
        .execute()
    ).data or []
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _load_candidate_tasks(
    client: Client, domain_id: str, account_id: str, limit: int,
    accounts: Dict[str, Dict[str, Any]] | None = None,
    pipeline_task_id: str = "",
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 50))
    page_size = 50
    if pipeline_task_id:
        query = (client.table("pipeline_tasks")
                 .select("id,domain_id,account_id,channel,status,stage,payload_jsonb,publish_jsonb,metrics_jsonb,published_at,created_at,updated_at")
                 .eq("domain_id", domain_id).eq("id", pipeline_task_id)
                 .in_("status", ["published", "publish_failed", "metrics_ready", "done", "reflecting", "reflection_failed"]))
        if account_id:
            query = query.eq("account_id", account_id)
        rows = query.limit(1).execute().data or []
        return [row for row in rows if _has_feedback_work(row, (accounts or {}).get(_text(row.get("account_id")), {}))]
    candidates: List[Dict[str, Any]] = []
    rotation_key = (domain_id, account_id, None)
    terminal_first = bool(_CANDIDATE_SCAN_OFFSETS.get(rotation_key, 0))
    for terminal in (terminal_first, not terminal_first):
        statuses = ["done"] if terminal else ["published", "publish_failed", "metrics_ready", "reflecting", "reflection_failed"]
        scan_key = (domain_id, account_id, terminal)
        offset = _CANDIDATE_SCAN_OFFSETS.get(scan_key, 0)
        for _ in range(5):
            query = (client.table("pipeline_tasks")
                     .select("id,domain_id,account_id,channel,status,stage,payload_jsonb,publish_jsonb,metrics_jsonb,published_at,created_at,updated_at")
                     .eq("domain_id", domain_id).in_("status", statuses)
                     .order("updated_at").order("id").range(offset, offset + page_size - 1))
            if account_id:
                query = query.eq("account_id", account_id)
            rows = query.execute().data or []
            for row in rows:
                account = (accounts or {}).get(_text(row.get("account_id")), {})
                if _has_feedback_work(row, account):
                    candidates.append(row)
                    if len(candidates) >= limit:
                        _CANDIDATE_SCAN_OFFSETS[scan_key] = 0
                        _CANDIDATE_SCAN_OFFSETS[rotation_key] = int(not terminal)
                        return candidates
            offset += len(rows)
            if len(rows) < page_size:
                offset = 0
                break
        # Rotate bounded scans past unchanged no-work rows on subsequent polls.
        if len(_CANDIDATE_SCAN_OFFSETS) >= 128 and scan_key not in _CANDIDATE_SCAN_OFFSETS:
            _CANDIDATE_SCAN_OFFSETS.pop(next(iter(_CANDIDATE_SCAN_OFFSETS)))
        _CANDIDATE_SCAN_OFFSETS[scan_key] = offset
    return candidates


def _extract_identity(task: Dict[str, Any]) -> Dict[str, str]:
    payload = _safe_dict(task.get("payload_jsonb"))
    publish_jsonb = _safe_dict(task.get("publish_jsonb"))
    identity = _safe_dict(publish_jsonb.get("identity"))
    last_result = _safe_dict(publish_jsonb.get("last_result"))
    merged: Dict[str, str] = {
        "feed_id": _text(identity.get("feed_id") or last_result.get("feed_id")),
        "xsec_token": _text(identity.get("xsec_token") or last_result.get("xsec_token")),
        "published_url": _text(identity.get("published_url") or last_result.get("published_url")),
        "remote_post_id": _text(identity.get("remote_post_id") or last_result.get("remote_post_id")),
        "recovered_by": _text(identity.get("recovered_by")),
    }
    for candidate in [
        _text(payload.get("published_url")),
        _text(last_result.get("published_url")),
        _text(identity.get("published_url")),
    ]:
        parsed = _parse_url_identity(candidate)
        for key, value in parsed.items():
            if value and not merged.get(key):
                merged[key] = value
    # Fallback: if we already know feed_id, always synthesize a canonical post URL.
    if merged.get("feed_id") and not merged.get("published_url"):
        token = _text(merged.get("xsec_token"))
        if token:
            merged["published_url"] = f"https://www.xiaohongshu.com/explore/{merged['feed_id']}?xsec_token={token}"
        else:
            merged["published_url"] = f"https://www.xiaohongshu.com/explore/{merged['feed_id']}"
    if merged.get("feed_id") and not merged.get("remote_post_id"):
        merged["remote_post_id"] = merged["feed_id"]
    return {key: value for key, value in merged.items() if value}


def _extract_real_metrics(metrics_jsonb: Dict[str, Any]) -> Tuple[Dict[str, float], bool]:
    if str(metrics_jsonb.get("metrics_mode") or "").startswith("synthetic"):
        return {}, False
    if isinstance(metrics_jsonb.get("observation"), dict):
        source = _safe_dict(metrics_jsonb["observation"].get("values"))
    elif isinstance(metrics_jsonb.get("post_metrics"), dict):
        source = metrics_jsonb["post_metrics"]
    else:
        source = metrics_jsonb
    aliases = {
        "views": ["views", "view_count"], "likes": ["likes", "like_count", "liked_count"],
        "collects": ["collects", "favorite_count", "collect_count", "collected_count"],
        "comments": ["comments_count", "comment_count", "comments"], "shares": ["shares", "share_count"],
        "follows": ["followers_delta", "follows"],
    }
    metrics: Dict[str, float] = {}
    for name, keys in aliases.items():
        for key in keys:
            value = source.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number) and (number >= 0 or name == "follows"):
                metrics[name] = number
                break
    return metrics, bool(metrics)


def _extract_observation(metrics_jsonb: Dict[str, Any]) -> Dict[str, Any]:
    observation = _safe_dict(metrics_jsonb.get("observation"))
    values, _ = _extract_real_metrics(metrics_jsonb)
    timestamp = (observation.get("observed_at") if isinstance(metrics_jsonb.get("observation"), dict)
                 else metrics_jsonb.get("post_metrics_synced_at"))
    return {"values": values, "observed_at": timestamp,
            "provider": observation.get("provider") or metrics_jsonb.get("mcp_source") or "persisted",
            "source_ref": observation.get("source_ref"), "provenance": _safe_dict(observation.get("provenance")),
            "observation_id": metrics_jsonb.get("observation_id")}


def _extract_comparable_metrics(metrics_jsonb: Dict[str, Any]) -> Tuple[Dict[str, float], bool]:
    observation = _extract_observation(metrics_jsonb)
    observed = _parse_iso(observation["observed_at"])
    values = observation["values"]
    ready = bool(observed and observed <= _now() and set(METRIC_FIELDS).issubset(values) and values["views"] > 0)
    return (values, True) if ready else ({}, False)


def _observation_checkpoint(
    published_at: datetime, observed_at: datetime, checkpoints: List[int], completed: List[int],
) -> Dict[str, Any]:
    if published_at.tzinfo is None or observed_at.tzinfo is None:
        return {"checkpoint_hours": None, "missed_hours": []}
    age_hours = (observed_at - published_at).total_seconds() / 3600
    eligible = sorted({hour for hour in checkpoints if 0 < hour <= age_hours})
    latest = eligible[-1] if eligible else None
    return {"checkpoint_hours": latest if latest not in completed else None,
            "missed_hours": [hour for hour in eligible[:-1] if hour not in completed]}


def _feedback_hours(publish_jsonb: Dict[str, Any], key: str) -> List[int]:
    return sorted({value for value in _safe_list(publish_jsonb.get(key))
                   if isinstance(value, int) and not isinstance(value, bool) and value > 0})


def _has_feedback_work(task: Dict[str, Any], account: Dict[str, Any]) -> bool:
    publish = _safe_dict(task.get("publish_jsonb"))
    plan = _normalize_feedback_plan(_safe_dict(account.get("config_jsonb")).get("feedback_plan"))
    published = _parse_iso(task.get("published_at") or publish.get("published_at"))
    observation = _extract_observation(_safe_dict(task.get("metrics_jsonb")))
    observed = _parse_iso(observation["observed_at"])
    review = _safe_dict(_safe_dict(publish.get("feedback_analysis")).get("basic_review"))
    if task.get("status") == "done" and not review and not publish.get("last_feedback_at") and not observation["observation_id"]:
        return False
    reviewed = _parse_iso(review.get("observed_at") or publish.get("last_feedback_at"))
    if (published and observed and published <= observed <= _now() and observation["values"]
            and (reviewed is None or observed >= reviewed)
            and (review.get("observed_at") != observed.isoformat() or review.get("values") != observation["values"]
                 or review.get("observation_id") != observation["observation_id"])):
        return True
    if task.get("status") == "done":
        return False
    resolved = _feedback_hours(publish, "feedback_completed_hours") + _feedback_hours(publish, "feedback_missed_hours")
    return bool(_compute_due_checkpoints(published, plan["checkpoints_hours"], resolved))


def _norm(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return max(0.0, min(1.0, float(value) / scale))


def _compute_ces(metrics: Dict[str, float]) -> Dict[str, Any]:
    score = (
        _norm(metrics.get("views", 0), 5000) * 0.10
        + _norm(metrics.get("likes", 0), 300) * 0.10
        + _norm(metrics.get("collects", 0), 120) * 0.30
        + _norm(metrics.get("comments", 0), 80) * 0.25
        + _norm(metrics.get("shares", 0), 50) * 0.15
        + _norm(metrics.get("follows", 0), 40) * 0.10
    )
    band = "low"
    if score >= 0.65:
        band = "high"
    elif score >= 0.35:
        band = "mid"
    observations: List[str] = []
    if metrics.get("collects", 0) >= metrics.get("likes", 0) * 0.3:
        observations.append("收藏占比较高，说明内容更像工具型信息。")
    if metrics.get("comments", 0) >= max(3.0, metrics.get("likes", 0) * 0.15):
        observations.append("评论占比较高，说明情绪共鸣或求助意愿较强。")
    return {
        "score": round(score, 4),
        "band": band,
        "weight_breakdown": {
            "views": 0.10,
            "likes": 0.10,
            "collects": 0.30,
            "comments": 0.25,
            "shares": 0.15,
            "follows": 0.10,
        },
        "observations": observations or ["当前样本偏少，先继续观察。"],
    }


def _compute_attribution(metrics: Dict[str, float]) -> Dict[str, Any]:
    views = metrics.get("views", 0)
    likes = metrics.get("likes", 0)
    collects = metrics.get("collects", 0)
    comments = metrics.get("comments", 0)
    follows = metrics.get("follows", 0)
    exposure = {
        "layer": "exposure",
        "diagnosis": "healthy",
        "reason": "曝光与点击代理正常",
        "next_action": "保持当前标题与封面策略",
    }
    value = {
        "layer": "value",
        "diagnosis": "healthy",
        "reason": "高价值互动正常",
        "next_action": "保持当前结构与信息密度",
    }
    conversion = {
        "layer": "conversion",
        "diagnosis": "healthy",
        "reason": "行动意向正常",
        "next_action": "保持当前 CTA 与人设承接",
    }
    primary = "none"
    if views >= 300 and likes <= max(8, views * 0.01):
        exposure = {
            "layer": "exposure",
            "diagnosis": "hook_weak",
            "reason": "展现代理数据存在，但点击/浅互动不足",
            "next_action": "下次加强标题痛点词、反差词和第一屏钩子。",
        }
        primary = "exposure"
    if views >= 200 and collects <= max(2, views * 0.005):
        value = {
            "layer": "value",
            "diagnosis": "density_or_structure_weak",
            "reason": "阅读代理存在，但收藏不足，说明信息密度或结构承接弱。",
            "next_action": "强化总分总、清单体和数据背书，减少空话。",
        }
        primary = "value"
    if collects >= max(5, likes * 0.3) and comments + follows <= max(2, collects * 0.15):
        conversion = {
            "layer": "conversion",
            "diagnosis": "cta_or_trust_weak",
            "reason": "内容有价值但未形成行动，CTA 或人设信任承接不足。",
            "next_action": "结尾加入具体行动引导，并强化专家/过来人语气。",
        }
        primary = "conversion"
    return {
        "exposure": exposure,
        "value": value,
        "conversion": conversion,
        "primary_bottleneck": primary if primary != "none" else ("value" if collects < likes * 0.2 else "exposure"),
    }


def _compute_ee(task: Dict[str, Any]) -> Dict[str, Any]:
    payload = _safe_dict(task.get("payload_jsonb"))
    analysis = _safe_dict(payload.get("analysis_jsonb"))
    mode = "exploit"
    if payload.get("is_explore") is True or analysis.get("explore") is True:
        mode = "explore"
    if "实验" in _text(payload.get("title")) or "测试" in _text(payload.get("title")):
        mode = "explore"
    return {
        "mode": mode,
        "recommendation": "保持 80/20 分配",
        "next_mix": {"exploit_ratio": 0.8, "explore_ratio": 0.2},
        "reason": "当前账号既要稳基本盘，也要保留新角度测试。",
    }


def _compute_feature_weights(task: Dict[str, Any], metrics: Dict[str, float], attribution: Dict[str, Any]) -> Dict[str, Any]:
    payload = _safe_dict(task.get("payload_jsonb"))
    title = _text(payload.get("title"))
    topic_name = _text(payload.get("topic")) or (title[:16] if title else "默认话题")
    bottleneck = _text(attribution.get("primary_bottleneck"))
    collects = metrics.get("collects", 0)
    comments = metrics.get("comments", 0)
    return {
        "topic": [
            {
                "name": topic_name,
                "delta": round(0.12 if collects >= 3 else -0.05, 2),
                "weight": round(0.6 if collects >= 3 else 0.35, 2),
                "reason": "收藏率较高" if collects >= 3 else "高价值互动偏弱",
            }
        ],
        "emotion": [
            {
                "name": "信息差 + 风险提示",
                "delta": round(0.08 if comments >= 2 else 0.0, 2),
                "weight": round(0.5 if comments >= 2 else 0.42, 2),
                "reason": "评论区有讨论" if comments >= 2 else "情绪共鸣一般",
            }
        ],
        "format": [
            {
                "name": "长文解释体",
                "delta": round(-0.06 if bottleneck == "value" else 0.07, 2),
                "weight": round(0.4 if bottleneck == "value" else 0.58, 2),
                "reason": "结构需要加强" if bottleneck == "value" else "结构承接正常",
            }
        ],
        "cta": [
            {
                "name": "评论区引导",
                "delta": round(-0.08 if bottleneck == "conversion" else 0.05, 2),
                "weight": round(0.3 if bottleneck == "conversion" else 0.48, 2),
                "reason": "转化承接不足" if bottleneck == "conversion" else "CTA 有基础效果",
            }
        ],
        "persona_expression": [
            {
                "name": "专业解释型",
                "delta": 0.05,
                "weight": 0.55,
                "reason": "当前账号需要稳住专业信任感。",
            }
        ],
    }


def _build_decision_summary(
    metrics_mode: str,
    ces: Dict[str, Any],
    attribution: Dict[str, Any],
) -> Dict[str, Any]:
    worked: List[str] = []
    failed: List[str] = []
    next_actions: List[str] = []
    if ces.get("band") in {"mid", "high"}:
        worked.append("当前内容具备基础可读性，账号方向没有跑偏。")
    if attribution.get("primary_bottleneck") == "exposure":
        failed.append("标题/钩子不足，入口吸引力不够。")
        next_actions.append("下一轮标题增强痛点词和反差词。")
    if attribution.get("primary_bottleneck") == "value":
        failed.append("信息密度和结构承接偏弱。")
        next_actions.append("下一轮正文改成更明确的清单体或总分总结构。")
    if attribution.get("primary_bottleneck") == "conversion":
        failed.append("CTA 和信任承接不足，叫好不叫座。")
        next_actions.append("下一轮在结尾加入更具体的评论/评估引导。")
    if metrics_mode != "real":
        next_actions.append("当前仅临时反馈，先等待真实指标后再决定升级。")
    return {
        "what_worked": worked or ["先完成发布确认，当前还在积累样本。"],
        "what_failed": failed,
        "next_actions": next_actions or ["继续按反馈计划回收数据。"],
    }


def _build_prompt_upgrade_targets(attribution: Dict[str, Any], metrics_mode: str) -> List[Dict[str, Any]]:
    if metrics_mode != "real":
        return []
    bottleneck = _text(attribution.get("primary_bottleneck"))
    if bottleneck == "exposure":
        return [
            {
                "agent_name": "draft_writer",
                "change_type": "adjust_hook",
                "reason": "曝光层表现弱，需要增强标题和首屏钩子。",
            }
        ]
    if bottleneck == "value":
        return [
            {
                "agent_name": "rebuild_copy",
                "change_type": "adjust_structure",
                "reason": "价值层偏弱，需要强化结构和信息密度。",
            }
        ]
    return [
        {
            "agent_name": "rebuild_strategy",
            "change_type": "adjust_cta",
            "reason": "转化层偏弱，需要调整 CTA 和人设承接。",
        }
    ]


def _upsert_pending_memory_item(
    client: Client,
    *,
    domain_id: str,
    account_id: str,
    title: str,
    content: str,
    tags: List[str],
    confidence: float = 0.0,
    created_by: str = "system:publish_feedback",
) -> None:
    if not account_id:
        return
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
        "tags": tags[:20],
        "confidence": max(0.0, min(1.0, confidence)),
        "updated_at": _now().isoformat(),
    }
    if existing:
        client.table("memory_items").update(payload).eq("id", existing[0]["id"]).execute()
        return
    client.table("memory_items").insert(
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "type": "strategy_rule",
            "title": title[:200],
            "content": content[:4000],
            "tags": tags[:20],
            "status": "pending",
            "confidence": max(0.0, min(1.0, confidence)),
            "created_by": created_by[:120],
        }
    ).execute()


def _pending_review_content(
    config: Dict[str, Any], evidence_refs: List[Dict[str, Any]], recommendations: Any,
    proposed: Dict[str, Any] | None = None,
) -> str:
    body: Dict[str, Any] = {"evidence_refs": evidence_refs[:10], "recommendations": recommendations,
                            "confirmation_required": True, "auto_apply": False, "causal_claim": False,
                            "has_own_account_evidence": bool(evidence_refs), "hold": True, "executable": False,
                            "reason": "no_valid_account_change_proposed"}
    proposal = _safe_dict(_safe_dict(proposed).get("proposal"))
    allowed = {
        "feedback_plan": set(DEFAULT_FEEDBACK_PLAN) | {"exposure_gate"},
        "collection_plan": {"mode", "steps", "fallback", "notes", "daily_schedule", "loop_gate", "topic_refresh"},
        "publish_preferences": {"next_publish_slot_local", "min_action_gap_seconds", "max_action_gap_seconds", "updated_by_feedback"},
    }
    target = proposal.get("target")
    before, after = proposal.get("before"), proposal.get("after")
    version = config.get("sop_latest_version", 0)
    if (isinstance(target, str) and target in allowed and evidence_refs and isinstance(before, dict)
            and isinstance(after, dict) and before == _safe_dict(config.get(target)) and before != after
            and type(version) is int and not _safe_dict(proposed).get("hold")
            and not _safe_dict(proposed).get("insufficient_evidence")):
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        if not changed - allowed[target]:
            body.update(hold=False, executable=True, reason="pending_account_change")
            body["proposal"] = {**proposal, "before_version": version}
    content = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(content) > 4000:
        body = {"hold": True, "executable": False, "reason": "suggestion_exceeds_storage_limit",
                "evidence_refs": evidence_refs[:1], "confirmation_required": True, "auto_apply": False}
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(content) > 4000:
        raise ValueError("review_evidence_exceeds_storage_limit")
    return content


def _feedback_guardrail_proposal(config: Dict[str, Any]) -> Dict[str, Any] | None:
    before = _safe_dict(config.get("feedback_plan"))
    after = dict(before)
    if before.get("require_real_metrics_for_upgrade") is False:
        after["require_real_metrics_for_upgrade"] = True
    if before.get("synthetic_preview_enabled") is True:
        after["synthetic_preview_enabled"] = False
    if after == before:
        return None
    return {"proposal": {"target": "feedback_plan", "before": before, "after": after,
                         "reason": "Own-account observations are available; require real evidence and disable synthetic previews for subsequent reviews."}}


def _recover_identity_from_recent_posts(
    task: Dict[str, Any],
    recent_items: List[Dict[str, Any]],
) -> Dict[str, str]:
    payload = _safe_dict(task.get("payload_jsonb"))
    target_title = _normalize_title(payload.get("title"))
    if not target_title:
        return {}
    for item in recent_items:
        item_title = _normalize_title(item.get("title"))
        if not item_title:
            continue
        if target_title == item_title or target_title in item_title or item_title in target_title:
            identity = _parse_url_identity(_text(item.get("url")))
            if identity:
                identity["recovered_by"] = "title_match"
                return identity
    return {}


def _compute_due_checkpoints(
    published_at: datetime | None,
    checkpoints: List[int],
    completed: List[int],
) -> List[int]:
    if published_at is None:
        return []
    age_hours = (_now() - published_at).total_seconds() / 3600
    return [hour for hour in checkpoints if age_hours >= hour and hour not in completed]


def _feedback_result(task: Dict[str, Any], publish: Dict[str, Any], *, changed: bool, suggestions_created: int = 0) -> Dict[str, Any]:
    analysis = _safe_dict(publish.get("feedback_analysis"))
    basic = _safe_dict(analysis.get("basic_review"))
    metrics = _safe_dict(basic.get("values"))
    return {
        "task_id": task.get("id"), "status": task.get("status"), "stage": task.get("stage"),
        "feedback_state": publish.get("feedback_state"), "identity_recovered": bool(_extract_identity(task).get("feed_id")),
        "metrics_mode": analysis.get("metrics_mode", "pending"), "real_metrics": bool(metrics),
        "next_feedback_at": publish.get("next_feedback_at"), "suggestions_created": suggestions_created,
        "ces_score": _safe_dict(analysis.get("ces")).get("score"), "metrics": metrics,
        "decision_summary": _safe_dict(analysis.get("decision_summary")),
        "history_comparison": _safe_dict(analysis.get("history_comparison")), "basic_review": basic,
        "full_comparison_ready": bool(analysis.get("full_comparison_ready")),
        "feedback_completed_hours": _feedback_hours(publish, "feedback_completed_hours"),
        "feedback_missed_hours": _feedback_hours(publish, "feedback_missed_hours"),
        "provider_status": publish.get("feedback_provider_status"), "provider_error": publish.get("feedback_provider_error"),
        "changed": changed, "observation_id": basic.get("observation_id"),
        "metrics_captured_at": basic.get("observed_at"), "updated_at": task.get("updated_at"),
    }


def _stale_feedback_result(task: Dict[str, Any]) -> Dict[str, Any]:
    metrics, real = _extract_real_metrics(_safe_dict(task.get("metrics_jsonb")))
    return {**_feedback_result(task, _safe_dict(task.get("publish_jsonb")), changed=False),
            "status": "stale", "task_status": task.get("status"), "feedback_state": "stale_write",
            "error": "feedback_snapshot_changed", "retryable": True, "real_metrics": real,
            "metrics_mode": "real" if real else "pending", "metrics": metrics, "metrics_stale": True,
            "full_comparison_ready": False, "ces_score": None,
            "basic_review": {"status": "pending", "reason": "stale_write"}, "history_comparison": {},
            "decision_summary": {}}


def _feedback_cas_filters(task: Dict[str, Any], domain_id: str) -> List[Tuple[str, Any]] | None:
    publish = _safe_dict(task.get("publish_jsonb"))
    metrics = _safe_dict(task.get("metrics_jsonb"))
    identity = _safe_dict(publish.get("identity"))
    observation = _safe_dict(metrics.get("observation"))
    filters = [("id", task.get("id")), ("domain_id", domain_id)]
    filters.extend((key, task.get(key)) for key in ("account_id", "updated_at", "status", "published_at"))
    filters.extend([
        ("publish_jsonb->manual_publication->>revision", _safe_dict(publish.get("manual_publication")).get("revision")),
        ("publish_jsonb->identity->>feed_id", identity.get("feed_id")),
        ("publish_jsonb->identity->>remote_post_id", identity.get("remote_post_id")),
        ("metrics_jsonb->>observation_id", metrics.get("observation_id")),
        ("metrics_jsonb->>post_metrics_synced_at", metrics.get("post_metrics_synced_at")),
        ("metrics_jsonb->observation->>observed_at", observation.get("observed_at")),
    ])
    if isinstance(metrics.get("observation"), dict):
        source, prefix = _safe_dict(observation.get("values")), "metrics_jsonb->observation->values->>"
    elif isinstance(metrics.get("post_metrics"), dict):
        source, prefix = metrics["post_metrics"], "metrics_jsonb->post_metrics->>"
    else:
        source, prefix = metrics, "metrics_jsonb->>"
    counters = {"views", "likes", "collects", "comments_count", "comments", "shares", "followers_delta", "follows"}
    counters.update(key for key in ("view_count", "like_count", "liked_count", "favorite_count", "collect_count",
                                   "collected_count", "comment_count", "share_count") if key in source)
    filters.extend((prefix + key, source.get(key)) for key in sorted(counters))
    # Unversioned legacy rows need complete equality; refuse it when it cannot fit.
    if _parse_iso(task.get("updated_at")) is None:
        try:
            filters.extend((key, json.dumps(task[key], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                            if task.get(key) is not None else None) for key in ("publish_jsonb", "metrics_jsonb"))
        except (TypeError, ValueError):
            return None
    if any(value is not None and not isinstance(value, (str, int, float, bool)) for _, value in filters):
        return None
    encoded = urlencode([(key, "is.null" if value is None else f"eq.{value}") for key, value in filters], quote_via=quote)
    return filters if len(encoded) <= 3000 else None


def _reconcile_task(
    client: Client,
    *,
    task: Dict[str, Any],
    domain_id: str,
    account: Dict[str, Any] | None,
    recent_posts_cache: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    task_id = _text(task.get("id"))
    account_id = _text(task.get("account_id"))
    payload = _safe_dict(task.get("payload_jsonb"))
    publish_jsonb = _safe_dict(task.get("publish_jsonb"))
    metrics_jsonb = _safe_dict(task.get("metrics_jsonb"))
    write_snapshot = task
    config = _safe_dict(_safe_dict(account or {}).get("config_jsonb"))
    plan = _normalize_feedback_plan(config.get("feedback_plan"))
    is_cli = _safe_dict(config.get("collection_plan")).get("mode") == "xhs_cli"
    identity = _extract_identity(task)
    if not is_cli and account_id and not identity.get("feed_id"):
        if account_id not in recent_posts_cache:
            recent_result = collect_recent_published_posts(client, account_id=account_id, limit=8)
            recent_posts_cache[account_id] = _safe_list(recent_result.get("items"))
        recovered = _recover_identity_from_recent_posts(task, recent_posts_cache.get(account_id, []))
        identity = {**identity, **recovered}
    if identity.get("feed_id") and not identity.get("remote_post_id"):
        identity["remote_post_id"] = identity["feed_id"]

    feedback_completed = _feedback_hours(publish_jsonb, "feedback_completed_hours")
    missed = _feedback_hours(publish_jsonb, "feedback_missed_hours")
    published_at = _parse_iso(task.get("published_at") or publish_jsonb.get("published_at"))
    due_checkpoints = _compute_due_checkpoints(published_at, plan["checkpoints_hours"], feedback_completed + missed)
    observation = _extract_observation(metrics_jsonb)
    observed_at = _parse_iso(observation["observed_at"])
    existing_review = _safe_dict(_safe_dict(publish_jsonb.get("feedback_analysis")).get("basic_review"))
    reviewed_at = _parse_iso(existing_review.get("observed_at"))
    if reviewed_at and observed_at and observed_at < reviewed_at:
        return _feedback_result(task, publish_jsonb, changed=False)
    new_observation = bool(observed_at and observation["values"]
                           and (existing_review.get("observed_at") != observed_at.isoformat()
                                or existing_review.get("values") != observation["values"]
                                or existing_review.get("observation_id") != observation["observation_id"]))
    provider_status = ("persisted" if is_cli or new_observation
                       else publish_jsonb.get("feedback_provider_status", "not_requested"))
    provider_error = None if new_observation else publish_jsonb.get("feedback_provider_error")
    if not is_cli and identity.get("feed_id") and due_checkpoints and not new_observation:
        sync_task = {**task, "publish_jsonb": {**publish_jsonb, "identity": identity}}
        try:
            sync_result = sync_pipeline_task_metrics_via_mcp(client, sync_task)
            provider_status = _text(sync_result.get("status")) or "failed"
            provider_error = sync_result.get("reason") or sync_result.get("error")
            if provider_status == "ok":
                query = (client.table("pipeline_tasks")
                         .select("id,domain_id,account_id,status,stage,published_at,payload_jsonb,metrics_jsonb,publish_jsonb,updated_at")
                         .eq("id", task_id).eq("domain_id", domain_id))
                query = query.eq("account_id", task["account_id"]) if task.get("account_id") is not None else query.is_("account_id", "null")
                refreshed = query.limit(1).execute().data or []
                if not refreshed:
                    provider_status, provider_error = "failed", "metrics_refresh_not_found"
                elif any(refreshed[0].get(key) != task.get(key) for key in
                         ("publish_jsonb", "published_at", "payload_jsonb", "status", "stage")):
                    return _stale_feedback_result(task)
                else:
                    write_snapshot = refreshed[0]
                    metrics_jsonb = _safe_dict(write_snapshot.get("metrics_jsonb"))
                    publish_jsonb = _safe_dict(write_snapshot.get("publish_jsonb"))
        except Exception:  # noqa: BLE001
            provider_status, provider_error = "failed", "metrics_provider_failed"

    observation = _extract_observation(metrics_jsonb)
    observed_at = _parse_iso(observation["observed_at"])
    valid_observation = bool(published_at and observed_at and published_at <= observed_at <= _now()
                             and observation["values"] and identity.get("feed_id"))
    effective_metrics = observation["values"]
    has_real_metrics = bool(effective_metrics)
    metrics_mode = "real" if has_real_metrics else "pending"
    checkpoint = (_observation_checkpoint(published_at, observed_at, plan["checkpoints_hours"], feedback_completed)
                  if valid_observation else {"checkpoint_hours": None, "missed_hours": []})
    matched = checkpoint["checkpoint_hours"]
    all_completed = sorted(set(feedback_completed + ([matched] if matched is not None else [])))
    missed = sorted((set(missed) | set(checkpoint["missed_hours"])) - set(all_completed))
    snapshots = [item for item in _safe_list(publish_jsonb.get("feedback_snapshots"))
                 if isinstance(item, dict) and item.get("checkpoint_hours") in plan["checkpoints_hours"]]
    if matched is not None and not any(item.get("checkpoint_hours") == matched for item in snapshots):
        snapshots.append({**observation, "observed_at": observed_at.isoformat(), "checkpoint_hours": matched})

    original_status = _text(task.get("status")) or "published"
    ee = {"mode": "explore" if payload.get("is_explore") is True else "unspecified", "basis": "task_configuration"}
    now_iso = _now().isoformat()
    history_task = {**task, "published_at": published_at.isoformat() if published_at else None,
                    "publish_jsonb": {**publish_jsonb, "feedback_completed_hours": all_completed}}
    history = compare_review_history(client, history_task, effective_metrics if valid_observation else {}, _extract_comparable_metrics)
    missing_fields = [name for name in METRIC_FIELDS if name not in effective_metrics]
    full_ready = bool(valid_observation and not missing_fields and effective_metrics["views"] > 0
                      and history["status"] == "compared" and not missed
                      and set(plan["checkpoints_hours"]).issubset(all_completed))
    ces = _compute_ces(effective_metrics) if full_ready else {}
    attribution = {"status": "unproven", "primary_bottleneck": "unknown", "causal_claim": False} if full_ready else {}
    evidence_refs = ([{"source_type": "pipeline_tasks.metrics_jsonb", "source_ref": observation.get("source_ref")
                       or f"https://www.xiaohongshu.com/explore/{identity['feed_id']}",
                       "task_id": task_id, "timestamp": observed_at.isoformat(), "provider": observation["provider"],
                       "account_id": account_id, "domain_id": domain_id,
                       "observation": {**observation, "observed_at": observed_at.isoformat()},
                       "provenance": observation["provenance"]}] if valid_observation else [])
    basic_review = {"status": "completed" if valid_observation else "pending", "values": effective_metrics,
                    "observation_id": observation["observation_id"],
                    "observed_at": observed_at.isoformat() if valid_observation else None,
                    "missing_fields": missing_fields, "evidence_refs": evidence_refs,
                    "rate_available": bool(valid_observation and effective_metrics.get("views", 0) > 0),
                    "causal_claim": False}
    decision_summary = {
        "what_worked": [item["action"] for item in history["keep_actions"]],
        "what_failed": [],
        "next_actions": [item["action"] for item in history["adjust_actions"]]
                        or ["继续回收同账号、同平台、同检查点的历史反馈，不据单篇数据判断因果。"],
        "discard_actions": [item["action"] for item in history["discard_actions"]],
        "confirmation_required": True,
        "auto_apply": False,
    }
    prompt_upgrade_targets = []
    remaining = [hour for hour in plan["checkpoints_hours"] if hour not in all_completed and hour not in missed]
    next_feedback_at = (
        (published_at + timedelta(hours=remaining[0])).isoformat()
        if published_at is not None and remaining
        else None
    )

    feedback_state = "pending_identity"
    stage = _text(task.get("stage")) or "feedback_pending"
    status = original_status
    if identity.get("feed_id"):
        feedback_state = "pending_metrics"
        stage = "feedback_collecting"
        if status == "publish_failed":
            status = "published"
    if published_at is None:
        feedback_state = "pending_publication_time"
        stage = "feedback_pending"
    elif valid_observation:
        feedback_state = "metrics_ready"
        stage = "feedback_ready"
        if status in {"published", "metrics_ready", "done", "reflecting", "reflection_failed"}:
            status = "metrics_ready"
    final_checkpoint = plan["checkpoints_hours"][-1] if plan["checkpoints_hours"] else 24
    is_final_due = final_checkpoint in all_completed
    if (
        valid_observation and is_final_due
        and plan["auto_retro_after_last_checkpoint"]
        and status in {"published", "metrics_ready", "done", "reflecting", "reflection_failed"}
    ):
        feedback_state = "retro_done" if full_ready else "basic_review_done"
        stage = "done"
        status = "done"

    feedback_analysis = {
        "metrics_mode": metrics_mode,
        "ces": ces,
        "attribution": attribution,
        "ee": ee,
        "feature_weights": {},
        "basic_review": basic_review,
        "full_comparison_ready": full_ready,
        "decision_summary": decision_summary,
        "prompt_upgrade_targets": prompt_upgrade_targets,
        "history_comparison": history,
    }

    updated_publish = {
        **publish_jsonb,
        "feedback_state": feedback_state,
        "feedback_schedule_hours": plan["checkpoints_hours"],
        "feedback_completed_hours": all_completed,
        "feedback_missed_hours": missed,
        "feedback_snapshots": snapshots,
        "feedback_unavailable_fields": missing_fields,
        "feedback_provider_status": provider_status,
        "feedback_provider_error": provider_error,
        "last_feedback_at": observed_at.isoformat() if valid_observation else publish_jsonb.get("last_feedback_at"),
        "next_feedback_at": next_feedback_at,
        "feedback_analysis": feedback_analysis,
        "identity": {
            **_safe_dict(publish_jsonb.get("identity")),
            **identity,
        },
    }
    changed = updated_publish != _safe_dict(task.get("publish_jsonb")) or status != original_status or stage != task.get("stage")
    if changed or (original_status != "done" and due_checkpoints):
        filters = _feedback_cas_filters(write_snapshot, domain_id)
        if filters is None:
            return _stale_feedback_result(task)
        query = client.table("pipeline_tasks").update(
            {"status": status, "stage": stage, "publish_jsonb": updated_publish, "updated_at": now_iso}
        )
        for key, value in filters:
            query = query.eq(key, value) if value is not None else query.is_(key, "null")
        if not query.execute().data:
            return _stale_feedback_result(task)

    suggestions_created = 0
    if changed and account_id and full_ready and history["adjust_actions"] and is_final_due:
        _upsert_pending_memory_item(
            client,
            domain_id=domain_id,
            account_id=account_id,
            title=f"反馈待确认：{task_id}",
            content=_pending_review_content(
                config, evidence_refs + [{**ref, "task_id": ref["source_ref"], "account_id": account_id,
                                          "domain_id": domain_id} for ref in history["evidence_refs"]],
                decision_summary["next_actions"],
                proposed=_feedback_guardrail_proposal(config),
            ),
            tags=["feedback", "history_comparison", "retro"],
        )
        suggestions_created += 1

    return _feedback_result(
        {**task, "status": status, "stage": stage, "publish_jsonb": updated_publish,
         "updated_at": now_iso if changed or (original_status != "done" and due_checkpoints) else task.get("updated_at")},
        updated_publish, changed=changed, suggestions_created=suggestions_created,
    )


def reconcile_publish_feedback(
    client: Client,
    *,
    domain_slug: str,
    account_id: str = "",
    pipeline_task_id: str = "",
    limit: int = 20,
) -> Dict[str, Any]:
    domain = _load_domain(client, domain_slug)
    accounts = _load_accounts(client)
    tasks = _load_candidate_tasks(client, domain["id"], account_id, limit, accounts, pipeline_task_id)
    recent_posts_cache: Dict[str, List[Dict[str, Any]]] = {}
    items: List[Dict[str, Any]] = []
    counters = {
        "reconciled": 0,
        "identity_recovered": 0,
        "metrics_ready": 0,
        "retro_done": 0,
        "suggestions_created": 0,
        "basic_review_done": 0,
        "provider_failures": 0,
        "task_failures": 0,
    }
    for task in tasks:
        account = accounts.get(_text(task.get("account_id")))
        try:
            result = _reconcile_task(
                client, task=task, domain_id=domain["id"], account=account,
                recent_posts_cache=recent_posts_cache,
            )
        except Exception:  # noqa: BLE001
            counters["task_failures"] += 1
            result = {**_feedback_result(task, _safe_dict(task.get("publish_jsonb")), changed=False),
                      "status": "failed", "task_status": task.get("status"), "feedback_state": "reconciliation_failed",
                      "real_metrics": False, "error": "feedback_reconciliation_failed"}
        counters["reconciled"] += 1
        if result.get("status") == "stale":
            counters["task_failures"] += 1
        if result["identity_recovered"]:
            counters["identity_recovered"] += 1
        if result["feedback_state"] == "metrics_ready":
            counters["metrics_ready"] += 1
        if result["feedback_state"] == "retro_done":
            counters["retro_done"] += 1
        if result["feedback_state"] == "basic_review_done":
            counters["basic_review_done"] += 1
        if result.get("provider_status") in {"failed", "disabled", "skipped"}:
            counters["provider_failures"] += 1
        counters["suggestions_created"] += int(result.get("suggestions_created") or 0)
        items.append(result)
    return {
        "status": "partial" if counters["provider_failures"] or counters["task_failures"] else "ok",
        "domain_slug": domain_slug,
        "account_id": account_id or None,
        "pipeline_task_id": pipeline_task_id or None,
        "summary": counters,
        "items": items,
    }
