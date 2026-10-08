import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from supabase import Client

from app.model_router import get_router
from app.content_branches import (
    PROJECT_OBSERVER, BRANCH_NAME, BRANCH_VERSION, OBSERVER_RULES,
    resolve_content_branch, observer_strategy, observer_contract_checks, project_source_ready, observer_fallback,
)
from app.runtime.accounts.account_prompt_versions import load_active_prompt_versions
from app.runtime.accounts.account_runtime import account_execution_scope, upsert_account_runtime
from app.runtime.accounts.kb_libraries import derive_runtime_guidance, load_kb_libraries
from app.runtime.accounts.account_strategy import merge_keywords, strategy_from_account
from app.tools.integrations.xhs_mcp_readonly import sync_pipeline_task_metrics_via_mcp
from app.tools.publishing.publisher import extract_publish_identity, publish_with_fallback

DRAFTABLE_STATUSES = {"queued", "intel_ready", "drafting", "review_rejected"}
_MOJIBAKE_MARKERS = (
    "锟",
    "鈥",
    "銆",
    "鍙",
    "鏃",
    "鏈",
    "鐨",
    "鍐",
    "闂",
    "瑙",
    "绗",
    "鍥",
    "鍚",
    "浼",
    "缁",
    "锛",
    "锚",
)
_CTA_TOKENS = (
    "评论区",
    "欢迎留言",
    "私信",
    "收藏",
    "关注",
    "你会怎么选",
    "你最关心",
    "需要的话我再写",
)


def _normalize_space(text: str) -> str:
    value = str(text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _mojibake_score(text: str) -> float:
    raw = str(text or "")
    if not raw:
        return 0.0
    marker_hits = sum(raw.count(token) for token in _MOJIBAKE_MARKERS)
    replacement_hits = raw.count("\ufffd")
    return (marker_hits + replacement_hits * 0.5) / max(1, len(raw))


def _is_text_usable(text: str) -> bool:
    raw = _normalize_space(text)
    if not raw:
        return False
    if len(raw) < 6:
        return False
    return _mojibake_score(raw) < 0.06


def _extract_hashtags(text: str) -> List[str]:
    raw = str(text or "")
    tags = re.findall(r"#([^\s#]{1,24})", raw)
    deduped: List[str] = []
    for tag in tags:
        token = str(tag).strip()
        if not token:
            continue
        if token not in deduped:
            deduped.append(token)
    return deduped



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _merge_json(*parts: Dict[str, Any] | None) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for part in parts:
        if isinstance(part, dict):
            merged.update(part)
    return merged


def _merge_quality_gate(base: Dict[str, Any] | None, override: Dict[str, Any] | None) -> Dict[str, Any]:
    base_map = base if isinstance(base, dict) else {}
    override_map = override if isinstance(override, dict) else {}
    merged = dict(base_map)
    for key, value in override_map.items():
        if value is None:
            continue
        merged[key] = value
    return merged


def _get_pipeline_task(client: Client, pipeline_task_id: str) -> Optional[Dict[str, Any]]:
    res = client.table("pipeline_tasks").select("*").eq("id", pipeline_task_id).limit(1).execute()
    if not res.data:
        return None
    return res.data[0]


def _get_domain(client: Client, domain_id: str) -> Optional[Dict[str, Any]]:
    res = client.table("domains").select("*").eq("id", domain_id).limit(1).execute()
    if not res.data:
        return None
    return res.data[0]


def _get_channel_account_runtime(client: Client, account_id: str) -> Dict[str, Any] | None:
    if not account_id:
        return None
    try:
        res = (
            client.table("channel_accounts")
            .select(
                "id,account_name,account_handle,login_mode,publish_selector,storage_state_path,"
                "user_data_dir,cookies_json,login_username,login_password,config_jsonb"
            )
            .eq("id", account_id)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    if not res.data:
        return None
    return res.data[0]


def _get_single_active_channel_account_runtime(client: Client, channel: str) -> Dict[str, Any] | None:
    try:
        rows = (
            client.table("channel_accounts")
            .select(
                "id,account_name,account_handle,login_mode,publish_selector,storage_state_path,"
                "user_data_dir,cookies_json,login_username,login_password,config_jsonb"
            )
            .eq("channel", channel)
            .eq("is_active", True)
            .order("updated_at", desc=True)
            .limit(2)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return None
    if len(rows) == 1:
        return rows[0]
    return None


def _fetch_recent_intel(client: Client, domain_id: str, *, account_id: str = "") -> List[Dict[str, Any]]:
    base_query = (
        client.table("intelligence_items")
        .select("raw_text,source_type,source_url,captured_at")
        .eq("domain_id", domain_id)
        .order("captured_at", desc=True)
        .limit(8)
    )
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        res = base_query.execute()
        rows = res.data or []
        cleaned = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = _normalize_space(str(row.get("raw_text") or ""))
            if text and _is_text_usable(text):
                cleaned.append({**row, "raw_text": text[:2000]})
        return cleaned or rows[:3]
    try:
        scoped_query = base_query.or_(f"account_id.eq.{normalized_account_id},account_id.is.null")
        res = scoped_query.execute()
        rows = res.data or []
        cleaned = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = _normalize_space(str(row.get("raw_text") or ""))
            if text and _is_text_usable(text):
                cleaned.append({**row, "raw_text": text[:2000]})
        return cleaned or rows[:3]
    except Exception:  # noqa: BLE001
        res = base_query.execute()
        rows = res.data or []
        cleaned = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = _normalize_space(str(row.get("raw_text") or ""))
            if text and _is_text_usable(text):
                cleaned.append({**row, "raw_text": text[:2000]})
        return cleaned or rows[:3]


def _load_memory_items_for_task(
    client: Client,
    *,
    domain_id: str,
    account_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    try:
        rows = (
            client.table("memory_items")
            .select("type,title,content,tags,status,account_id,updated_at")
            .eq("domain_id", domain_id)
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(max(1, min(limit, 60)))
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return []
    prioritized: List[Dict[str, Any]] = []
    fallback: List[Dict[str, Any]] = []
    for row in rows:
        row_account_id = str(row.get("account_id") or "").strip()
        if row_account_id and row_account_id == account_id:
            prioritized.append(row)
        elif not row_account_id:
            fallback.append(row)
    return (prioritized + fallback)[:limit]


def _latest_optimization_hint(client: Client, domain_id: str) -> Dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = (
        client.table("pipeline_tasks")
        .select("payload_jsonb,metrics_jsonb,published_at")
        .eq("domain_id", domain_id)
        .in_("status", ["published", "done"])
        .gte("published_at", since)
        .order("published_at", desc=True)
        .limit(40)
        .execute()
    ).data or []
    if not rows:
        return {}
    ranked: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        metrics = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
        title = _normalize_space(str(payload.get("title") or ""))
        body = _normalize_space(str(payload.get("body") or ""))
        if not title and not body:
            continue
        score = (
            _safe_int(metrics.get("likes"))
            + _safe_int(metrics.get("collects"))
            + _safe_int(metrics.get("comments_count"))
            + _safe_int(metrics.get("shares"))
        )
        ranked.append({"title": title, "body": body, "score": score})
    if not ranked:
        return {}
    ranked.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    top = ranked[0]
    return {
        "best_title": str(top.get("title") or "")[:300],
        "best_hint": str(top.get("body") or "")[:400],
    }


def _domain_focus_keywords(domain: Dict[str, Any]) -> List[str]:
    cfg = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
    direct = cfg.get("focus_keywords")
    if isinstance(direct, list):
        values = [str(item or "").strip() for item in direct if str(item or "").strip()]
        if values:
            return values[:12]
    crawler_template = cfg.get("crawler_template") if isinstance(cfg.get("crawler_template"), dict) else {}
    groups = crawler_template.get("keyword_groups") if isinstance(crawler_template.get("keyword_groups"), dict) else {}
    template_focus = groups.get("focus_keywords")
    if isinstance(template_focus, list):
        values = [str(item or "").strip() for item in template_focus if str(item or "").strip()]
        if values:
            return values[:12]
    slug = str(domain.get("slug") or "").strip().lower()
    if "japan" in slug or "日本" in slug:
        return ["日本移民", "日本经营管理签证", "日本留学", "日本永住", "签证避坑"]
    return []


def _build_persona_guardrail(
    domain: Dict[str, Any],
    intent: Dict[str, Any],
    channel: str,
    *,
    account_strategy: Dict[str, Any] | None = None,
    memory_items: List[Dict[str, Any]] | None = None,
) -> str:
    cfg = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
    strategy = account_strategy if isinstance(account_strategy, dict) else {}
    audience = strategy.get("audience") if isinstance(strategy.get("audience"), list) else []
    pain_points = strategy.get("pain_points") if isinstance(strategy.get("pain_points"), list) else []
    brand_tone = str(strategy.get("tone_style") or "").strip()
    forbidden_claims = strategy.get("forbidden_claims") if isinstance(strategy.get("forbidden_claims"), list) else []
    sub_niches = cfg.get("sub_niches") if isinstance(cfg.get("sub_niches"), list) else []
    focus_keywords = merge_keywords(
        strategy.get("focus_keywords") if isinstance(strategy.get("focus_keywords"), list) else [],
        _domain_focus_keywords(domain),
        max_items=12,
    )
    topic = str(intent.get("topic") or "").strip()
    persona_name = str(strategy.get("persona_name") or "").strip()
    ip_positioning = str(strategy.get("ip_positioning") or "").strip()
    cta_style = str(strategy.get("cta_style") or "").strip()
    if not audience:
        audience = cfg.get("audience") if isinstance(cfg.get("audience"), list) else []
    if not audience:
        audience = ["关注日本移民的家庭", "日本留学与身份规划人群"]
    if not pain_points:
        pain_points = cfg.get("pain_points") if isinstance(cfg.get("pain_points"), list) else []
    if not pain_points:
        pain_points = ["条件边界不清晰", "预算与时间线不确定", "申请流程容易踩坑"]
    if not brand_tone:
        brand_tone = str(cfg.get("brand_tone") or "").strip()
    if not brand_tone:
        brand_tone = "专业、直接、先讲边界再给方案，不夸大承诺"
    if not sub_niches:
        sub_niches = ["经营管理签证", "留学转身份", "永住路径"]
    if not forbidden_claims:
        forbidden_claims = cfg.get("forbidden_claims") if isinstance(cfg.get("forbidden_claims"), list) else []

    lines: List[str] = []
    lines.append("你必须保持账号既定人设，不要写成泛内容。")
    if persona_name:
        lines.append(f"账号人设：{persona_name}")
    if ip_positioning:
        lines.append(f"定位说明：{ip_positioning}")
    if brand_tone:
        lines.append(f"语气要求：{brand_tone}")
    if audience:
        lines.append(f"目标受众：{' / '.join(str(x) for x in audience[:4] if str(x).strip())}")
    if pain_points:
        lines.append(f"优先痛点：{' / '.join(str(x) for x in pain_points[:4] if str(x).strip())}")
    if sub_niches:
        lines.append(f"细分赛道：{' / '.join(str(x) for x in sub_niches[:4] if str(x).strip())}")
    if focus_keywords:
        lines.append(f"关键词约束：至少覆盖其中2个 -> {' / '.join(focus_keywords[:8])}")
    if forbidden_claims:
        lines.append(f"禁用词与禁用承诺：{' / '.join(str(x) for x in forbidden_claims[:8] if str(x).strip())}")
    if topic:
        lines.append(f"本轮主题：{topic}")
    if cta_style:
        lines.append(f"互动与行动引导：{cta_style}")
    if channel == "xiaohongshu":
        lines.append("平台约束：小红书图文风格，开头3行给结论或悬念，段落短，结尾有互动引导。")
    memory_rows = memory_items if isinstance(memory_items, list) else []
    if memory_rows:
        memory_lines = []
        for item in memory_rows[:6]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if not title or not content:
                continue
            memory_lines.append(f"{title}: {content[:120]}")
        if memory_lines:
            lines.append("历史记忆（必须尽量对齐）：")
            lines.extend([f"- {row}" for row in memory_lines])
    return "\n".join(lines)


def _pipeline_audit(client: Client, action: str, target_id: str, diff: Dict[str, Any]) -> None:
    client.table("audit_logs").insert(
        {
            "actor": "system:pipeline",
            "action": action,
            "target_type": "pipeline_task",
            "target_id": target_id,
            "diff_jsonb": diff,
        }
    ).execute()


def _record_reflection_insight(
    client: Client,
    pipeline_task_id: str,
    domain_id: str,
    reflection_result: Dict[str, Any],
) -> None:
    status_value = reflection_result.get("status", "unknown")
    applied = status_value == "ok"
    confidence = 0.85 if applied else 0.35
    try:
        client.table("evolution_insights").insert(
            {
                "target_type": "domain_strategy",
                "target_id": domain_id,
                "domain_id": domain_id,
                "pipeline_task_id": pipeline_task_id,
                "insight_jsonb": {"reflection_result": reflection_result},
                "action_jsonb": {"auto_applied": applied},
                "confidence": confidence,
                "applied": applied,
                "applied_at": _now_iso() if applied else None,
                "created_by": "system:reflector",
            }
        ).execute()
    except Exception:  # noqa: BLE001
        # Keep pipeline execution non-blocking even if insight table is not yet migrated.
        return


def _update_pipeline_task(client: Client, pipeline_task_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    next_fields = {**fields, "updated_at": _now_iso()}
    updated = client.table("pipeline_tasks").update(next_fields).eq("id", pipeline_task_id).execute()
    if updated.data:
        return updated.data[0]
    latest = _get_pipeline_task(client, pipeline_task_id)
    return latest or {"id": pipeline_task_id}


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except Exception:  # noqa: BLE001
        return 0


def _load_items_from_local_payload(file_path: Path, limit: int = 80) -> List[Dict[str, Any]]:
    if not file_path.exists():
        return []
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return []
    rows = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    usable: List[Dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        title = _normalize_space(str(row.get("title") or ""))
        content = _normalize_space(str(row.get("content") or ""))
        if not _is_text_usable(title) or not _is_text_usable(content):
            continue
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        weight = _safe_int(metrics.get("likes")) + _safe_int(metrics.get("collects")) + _safe_int(metrics.get("comments_count"))
        usable.append(
            {
                "title": title[:60],
                "content": content[:260],
                "weight": weight,
                "source": str(file_path.name),
                "tags": row.get("tags") if isinstance(row.get("tags"), list) else [],
            }
        )
    return usable


def _load_local_style_samples(limit: int = 6) -> List[Dict[str, Any]]:
    root = Path.cwd()
    paths = [
        root / "data" / "octopus" / "payload_case_built.json",
        root / "data" / "octopus" / "payload_case.json",
    ]
    rows: List[Dict[str, Any]] = []
    for file_path in paths:
        rows.extend(_load_items_from_local_payload(file_path, limit=120))
    rows.sort(key=lambda item: int(item.get("weight") or 0), reverse=True)
    deduped: List[Dict[str, Any]] = []
    seen_titles: set[str] = set()
    for row in rows:
        title = str(row.get("title") or "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def _fetch_db_case_style_samples(client: Client, domain_id: str, *, account_id: str = "", limit: int = 30) -> List[Dict[str, Any]]:
    try:
        query = (
            client.table("cases")
            .select("title,content,metrics,updated_at,account_id")
            .eq("domain_id", domain_id)
            .order("updated_at", desc=True)
            .limit(max(1, min(limit, 80)))
        )
        normalized_account_id = str(account_id or "").strip()
        if normalized_account_id:
            query = query.or_(f"account_id.eq.{normalized_account_id},account_id.is.null")
        rows = query.execute().data or []
    except Exception:  # noqa: BLE001
        return []
    samples: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _normalize_space(str(row.get("title") or ""))
        content = _normalize_space(str(row.get("content") or ""))
        if not _is_text_usable(title) or not _is_text_usable(content):
            continue
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        weight = _safe_int(metrics.get("likes")) + _safe_int(metrics.get("collects")) + _safe_int(metrics.get("comments_count"))
        samples.append(
            {
                "title": title[:60],
                "content": content[:260],
                "weight": weight,
                "source": "cases",
                "tags": [],
            }
        )
    samples.sort(key=lambda item: int(item.get("weight") or 0), reverse=True)
    return samples[:limit]


def _collect_style_samples(client: Client, *, domain_id: str, account_id: str, domain_slug: str = "", limit: int = 6) -> List[Dict[str, Any]]:
    merged = _fetch_db_case_style_samples(client, domain_id, account_id=account_id, limit=30)
    if domain_slug == "japan_immigration":
        merged += _load_local_style_samples(limit=12)
    if not merged and domain_slug != "japan_immigration":
        return []
    if not merged:
        return [
            {"title": "别急着做日本移民，先看这3个边界", "content": "先说结论：不是谁都适合同一条路径。先看条件，再谈方案。", "weight": 1, "source": "default"},
            {"title": "日本签证避坑：这4个细节最容易踩雷", "content": "把风险讲清楚，再给执行步骤，最后引导评论区提问。", "weight": 1, "source": "default"},
        ]
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in merged:
        title = str(item.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _ensure_hashtag_tail(body: str, keywords: List[str], *, target_count: int = 4) -> str:
    normalized = _normalize_space(body)
    tags = _extract_hashtags(normalized)
    if len(tags) >= target_count:
        return normalized
    for kw in keywords:
        token = str(kw or "").strip().strip("#")
        if not token:
            continue
        if token not in tags:
            tags.append(token)
        if len(tags) >= target_count:
            break
    if not tags:
        return normalized
    tag_line = " ".join(f"#{tag}" for tag in tags[:max(target_count, 3)])
    if tag_line in normalized:
        return normalized
    return f"{normalized}\n\n{tag_line}"


def _evaluate_xhs_quality(
    *,
    title: str,
    body: str,
    focus_keywords: List[str],
    forbidden_claims: List[str],
    quality_gate_config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    cfg = quality_gate_config if isinstance(quality_gate_config, dict) else {}
    observer = cfg.get("content_branch") == PROJECT_OBSERVER
    if observer:
        cfg = {**cfg, "title_min": 6, "title_max": 30, "body_min": 80, "body_max": 420,
               "paragraph_min": 3}
    title_min = max(6, min(30, _safe_int(cfg.get("title_min") or 10)))
    title_max = max(title_min, min(40, _safe_int(cfg.get("title_max") or 28)))
    body_min = max(60, min(800, _safe_int(cfg.get("body_min") or 140)))
    body_max = max(body_min, min(3000, _safe_int(cfg.get("body_max") or 1200)))
    paragraph_min = max(2, min(10, _safe_int(cfg.get("paragraph_min") or 4)))
    min_keyword_hits = max(1, min(5, _safe_int(cfg.get("min_keyword_hits") or 2)))
    hashtag_target = max(1, min(8, _safe_int(cfg.get("hashtag_target") or 3)))
    pass_score = max(50, min(95, _safe_int(cfg.get("pass_score") or 72)))
    emphasize_checks = [
        str(item or "").strip()
        for item in (cfg.get("emphasize_checks") if isinstance(cfg.get("emphasize_checks"), list) else [])
        if str(item or "").strip()
    ]
    weights: Dict[str, int] = {
        "title_length": 12,
        "body_length": 16,
        "paragraph_count": 14,
        "keyword_coverage": 18,
        "hashtag_count": 12,
        "cta_presence": 12,
        "forbidden_claims": 10,
        "encoding_clean": 6,
    }
    for key in emphasize_checks:
        if key in weights:
            weights[key] = min(28, weights[key] + 4)

    raw_title = _normalize_space(title)
    raw_body = _normalize_space(body)
    checks: List[Dict[str, Any]] = []

    title_len = len(raw_title)
    body_len = len(raw_body)
    lines = [line for line in raw_body.split("\n") if line.strip()]
    keyword_hits = [kw for kw in focus_keywords if str(kw or "").strip() and str(kw) in (raw_title + raw_body)]
    hashtags = _extract_hashtags(raw_title + "\n" + raw_body)
    cta_hit = any(token in raw_body for token in _CTA_TOKENS)
    forbidden_hits = [token for token in forbidden_claims if str(token or "").strip() and str(token) in (raw_title + raw_body)]
    mojibake = max(_mojibake_score(raw_title), _mojibake_score(raw_body))

    checks.append({"name": "title_length", "passed": title_min <= title_len <= title_max, "weight": weights["title_length"], "detail": title_len})
    checks.append({"name": "body_length", "passed": body_min <= body_len <= body_max, "weight": weights["body_length"], "detail": body_len})
    checks.append({"name": "paragraph_count", "passed": len(lines) >= paragraph_min, "weight": weights["paragraph_count"], "detail": len(lines)})
    checks.append({"name": "keyword_coverage", "passed": len(keyword_hits) >= min_keyword_hits, "weight": weights["keyword_coverage"], "detail": keyword_hits[:6]})
    checks.append({"name": "hashtag_count", "passed": len(hashtags) >= hashtag_target, "weight": weights["hashtag_count"], "detail": hashtags[:8]})
    checks.append({"name": "cta_presence", "passed": cta_hit, "weight": weights["cta_presence"], "detail": cta_hit})
    checks.append({"name": "forbidden_claims", "passed": not forbidden_hits, "weight": weights["forbidden_claims"], "detail": forbidden_hits[:5]})
    checks.append({"name": "encoding_clean", "passed": mojibake < 0.045, "weight": weights["encoding_clean"], "detail": round(mojibake, 4)})

    if observer:
        for check in checks:
            if check["name"] in {"keyword_coverage", "hashtag_count", "cta_presence"}:
                check.update({"passed": True, "weight": 0, "detail": "本分支不要求关键词堆叠、标签或互动引导"})
        checks.extend(observer_contract_checks(raw_body, cfg.get("project_reference"), raw_title))

    total_weight = sum(int(item["weight"]) for item in checks)
    score = 0
    failed: List[str] = []
    for item in checks:
        if bool(item["passed"]):
            score += int(item["weight"])
        else:
            failed.append(str(item["name"]))
    passed = score >= pass_score and not forbidden_hits and mojibake < 0.06
    if observer:
        passed = all(check["passed"] for check in checks if check["weight"])
    return {
        "score": round((score / max(1, total_weight)) * 100, 2),
        "passed": passed,
        "failed_checks": failed,
        "checks": checks,
        "keyword_hits": keyword_hits[:8],
        "hashtags": hashtags[:10],
        "forbidden_hits": forbidden_hits[:8],
        "mojibake_score": round(mojibake, 4),
        "config": {
            "pass_score": pass_score,
            "title_min": title_min,
            "title_max": title_max,
            "body_min": body_min,
            "body_max": body_max,
            "paragraph_min": paragraph_min,
            "min_keyword_hits": min_keyword_hits,
            "hashtag_target": hashtag_target,
            "emphasize_checks": emphasize_checks[:8],
        },
    }


def _heuristic_polish_draft(
    *,
    title: str,
    body: str,
    focus_keywords: List[str],
    style_samples: List[Dict[str, Any]],
    content_branch: str = "tutorial",
) -> Dict[str, str]:
    next_title = _normalize_space(title)
    next_body = _normalize_space(body)
    if content_branch == PROJECT_OBSERVER:
        return {"title": next_title, "body": next_body}
    if not next_title:
        fallback_title = str((style_samples[0] or {}).get("title") or "").strip() if style_samples else ""
        next_title = fallback_title or "日本移民避坑：先看这 3 个关键点"
    if len(next_title) > 30:
        next_title = next_title[:30].rstrip("，。；：!?！？")
    if len(next_title) < 10:
        next_title = f"避坑提醒｜{next_title}" if next_title else "日本移民避坑：先看这 3 个关键点"

    if not next_body:
        next_body = "先说结论：先看边界再做方案，别急着套模板。"
    if "先说结论" not in next_body[:40]:
        next_body = f"先说结论：这件事能做，但要先确认条件和时间线。\n\n{next_body}"
    lines = [line for line in next_body.split("\n") if line.strip()]
    if len(lines) < 4:
        brief = str((style_samples[0] or {}).get("content") or "").strip() if style_samples else ""
        next_body = (
            f"先说结论：{next_title}不是只看结果，先把边界看清。\n\n"
            "为什么现在要关注：最近政策与审核口径变化更快，盲目跟风风险高。\n\n"
            f"怎么做更稳：\n1) 先确认自身条件；\n2) 再选路径；\n3) 每一步留痕复核。{brief[:80]}\n\n"
            "你最关心哪一步？评论区告诉我，我按你的情况给你可执行清单。"
        )
    if not any(token in next_body for token in _CTA_TOKENS):
        next_body = f"{next_body}\n\n你最关心哪一步？欢迎在评论区告诉我。"

    next_body = _ensure_hashtag_tail(next_body, focus_keywords, target_count=4)
    return {"title": next_title, "body": next_body}


def _extract_focus_terms(focus_keywords: List[str], *, limit: int = 12) -> List[str]:
    terms: List[str] = []
    for raw in focus_keywords:
        token = str(raw or "").strip().strip("#")
        if not token:
            continue
        if token in terms:
            continue
        terms.append(token)
        if len(terms) >= limit:
            break
    return terms


def _title_logic_tags(title: str) -> Dict[str, Any]:
    raw = _normalize_space(title)
    tags: List[str] = []
    reasons: List[str] = []
    has_digit = bool(re.search(r"\d", raw))
    has_question = ("？" in raw) or ("?" in raw)
    has_risk = any(token in raw for token in ("避坑", "别踩", "后悔", "风险", "警惕", "暴涨"))
    has_action = any(token in raw for token in ("怎么", "如何", "先做", "步骤", "清单", "攻略"))
    if has_risk:
        tags.append("risk_hook")
        reasons.append("用风险词提升停留和点击")
    if has_digit:
        tags.append("numbered")
        reasons.append("数字结构增强信息密度感")
    if has_action:
        tags.append("actionable")
        reasons.append("动作导向便于用户立刻执行")
    if has_question:
        tags.append("question_hook")
        reasons.append("问句可增强互动预期")
    if not tags:
        tags.append("plain_statement")
        reasons.append("陈述型标题，强调清晰和稳健")
    return {"title": raw, "tags": tags, "reasons": reasons}


def _split_body_sections(body: str) -> List[str]:
    raw = _normalize_space(body)
    if not raw:
        return []
    sections = [part.strip() for part in raw.split("\n\n") if part.strip()]
    return sections[:8]


def _reference_overlap_score(section: str, ref_text: str, focus_terms: List[str]) -> int:
    if not section or not ref_text:
        return 0
    score = 0
    for term in focus_terms:
        if term and term in section and term in ref_text:
            score += 2
    shared_chars = set(ch for ch in section if "\u4e00" <= ch <= "\u9fff")
    ref_chars = set(ch for ch in ref_text if "\u4e00" <= ch <= "\u9fff")
    score += min(3, len(shared_chars.intersection(ref_chars)) // 6)
    return score


def _build_evidence_map(
    *,
    title: str,
    body: str,
    reference_items: List[Dict[str, Any]],
    focus_keywords: List[str],
    account_strategy: Dict[str, Any],
) -> Dict[str, Any]:
    refs: List[Dict[str, Any]] = []
    for idx, item in enumerate(reference_items[:8], start=1):
        if not isinstance(item, dict):
            continue
        refs.append(
            {
                "ref_id": f"ref_{idx}",
                "source_type": str(item.get("source_type") or ""),
                "captured_at": item.get("captured_at"),
                "source_url": str(item.get("source_url") or ""),
                "raw_text": str(item.get("raw_text") or "")[:220],
            }
        )

    focus_terms = _extract_focus_terms(focus_keywords)
    sections = _split_body_sections(body)
    section_rows: List[Dict[str, Any]] = []
    for idx, section in enumerate(sections, start=1):
        scored_refs: List[tuple[int, Dict[str, Any]]] = []
        for ref in refs:
            score = _reference_overlap_score(section, str(ref.get("raw_text") or ""), focus_terms)
            scored_refs.append((score, ref))
        scored_refs.sort(key=lambda pair: pair[0], reverse=True)
        chosen = [ref.get("ref_id") for score, ref in scored_refs if score > 0][:2]
        if not chosen and refs:
            chosen = [refs[0].get("ref_id")]
        section_rows.append(
            {
                "section_id": f"p{idx}",
                "preview": section[:120],
                "evidence_refs": chosen,
                "why_this_section": "先给结论再给动作，降低理解成本" if idx == 1 else "承接上文给执行细节并增强可信度",
            }
        )

    persona = str(account_strategy.get("persona_name") or "").strip()
    positioning = str(account_strategy.get("ip_positioning") or "").strip()
    goal = str(account_strategy.get("primary_goal") or "").strip()
    return {
        "version": "v1",
        "title_logic": _title_logic_tags(title),
        "body_framework": {
            "template": "结论 -> 解释 -> 步骤 -> 收口CTA",
            "reasons": [
                "优先保证小红书阅读节奏",
                "每段有明确功能，便于审核和复盘",
            ],
        },
        "account_constraints": {
            "persona_name": persona,
            "ip_positioning": positioning,
            "primary_goal": goal,
            "focus_keywords": focus_terms,
        },
        "sections": section_rows,
        "references": refs,
    }


def _draft_image_prompt(domain: Dict[str, Any], title: str, body: str) -> str:
    return (f"信息型内容封面，赛道：{domain.get('name') or domain.get('slug') or '内容分享'}。标题：{title}。"
            f"主题摘要：{body[:80]}。信息层级清晰，与账号定位一致，不添加未经证实的效果承诺。")


def _draft_manual_pipeline_task(client: Client, pipeline_task: Dict[str, Any], domain: Dict[str, Any]) -> Dict[str, Any]:
    pipeline_task_id = pipeline_task["id"]
    _update_pipeline_task(client, pipeline_task_id, {"status": "drafting", "stage": "copywriting"})

    intent = pipeline_task.get("intent_jsonb") if isinstance(pipeline_task.get("intent_jsonb"), dict) else {}
    payload = pipeline_task.get("payload_jsonb") if isinstance(pipeline_task.get("payload_jsonb"), dict) else {}

    title = str(payload.get("title") or intent.get("topic") or "").strip() or "未命名草稿"
    body = str(payload.get("body") or "").strip()
    if not body:
        body = f"【手动草稿】请在审核前补全正文细节。\n\n主题：{title}"

    hashtags = payload.get("hashtags")
    if not isinstance(hashtags, list):
        hashtags = []
    tags = [str(x).strip() for x in hashtags if str(x).strip()][:12]

    image_prompt = _draft_image_prompt(domain, title, body)
    evidence_map = _build_evidence_map(
        title=title,
        body=body,
        reference_items=[],
        focus_keywords=tags,
        account_strategy={},
    )
    analysis_jsonb = _merge_json(
        payload.get("analysis_jsonb") if isinstance(payload.get("analysis_jsonb"), dict) else {},
        {
            "logic": [
                "用户手动输入草稿",
                "跳过采集/分析/重构节点",
                "直接进入人工审核",
            ],
            "evidence_map": evidence_map,
            "manual_bypass": True,
            "bypass_reason": str(payload.get("bypass_reason") or "manual_input"),
        },
    )

    next_payload = _merge_json(
        payload,
        {
            "title": title,
            "body": body,
            "hashtags": tags,
            "image_prompt": image_prompt,
            "analysis_jsonb": analysis_jsonb,
            "manual_input": True,
        },
    )

    reviewed = _update_pipeline_task(
        client,
        pipeline_task_id,
        {
            "status": "pending_review",
            "stage": "human_review",
            "payload_jsonb": next_payload,
            "review_jsonb": _merge_json(
                pipeline_task.get("review_jsonb"),
                {
                    "review_required": True,
                    "review_updated_at": _now_iso(),
                    "manual_bypass": True,
                },
            ),
        },
    )

    _pipeline_audit(
        client,
        action="pipeline.task_drafted_manual",
        target_id=pipeline_task_id,
        diff={
            "to_status": "pending_review",
            "channel": pipeline_task["channel"],
            "manual_bypass": True,
        },
    )
    return {
        "status": "ok",
        "mode": "draft_manual",
        "pipeline_task_id": pipeline_task_id,
        "task": reviewed,
    }


def _draft_pipeline_task(client: Client, pipeline_task: Dict[str, Any], domain: Dict[str, Any]) -> Dict[str, Any]:
    pipeline_task_id = pipeline_task["id"]
    _update_pipeline_task(client, pipeline_task_id, {"status": "drafting", "stage": "copywriting"})

    strategy = (domain.get("config_jsonb") or {}).get("prompt_templates") or {}
    intent = pipeline_task.get("intent_jsonb") or {}
    strategy_for_task = dict(strategy)
    payload = pipeline_task.get("payload_jsonb") if isinstance(pipeline_task.get("payload_jsonb"), dict) else {}
    channel_account_id = str(payload.get("channel_account_id") or "").strip()
    account_runtime = _get_channel_account_runtime(client, channel_account_id) if channel_account_id else None
    account_strategy = strategy_from_account(account_runtime)
    content_branch = resolve_content_branch({**intent, "content_branch": intent.get("content_branch", "tutorial")}, account_strategy)
    project_reference = intent.get("project_reference") if isinstance(intent.get("project_reference"), dict) else {}
    if content_branch == PROJECT_OBSERVER and domain.get("slug") != "ai_content":
        raise ValueError("AI 项目观察分支仅适用于 AI 内容赛道")
    if content_branch == PROJECT_OBSERVER and not project_source_ready(project_reference):
        _update_pipeline_task(client, pipeline_task_id, {"status": "review_rejected", "stage": "intake",
            "review_jsonb": _merge_json(pipeline_task.get("review_jsonb"), {
                "review_required": True, "blocked_reason": "project_reference_required"})})
        return {"status": "blocked", "reason": "project_reference_required", "pipeline_task_id": pipeline_task_id}
    if channel_account_id and account_runtime:
        upsert_account_runtime(
            channel_account_id,
            {
                "channel": pipeline_task.get("channel"),
                "login_mode": account_runtime.get("login_mode"),
                "storage_state_path": account_runtime.get("storage_state_path"),
                "user_data_dir": account_runtime.get("user_data_dir"),
                "cookies_json": account_runtime.get("cookies_json"),
            },
        )
    account_id = str(account_strategy.get("account_id") or channel_account_id or "").strip()
    memory_items = _load_memory_items_for_task(client, domain_id=str(domain["id"]), account_id=account_id, limit=20)
    cfg = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
    strategy_for_task["brand_tone"] = (
        str(account_strategy.get("tone_style") or "").strip()
        or str(cfg.get("brand_tone") or "").strip()
        or "专业、直接、反空话，强调可执行与合规边界"
    )
    strategy_for_task["audience"] = (
        account_strategy.get("audience")
        if isinstance(account_strategy.get("audience"), list) and account_strategy.get("audience")
        else cfg.get("audience")
        if isinstance(cfg.get("audience"), list) and cfg.get("audience")
        else ["关注日本移民的家庭", "日本留学与身份规划人群"]
    )
    strategy_for_task["pain_points"] = (
        account_strategy.get("pain_points")
        if isinstance(account_strategy.get("pain_points"), list) and account_strategy.get("pain_points")
        else cfg.get("pain_points")
        if isinstance(cfg.get("pain_points"), list) and cfg.get("pain_points")
        else ["条件边界不清晰", "预算与时间线不确定", "申请流程容易踩坑"]
    )
    strategy_for_task["hooks"] = (
        cfg.get("hooks")
        if isinstance(cfg.get("hooks"), list) and cfg.get("hooks")
        else ["别被信息差带偏", "先看边界再做决定", "避坑清单先收好"]
    )
    strategy_for_task["forbidden_claims"] = (
        account_strategy.get("forbidden_claims")
        if isinstance(account_strategy.get("forbidden_claims"), list) and account_strategy.get("forbidden_claims")
        else cfg.get("forbidden_claims")
        if isinstance(cfg.get("forbidden_claims"), list)
        else []
    )
    strategy_for_task["focus_keywords"] = merge_keywords(
        account_strategy.get("focus_keywords") if isinstance(account_strategy.get("focus_keywords"), list) else [],
        _domain_focus_keywords(domain),
        max_items=16,
    )
    strategy_for_task["persona_guardrail"] = _build_persona_guardrail(
        domain=domain,
        intent=intent if isinstance(intent, dict) else {},
        channel=str(pipeline_task.get("channel") or "xiaohongshu"),
        account_strategy=account_strategy,
        memory_items=memory_items,
    )
    strategy_for_task["memory_items"] = memory_items
    account_prompt_overrides = (
        account_strategy.get("prompt_overrides")
        if isinstance(account_strategy.get("prompt_overrides"), dict)
        else {}
    )
    active_prompt_versions = load_active_prompt_versions(
        client,
        domain_slug=str(domain.get("slug") or "japan_immigration"),
        account_id=account_id,
    )
    active_draft_prompt = str(
        (active_prompt_versions.get("draft_writer") or {}).get("system_prompt") or ""
    ).strip()
    if active_draft_prompt:
        existing_draft_prompt = str(strategy_for_task.get("draft") or "").strip()
        strategy_for_task["draft"] = (
            f"{active_draft_prompt}\n{existing_draft_prompt}".strip()
            if existing_draft_prompt
            else active_draft_prompt
        )
    draft_appendix = str(account_prompt_overrides.get("draft_appendix") or "").strip()
    if draft_appendix:
        existing_draft_prompt = str(strategy_for_task.get("draft") or "").strip()
        strategy_for_task["draft"] = f"{draft_appendix}\n{existing_draft_prompt}".strip()
    intel_items = _fetch_recent_intel(client, domain["id"], account_id=account_id)
    source_opinion = str(intent.get("source_opinion") or "").strip()
    if source_opinion:
        intel_items = [
            {
                "raw_text": source_opinion,
                "source_type": "viewpoint_source",
                "source_url": "",
                "captured_at": _now_iso(),
            },
            *intel_items,
        ]
        existing_draft_prompt = str(strategy_for_task.get("draft") or "").strip()
        strategy_for_task["draft"] = (
            "必须先对输入观点做客观拆解（成立部分、缺失前提、风险边界），"
            "再给出可执行方案与行动引导。"
            + (f"\n{existing_draft_prompt}" if existing_draft_prompt else "")
        )
    governance_libs = load_kb_libraries(
        client,
        domain_slug=str(domain.get("slug") or "japan_immigration"),
        account_id=account_id,
        applies_to="copy",
        stage="copy",
        direction="egress",
        entity_type="asset",
        target_agent="copy_agent",
        status="active",
    )
    governance_guidance = derive_runtime_guidance(governance_libs)
    governance_appendix = str(governance_guidance.get("prompt_appendix") or "").strip()
    if governance_appendix:
        existing_draft_prompt = str(strategy_for_task.get("draft") or "").strip()
        strategy_for_task["draft"] = (
            f"{governance_appendix}\n{existing_draft_prompt}".strip()
            if existing_draft_prompt
            else governance_appendix
        )
    strategy_for_task["governance_context"] = {
        "rule_refs": governance_guidance.get("rule_refs") if isinstance(governance_guidance.get("rule_refs"), list) else [],
        "playbook_refs": governance_guidance.get("playbook_refs") if isinstance(governance_guidance.get("playbook_refs"), list) else [],
        "io_rule_refs": governance_guidance.get("io_rule_refs") if isinstance(governance_guidance.get("io_rule_refs"), list) else [],
        "method_steps": governance_guidance.get("method_steps") if isinstance(governance_guidance.get("method_steps"), list) else [],
    }
    if content_branch == PROJECT_OBSERVER:
        variants = ("discovery_first", "input_first", "capability_first")
        variant = str(intent.get("content_variant") or variants[sum(map(ord, pipeline_task_id)) % len(variants)])
        if variant not in variants:
            variant = variants[0]
        strategy_for_task = observer_strategy(strategy_for_task, project_reference, variant)
        intel_items = [{"raw_text": json.dumps(project_reference, ensure_ascii=False),
                        "source_type": "project_reference", "source_url": project_reference.get("source_url", ""),
                        "captured_at": project_reference.get("checked_at")}]
    focus_keywords = [
        str(item).strip()
        for item in (strategy_for_task.get("focus_keywords") if isinstance(strategy_for_task.get("focus_keywords"), list) else [])
        if str(item).strip()
    ][:16]
    quality_gate_base = (
        account_strategy.get("quality_gate")
        if isinstance(account_strategy.get("quality_gate"), dict)
        else {}
    )
    quality_gate_config = _merge_quality_gate(
        quality_gate_base,
        governance_guidance.get("quality_gate") if isinstance(governance_guidance.get("quality_gate"), dict) else {},
    )
    if content_branch == PROJECT_OBSERVER:
        quality_gate_config.update({"content_branch": content_branch, "project_reference": project_reference})
    forbidden_claim_candidates = []
    forbidden_claim_candidates.extend(
        account_strategy.get("forbidden_claims")
        if isinstance(account_strategy.get("forbidden_claims"), list)
        else []
    )
    forbidden_claim_candidates.extend(
        governance_guidance.get("forbidden_claims")
        if isinstance(governance_guidance.get("forbidden_claims"), list)
        else []
    )
    forbidden_claims = [
        str(item).strip()
        for item in forbidden_claim_candidates
        if str(item).strip()
    ][:12]
    strategy_for_task["forbidden_claims"] = forbidden_claims
    style_samples = _collect_style_samples(client, domain_id=str(domain["id"]), domain_slug=str(domain.get("slug") or ""), account_id=account_id, limit=6)
    if content_branch == PROJECT_OBSERVER:
        style_samples = []

    router = get_router()
    draft = router.generate_draft(strategy=strategy_for_task, intel=intel_items, channel=pipeline_task["channel"])
    base_title = _normalize_space(str(draft.get("title") or ""))
    base_body = _normalize_space(str(draft.get("body") or ""))
    quality_before = _evaluate_xhs_quality(
        title=base_title,
        body=base_body,
        focus_keywords=focus_keywords,
        forbidden_claims=forbidden_claims,
        quality_gate_config=quality_gate_config,
    )

    polished = _heuristic_polish_draft(
        title=base_title,
        body=base_body,
        focus_keywords=focus_keywords,
        style_samples=style_samples,
        content_branch=content_branch,
    )
    quality_after = _evaluate_xhs_quality(
        title=polished["title"],
        body=polished["body"],
        focus_keywords=focus_keywords,
        forbidden_claims=forbidden_claims,
        quality_gate_config=quality_gate_config,
    )

    final_title = polished["title"] if float(quality_after["score"]) >= float(quality_before["score"]) else base_title
    final_body = polished["body"] if float(quality_after["score"]) >= float(quality_before["score"]) else base_body
    quality_final = quality_after if float(quality_after["score"]) >= float(quality_before["score"]) else quality_before
    quality_rewrite: Dict[str, Any] | None = None
    rewrite_applied = False
    reference_layout_applied = False

    if pipeline_task["channel"] == "xiaohongshu" and not bool(quality_final.get("passed")):
        rewritten = router.rewrite_draft(
            strategy=strategy_for_task,
            intel=intel_items,
            channel=pipeline_task["channel"],
            draft={"title": final_title, "body": final_body},
            quality_report=quality_final,
            style_samples=style_samples,
        )
        rewritten = _heuristic_polish_draft(
            title=str(rewritten.get("title") or final_title),
            body=str(rewritten.get("body") or final_body),
            focus_keywords=focus_keywords,
            style_samples=style_samples,
            content_branch=content_branch,
        )
        quality_rewrite = _evaluate_xhs_quality(
            title=rewritten["title"],
            body=rewritten["body"],
            focus_keywords=focus_keywords,
            forbidden_claims=forbidden_claims,
            quality_gate_config=quality_gate_config,
        )
        rewrite_applied = True
        if float(quality_rewrite["score"]) >= float(quality_final["score"]):
            final_title = rewritten["title"]
            final_body = rewritten["body"]
            quality_final = quality_rewrite

    if content_branch == PROJECT_OBSERVER and not quality_final.get("passed"):
        reference_draft = observer_fallback(strategy_for_task)
        reference_quality = _evaluate_xhs_quality(title=reference_draft["title"], body=reference_draft["body"],
            focus_keywords=focus_keywords, forbidden_claims=forbidden_claims, quality_gate_config=quality_gate_config)
        if reference_quality["passed"]:
            final_title, final_body = reference_draft["title"], reference_draft["body"]
            quality_final = reference_quality
            reference_layout_applied = True

    title = _normalize_space(final_title)
    body = _normalize_space(final_body)
    reference_items = [
        {
            "source_type": item.get("source_type"),
            "source_url": item.get("source_url"),
            "captured_at": item.get("captured_at"),
            "raw_text": str(item.get("raw_text") or "")[:220],
        }
        for item in intel_items[:8]
        if isinstance(item, dict)
    ]
    evidence_map = _build_evidence_map(
        title=title,
        body=body,
        reference_items=reference_items,
        focus_keywords=focus_keywords,
        account_strategy=account_strategy if isinstance(account_strategy, dict) else {},
    )
    optimization_hint = intent.get("optimization_hint") if isinstance(intent.get("optimization_hint"), dict) else {}
    if not optimization_hint:
        optimization_hint = _latest_optimization_hint(client, domain["id"])
    analysis_jsonb = {
        "logic": [
            "提取热点与观点信号",
            "对齐赛道策略与高转化历史模式",
            "生成标题/正文并嵌入行动引导",
            "输出图片创意与审核要点",
        ],
        "reference_items": reference_items,
        "evidence_map": evidence_map,
        "optimization_hint": optimization_hint,
        "source_opinion": source_opinion,
        "account_strategy": {
            "account_id": account_strategy.get("account_id"),
            "account_name": account_strategy.get("account_name"),
            "persona_name": account_strategy.get("persona_name"),
            "ip_positioning": account_strategy.get("ip_positioning"),
            "primary_goal": account_strategy.get("primary_goal"),
            "focus_keywords": strategy_for_task.get("focus_keywords", []),
        },
            "active_prompt_versions": {
                key: {
                    "version": str(value.get("version") or ""),
                    "updated_at": value.get("updated_at"),
                }
                for key, value in active_prompt_versions.items()
                if isinstance(value, dict)
            },
            "governance": {
                "rule_refs": strategy_for_task.get("governance_context", {}).get("rule_refs", []),
                "playbook_refs": strategy_for_task.get("governance_context", {}).get("playbook_refs", []),
                "io_rule_refs": strategy_for_task.get("governance_context", {}).get("io_rule_refs", []),
                "method_steps": strategy_for_task.get("governance_context", {}).get("method_steps", []),
            },
            "quality_gate": {
                "before": quality_before,
                "after_heuristic": quality_after,
                "rewrite_attempted": rewrite_applied,
                "after_rewrite": quality_rewrite,
                "reference_layout_applied": reference_layout_applied,
                "final": quality_final,
            },
            "style_samples": [
                {
                    "title": str(item.get("title") or "")[:80],
                    "content": str(item.get("content") or "")[:120],
                    "source": str(item.get("source") or ""),
                }
                for item in style_samples[:6]
                if isinstance(item, dict)
            ],
            "memory_items": [
                {
                    "type": row.get("type"),
                "title": row.get("title"),
                "content": str(row.get("content") or "")[:220],
                "tags": row.get("tags") or [],
            }
            for row in memory_items[:12]
            if isinstance(row, dict)
        ],
    }
    image_prompt = _draft_image_prompt(domain, title, body)
    if content_branch == PROJECT_OBSERVER:
        analysis_jsonb["logic"] = ["核对项目资料与来源", "用技术手段、优势和输入输出讲清项目",
                                   "以第三者设想的职业生活画面引出共情", "最后揭晓项目名称，保留人工审核"]
        evidence_map["body_framework"] = {"template": "项目事实与输入输出 -> 设想的职业画面 -> 名称揭晓",
            "reasons": ["结构可以变动；事实来自官方资料，职业画面不当作事实证据"]}
        imagined = False
        for section in evidence_map["sections"]:
            preview = section["preview"]
            if any(word in preview for word in ("想象", "假如", "如果")):
                imagined = True
            if project_reference["name"] in preview:
                imagined = False
            section.update({"source_type": "imagined" if imagined else "project_reference",
                "evidence_refs": [] if imagined else ["ref_1"],
                "why_this_section": "创作设想，不是真人采访或实测" if imagined else "根据已核对的项目资料组织信息"})
        analysis_jsonb["content_branch"] = {"id": content_branch, "name": BRANCH_NAME, "version": BRANCH_VERSION,
            "rules": OBSERVER_RULES, "variant": strategy_for_task["content_variant"],
            "output_method": "reference_layout" if reference_layout_applied else "draft_router",
            "project_reference": project_reference, "reaction_type": "imagined", "review_required": True}

    next_payload = _merge_json(
        pipeline_task.get("payload_jsonb"),
        {
            "title": title,
            "body": body,
            "image_prompt": image_prompt,
            "analysis_jsonb": analysis_jsonb,
            "content_branch": content_branch,
        },
    )
    reviewed = _update_pipeline_task(
        client,
        pipeline_task_id,
        {
            "status": "pending_review",
            "stage": "human_review",
            "payload_jsonb": next_payload,
            "review_jsonb": _merge_json(
                pipeline_task.get("review_jsonb"),
                {"review_required": True, "review_updated_at": _now_iso()},
            ),
        },
    )

    _pipeline_audit(
        client,
        action="pipeline.task_drafted",
        target_id=pipeline_task_id,
        diff={
            "to_status": "pending_review",
            "channel": pipeline_task["channel"],
        },
    )
    return {
        "status": "ok",
        "mode": "draft",
        "pipeline_task_id": pipeline_task_id,
        "task": reviewed,
    }


def _publish_pipeline_task(client: Client, pipeline_task: Dict[str, Any], domain: Dict[str, Any]) -> Dict[str, Any]:
    pipeline_task_id = pipeline_task["id"]
    payload = pipeline_task.get("payload_jsonb") or {}
    channel_account_id = str(payload.get("channel_account_id") or "")
    account_runtime = _get_channel_account_runtime(client, channel_account_id) if channel_account_id else None

    if pipeline_task.get("channel") == "xiaohongshu":
        if not channel_account_id:
            auto_runtime = _get_single_active_channel_account_runtime(client, "xiaohongshu")
            if auto_runtime and str(auto_runtime.get("id") or "").strip():
                channel_account_id = str(auto_runtime.get("id") or "").strip()
                account_runtime = auto_runtime
                payload = _merge_json(payload, {"channel_account_id": channel_account_id})
                _update_pipeline_task(client, pipeline_task_id, {"payload_jsonb": payload})
            else:
                failed_row = _update_pipeline_task(
                    client,
                    pipeline_task_id,
                    {
                        "status": "publish_failed",
                        "stage": "publishing",
                        "publish_jsonb": _merge_json(
                            pipeline_task.get("publish_jsonb"),
                            {"last_result": {"status": "failed", "error": "missing_channel_account_id"}},
                        ),
                    },
                )
                return {
                    "status": "ok",
                    "mode": "publish",
                    "pipeline_task_id": pipeline_task_id,
                    "task": failed_row,
                    "publish_result": {"status": "failed", "error": "missing_channel_account_id"},
                }
        if not account_runtime:
            failed_row = _update_pipeline_task(
                client,
                pipeline_task_id,
                {
                    "status": "publish_failed",
                    "stage": "publishing",
                    "publish_jsonb": _merge_json(
                        pipeline_task.get("publish_jsonb"),
                        {"last_result": {"status": "failed", "error": "channel_account_not_found_or_inactive"}},
                    ),
                },
            )
            return {
                "status": "ok",
                "mode": "publish",
                "pipeline_task_id": pipeline_task_id,
                "task": failed_row,
                "publish_result": {"status": "failed", "error": "channel_account_not_found_or_inactive"},
            }
    publish_selector = (
        payload.get("publish_selector")
        or (account_runtime or {}).get("publish_selector")
    )
    login_mode = str(payload.get("playwright_login_mode") or (account_runtime or {}).get("login_mode") or "").strip()
    storage_state_path = payload.get("playwright_storage_state_path") or (account_runtime or {}).get("storage_state_path")
    user_data_dir = payload.get("playwright_user_data_dir") or (account_runtime or {}).get("user_data_dir")
    cookies_json = payload.get("playwright_session_cookies_json") or (account_runtime or {}).get("cookies_json")
    login_username = payload.get("playwright_login_username") or (account_runtime or {}).get("login_username")
    login_password = payload.get("playwright_login_password") or (account_runtime or {}).get("login_password")

    if pipeline_task.get("channel") == "xiaohongshu" and login_mode == "credential":
        # Credential mode should not leak global/default logged state.
        storage_state_path = ""
        user_data_dir = ""
        cookies_json = ""

    meta_jsonb = _merge_json(
        pipeline_task.get("intent_jsonb"),
        {
            "title": payload.get("title"),
            "body": payload.get("body"),
            "hashtags": payload.get("hashtags"),
            "tags": payload.get("tags"),
            "cta": payload.get("cta"),
            "content_type": pipeline_task.get("content_type"),
            "images": payload.get("images"),
            "image_paths": payload.get("image_paths"),
            "local_images": payload.get("local_images"),
            "video": payload.get("video"),
            "video_path": payload.get("video_path"),
            "schedule_at": payload.get("schedule_at"),
            "publish_method_order": payload.get("publish_method_order"),
            "publish_selector": publish_selector,
            "channel_account_id": channel_account_id or None,
            "playwright_storage_state_path": storage_state_path,
            "playwright_user_data_dir": user_data_dir,
            "playwright_session_cookies_json": cookies_json,
            "playwright_login_mode": login_mode,
            "playwright_login_username": login_username,
            "playwright_login_password": login_password,
            "playwright_strict_account_scope": bool(channel_account_id),
            "expected_account_name": (account_runtime or {}).get("account_name"),
            "expected_account_handle": (account_runtime or {}).get("account_handle"),
            "verify_account_name": bool(str((account_runtime or {}).get("account_handle") or "").strip()),
            "pipeline_task_id": pipeline_task_id,
        },
    )

    _update_pipeline_task(client, pipeline_task_id, {"status": "publishing", "stage": "publishing"})
    publish_input = {
        "id": pipeline_task_id,
        "channel": pipeline_task["channel"],
        "meta_jsonb": meta_jsonb,
    }
    runtime_metadata = {
        "channel": pipeline_task.get("channel"),
        "login_mode": login_mode,
        "storage_state_path": storage_state_path,
        "user_data_dir": user_data_dir,
        "cookies_json": cookies_json,
    }
    if channel_account_id:
        upsert_account_runtime(channel_account_id, runtime_metadata)
    with account_execution_scope(
        channel_account_id,
        action="publish",
        task_id=pipeline_task_id,
        metadata=runtime_metadata,
    ):
        result = publish_with_fallback(publish_input)
    publish_identity = extract_publish_identity(result)
    if publish_identity:
        result = _merge_json(result, publish_identity)

    publish_status = "published" if result.get("status") == "success" else "publish_failed"
    verification_pending = bool(result.get("verification_pending")) or not bool(str(result.get("feed_id") or "").strip())
    feedback_state = "pending_identity" if verification_pending else "pending_metrics"
    feedback_schedule_hours = [1, 3, 24]
    published_at_iso = _now_iso() if publish_status == "published" else None
    next_payload = _merge_json(
        payload,
        {
            "published_url": result.get("published_url"),
            "remote_post_id": result.get("remote_post_id"),
            "feed_id": result.get("feed_id"),
            "xsec_token": result.get("xsec_token"),
            "canonical_note_url": result.get("canonical_note_url"),
        },
    )
    next_fields = {
        "status": publish_status,
        "stage": "feedback_pending" if publish_status == "published" else "publishing",
        "payload_jsonb": next_payload,
        "publish_jsonb": _merge_json(
            pipeline_task.get("publish_jsonb"),
            {
                "last_result": result,
                "identity": publish_identity,
                "verification_pending": verification_pending,
                "verification_method": result.get("verification_method") or ("identity" if not verification_pending else "title_match"),
                "feedback_state": feedback_state,
                "feedback_schedule_hours": feedback_schedule_hours,
                "feedback_completed_hours": [],
                "last_feedback_at": None,
                "next_feedback_at": (
                    datetime.now(timezone.utc) + timedelta(hours=feedback_schedule_hours[0])
                ).isoformat()
                if publish_status == "published"
                else None,
            },
        ),
        "published_at": published_at_iso,
    }
    published_row = _update_pipeline_task(client, pipeline_task_id, next_fields)

    if publish_status != "published":
        _pipeline_audit(
            client,
            action="pipeline.task_publish_failed",
            target_id=pipeline_task_id,
            diff={"result": result},
        )
        return {
            "status": "ok",
            "mode": "publish",
            "pipeline_task_id": pipeline_task_id,
            "task": published_row,
            "publish_result": result,
        }

    metrics_sync_result = sync_pipeline_task_metrics_via_mcp(client, published_row)
    finalized = published_row
    if metrics_sync_result.get("status") == "ok":
        finalized = _update_pipeline_task(
            client,
            pipeline_task_id,
            {
                "status": "published",
                "stage": "feedback_pending",
                "publish_jsonb": _merge_json(
                    published_row.get("publish_jsonb"),
                    {
                        "feedback_state": "pending_metrics",
                        "last_feedback_at": _now_iso(),
                        "last_metrics_result": metrics_sync_result,
                    },
                ),
            },
        )

    _pipeline_audit(
        client,
        action="pipeline.task_published",
        target_id=pipeline_task_id,
        diff={
            "publish_result": result,
            "metrics_sync_result": metrics_sync_result,
        },
    )
    return {
        "status": "ok",
        "mode": "publish",
        "pipeline_task_id": pipeline_task_id,
        "task": finalized,
        "publish_result": result,
        "metrics_sync_result": metrics_sync_result,
        "reflection_result": {"status": "scheduled_for_delayed_feedback"},
    }


def run_pipeline_task(client: Client, pipeline_task_id: str) -> Dict[str, Any]:
    pipeline_task = _get_pipeline_task(client, pipeline_task_id)
    if not pipeline_task:
        return {"status": "error", "reason": "pipeline_task_not_found", "pipeline_task_id": pipeline_task_id}

    domain = _get_domain(client, pipeline_task["domain_id"])
    if not domain:
        return {"status": "error", "reason": "domain_not_found", "pipeline_task_id": pipeline_task_id}

    task_status = pipeline_task.get("status")
    if task_status in DRAFTABLE_STATUSES:
        intent = pipeline_task.get("intent_jsonb") if isinstance(pipeline_task.get("intent_jsonb"), dict) else {}
        payload = pipeline_task.get("payload_jsonb") if isinstance(pipeline_task.get("payload_jsonb"), dict) else {}
        generation_mode = str(intent.get("generation_mode") or payload.get("generation_mode") or "").strip().lower()
        manual_flag = bool(payload.get("manual_input")) or generation_mode in {
            "manual_input",
            "manual_override",
            "skip_rebuild",
            "direct_to_review",
        }
        has_manual_copy = bool(str(payload.get("title") or "").strip() and str(payload.get("body") or "").strip())
        if manual_flag or has_manual_copy:
            return _draft_manual_pipeline_task(client, pipeline_task, domain)
        return _draft_pipeline_task(client, pipeline_task, domain)
    if task_status == "approved":
        return {
            "status": "blocked",
            "reason": "delegated_publishing_disabled",
            "message": "AI 代发布已取消，草稿保留，请自行发布。",
            "pipeline_task_id": pipeline_task_id,
        }
    if task_status == "pending_review":
        return {
            "status": "ok",
            "mode": "noop",
            "reason": "already_pending_review",
            "pipeline_task_id": pipeline_task_id,
        }
    return {
        "status": "ok",
        "mode": "noop",
        "reason": f"unsupported_status:{task_status}",
        "pipeline_task_id": pipeline_task_id,
    }




