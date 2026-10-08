from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from html import unescape
import re
from time import sleep
from typing import Any, Dict, List
from urllib.parse import quote_plus
from urllib.request import urlopen
from uuid import uuid4
import xml.etree.ElementTree as ET

import httpx
from postgrest.exceptions import APIError
from supabase import Client

from app.config import get_settings
from app.orchestration.pipeline_runner import run_pipeline_task
from app.runtime.browser.playwright_runtime import paced_wait, playwright_page_scope
from app.runtime.accounts.account_runtime import account_execution_scope, mark_account_runtime, upsert_account_runtime
from app.runtime.accounts.account_strategy import mcp_flags_from_mode, strategy_from_account
from app.tools.integrations.xhs_mcp_readonly import (
    collect_xhs_home_intel_via_mcp,
    collect_xhs_search_intel_via_mcp,
    get_mcp_readonly_status,
)
from app.tools.publishing.publisher import _open_context


HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
URL_RE = re.compile(r"https?://\S+")


def _normalize_collection_plan(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    steps_raw = src.get("steps") if isinstance(src.get("steps"), list) else []
    fallback_raw = src.get("fallback") if isinstance(src.get("fallback"), list) else []

    def _normalize_steps(items: List[Any]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for item in items[:12]:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "").strip()
            if not tool:
                continue
            try:
                limit = int(item.get("limit") or 8)
            except (TypeError, ValueError):
                limit = 8
            scope = str(item.get("scope") or "").strip().lower()
            if scope not in {"global", "account"}:
                scope = "account" if tool.startswith("playwright_") else "global"
            result.append(
                {
                    "tool": tool,
                    "limit": max(1, min(50, limit)),
                    "query": str(item.get("query") or "").strip(),
                    "profile_hint": str(item.get("profile_hint") or "").strip(),
                    "profile_id": str(item.get("profile_id") or "").strip(),
                    "detail_url": str(item.get("detail_url") or "").strip(),
                    "scope": scope,
                    "reason": str(item.get("reason") or "").strip(),
                    "fallback_to": str(item.get("fallback_to") or "").strip() or None,
                    "label": str(item.get("label") or "").strip(),
                }
            )
        return result

    return {
        "mode": str(src.get("mode") or "hybrid").strip() or "hybrid",
        "steps": _normalize_steps(steps_raw),
        "fallback": _normalize_steps(fallback_raw),
        "notes": str(src.get("notes") or "").strip(),
    }


def _normalize_text(text: str) -> str:
    cleaned = unescape(text or "")
    cleaned = HTML_TAG_RE.sub(" ", cleaned)
    cleaned = URL_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("|", " ").replace("·", " ")
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


def _domain_keywords(domain: Dict[str, Any], source_kind: str) -> List[str]:
    cfg = domain.get("config_jsonb") or {}
    configured = cfg.get("focus_keywords")
    if isinstance(configured, list):
        values = [str(x).strip() for x in configured if str(x).strip()]
        if values:
            return values

    base = ["日本", "签证", "移民", "永住", "留学", "经营管理", "归化", "在留", "高才", "优才"]
    if source_kind == "viewpoint":
        base.extend(["争议", "观点", "质疑", "风险", "避坑"])
    return base


def _is_relevant_item(text: str, keywords: List[str]) -> bool:
    lower_text = text.lower()
    for kw in keywords:
        token = kw.strip().lower()
        if token and token in lower_text:
            return True
    return False


def parse_google_news_rss(xml_text: str, limit: int = 5) -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: List[Dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()
        if not title or not link:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "description": description,
            }
        )
        if len(items) >= max(1, limit):
            break
    return items


def _load_domain(client: Client, domain_slug: str) -> Dict[str, Any] | None:
    res = client.table("domains").select("*").eq("slug", domain_slug).limit(1).execute()
    if not res.data:
        return None
    return res.data[0]


def _table_ready(client: Client, table_name: str) -> bool:
    try:
        client.table(table_name).select("id").limit(1).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def _loop_ledger_ready(client: Client) -> bool:
    return _table_ready(client, "runs_daily") and _table_ready(client, "decisions")


def _memory_ready(client: Client) -> bool:
    return _table_ready(client, "memory_items")


def _safe_upsert_account_memory(
    client: Client,
    *,
    domain_id: str,
    account_id: str,
    account_strategy: Dict[str, Any],
    actor: str,
) -> None:
    if not _memory_ready(client):
        return
    if not account_id:
        return
    persona_name = str(account_strategy.get("persona_name") or "").strip()
    ip_positioning = str(account_strategy.get("ip_positioning") or "").strip()
    tone_style = str(account_strategy.get("tone_style") or "").strip()
    primary_goal = str(account_strategy.get("primary_goal") or "").strip()
    cta_style = str(account_strategy.get("cta_style") or "").strip()

    base_items: List[Dict[str, Any]] = []
    if persona_name:
        base_items.append(
            {
                "type": "brand_preference",
                "title": "账号人设",
                "content": persona_name,
                "tags": ["account", "persona"],
            }
        )
    if ip_positioning:
        base_items.append(
            {
                "type": "brand_preference",
                "title": "账号定位",
                "content": ip_positioning,
                "tags": ["account", "positioning"],
            }
        )
    if tone_style:
        base_items.append(
            {
                "type": "brand_preference",
                "title": "语气风格",
                "content": tone_style,
                "tags": ["copy", "tone"],
            }
        )
    if primary_goal:
        base_items.append(
            {
                "type": "strategy_rule",
                "title": "核心目标",
                "content": primary_goal,
                "tags": ["goal", "conversion"],
            }
        )
    if cta_style:
        base_items.append(
            {
                "type": "strategy_rule",
                "title": "CTA规则",
                "content": cta_style,
                "tags": ["cta", "engagement"],
            }
        )

    for item in base_items:
        try:
            existing = (
                client.table("memory_items")
                .select("id,content")
                .eq("domain_id", domain_id)
                .eq("account_id", account_id)
                .eq("status", "active")
                .eq("type", item["type"])
                .eq("title", item["title"])
                .limit(1)
                .execute()
            ).data or []
            if existing:
                current = existing[0]
                if str(current.get("content") or "").strip() == str(item["content"]).strip():
                    continue
                client.table("memory_items").update(
                    {
                        "content": item["content"],
                        "tags": item["tags"],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "created_by": actor,
                    }
                ).eq("id", current["id"]).execute()
            else:
                client.table("memory_items").insert(
                    {
                        "domain_id": domain_id,
                        "account_id": account_id,
                        "type": item["type"],
                        "title": item["title"],
                        "content": item["content"],
                        "tags": item["tags"],
                        "status": "active",
                        "confidence": 0.9,
                        "created_by": actor,
                    }
                ).execute()
        except Exception:  # noqa: BLE001
            continue


def _load_active_memory_items(client: Client, domain_id: str, account_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    if not _memory_ready(client):
        return []
    try:
        rows = (
            client.table("memory_items")
            .select("type,title,content,tags,status,account_id,updated_at")
            .eq("domain_id", domain_id)
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(max(1, min(limit, 100)))
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


def _metrics_last_7d(client: Client, domain_id: str) -> List[Dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        rows = (
            client.table("task_metrics")
            .select("captured_at,metrics_jsonb")
            .gte("captured_at", since)
            .order("captured_at", desc=False)
            .limit(80)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        rows = []

    if rows:
        result: List[Dict[str, Any]] = []
        for row in rows[:30]:
            metrics = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
            result.append(
                {
                    "date": str(row.get("captured_at") or "")[:10],
                    "views": int(metrics.get("views") or 0),
                    "likes": int(metrics.get("likes") or 0),
                    "collects": int(metrics.get("collects") or 0),
                    "comments_count": int(metrics.get("comments_count") or 0),
                    "shares": int(metrics.get("shares") or 0),
                }
            )
        return result

    # Fallback for MVP: derive from published pipeline task signals.
    try:
        task_rows = (
            client.table("pipeline_tasks")
            .select("published_at,metrics_jsonb")
            .eq("domain_id", domain_id)
            .in_("status", ["published", "done"])
            .gte("published_at", since)
            .order("published_at", desc=False)
            .limit(50)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        task_rows = []

    fallback: List[Dict[str, Any]] = []
    for row in task_rows:
        metrics = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
        leads = int(metrics.get("leads_generated") or metrics.get("leads") or 0)
        fallback.append(
            {
                "date": str(row.get("published_at") or "")[:10],
                "views": 0,
                "likes": 0,
                "collects": 0,
                "comments_count": 0,
                "shares": 0,
                "leads_count": leads,
            }
        )
    return fallback


def _top_posts_last_30d(client: Client, domain_id: str) -> List[Dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = (
        client.table("pipeline_tasks")
        .select("id,published_at,payload_jsonb,metrics_jsonb")
        .eq("domain_id", domain_id)
        .in_("status", ["published", "done"])
        .gte("published_at", since)
        .order("published_at", desc=True)
        .limit(80)
        .execute()
    ).data or []
    ranked: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        metrics = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
        title = str(payload.get("title") or "").strip()
        if not title:
            continue
        views = int(metrics.get("views") or 0)
        likes = int(metrics.get("likes") or 0)
        collects = int(metrics.get("collects") or 0)
        comments = int(metrics.get("comments_count") or 0)
        leads = int(metrics.get("leads_generated") or metrics.get("leads") or 0)
        score = likes + collects + comments + leads * 5
        ranked.append(
            {
                "post_id": str(row.get("id") or ""),
                "title": title,
                "views": views,
                "likes": likes,
                "collects": collects,
                "comments_count": comments,
                "leads_count": leads,
                "published_at": row.get("published_at"),
                "_score": score,
            }
        )
    ranked.sort(key=lambda item: int(item.get("_score") or 0), reverse=True)
    result: List[Dict[str, Any]] = []
    for row in ranked[:20]:
        result.append(
            {
                "post_id": row.get("post_id"),
                "title": row.get("title"),
                "views": int(row.get("views") or 0),
                "likes": int(row.get("likes") or 0),
                "collects": int(row.get("collects") or 0),
                "comments_count": int(row.get("comments_count") or 0),
                "leads_count": int(row.get("leads_count") or 0),
                "published_at": row.get("published_at"),
            }
        )
    return result


def _recent_comments_sample(client: Client, domain_id: str, limit: int = 12) -> List[Dict[str, Any]]:
    # MVP fallback: use recent viewpoint/raw comments-like signals from intelligence items.
    rows = (
        client.table("intelligence_items")
        .select("raw_text,captured_at,source_type")
        .eq("domain_id", domain_id)
        .order("captured_at", desc=True)
        .limit(max(1, min(limit, 30)))
        .execute()
    ).data or []
    result: List[Dict[str, Any]] = []
    for row in rows:
        source_type = str(row.get("source_type") or "")
        if "viewpoint" not in source_type and "xiaohongshu" not in source_type:
            continue
        text = str(row.get("raw_text") or "").strip()
        if not text:
            continue
        result.append(
            {
                "text": text[:300],
                "created_at": row.get("captured_at"),
                "source_type": source_type,
            }
        )
        if len(result) >= limit:
            break
    return result


def _build_evidence_pack(
    client: Client,
    *,
    domain: Dict[str, Any],
    account_strategy: Dict[str, Any],
    target_posts_min: int,
    topic: str,
    hotspot_items: List[Dict[str, Any]],
    optimization: Dict[str, Any],
) -> Dict[str, Any]:
    account_id = str(account_strategy.get("account_id") or "").strip()
    memory_rows = _load_active_memory_items(client, str(domain["id"]), account_id, limit=30)
    return {
        "account": {
            "id": account_id,
            "nickname": account_strategy.get("account_name") or "",
            "platform": "xhs",
            "persona_name": account_strategy.get("persona_name") or "",
            "ip_positioning": account_strategy.get("ip_positioning") or "",
            "tone_style": account_strategy.get("tone_style") or "",
        },
        "goal": {
            "target_posts_min": target_posts_min,
            "date": datetime.now(timezone.utc).date().isoformat(),
            "primary_goal": account_strategy.get("primary_goal") or "",
            "topic": topic,
        },
        "metrics_last_7d": _metrics_last_7d(client, str(domain["id"])),
        "top_posts_last_30d": _top_posts_last_30d(client, str(domain["id"])),
        "recent_comments_sample": _recent_comments_sample(client, str(domain["id"]), limit=10),
        "hotspot_sample": [
            {
                "source_type": row.get("source_type"),
                "source_url": row.get("source_url"),
                "captured_at": row.get("captured_at"),
                "raw_text": str(row.get("raw_text") or "")[:220],
            }
            for row in hotspot_items[:12]
        ],
        "optimization_hint": optimization,
        "memory_items": memory_rows,
    }


def _start_run_ledger(
    client: Client,
    *,
    run_key: str,
    domain_id: str,
    account_id: str,
    flow: str,
    target_posts_min: int,
) -> str | None:
    if not _loop_ledger_ready(client):
        return None
    try:
        existing = client.table("runs_daily").select("id").eq("run_key", run_key).limit(1).execute().data or []
        if existing:
            return str(existing[0].get("id") or "")
        created = (
            client.table("runs_daily")
            .insert(
                {
                    "run_key": run_key,
                    "domain_id": domain_id,
                    "account_id": account_id or None,
                    "flow": flow,
                    "run_date": datetime.now(timezone.utc).date().isoformat(),
                    "status": "CREATED",
                    "target_posts_min": target_posts_min,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .execute()
        )
        if not created.data:
            return None
        return str(created.data[0].get("id") or "")
    except Exception:  # noqa: BLE001
        return None


def _update_run_ledger(
    client: Client,
    run_id: str | None,
    *,
    status: str | None = None,
    evidence_pack: Dict[str, Any] | None = None,
    llm_plan: Dict[str, Any] | None = None,
    result_jsonb: Dict[str, Any] | None = None,
    retro_report: str | None = None,
    error_message: str | None = None,
    finished: bool = False,
) -> None:
    if not run_id:
        return
    payload: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if status:
        payload["status"] = status
    if isinstance(evidence_pack, dict):
        payload["evidence_pack"] = evidence_pack
    if isinstance(llm_plan, dict):
        payload["llm_plan"] = llm_plan
    if isinstance(result_jsonb, dict):
        payload["result_jsonb"] = result_jsonb
    if retro_report is not None:
        payload["retro_report"] = retro_report
    if error_message is not None:
        payload["error_message"] = error_message[:3000]
    if finished:
        payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    try:
        client.table("runs_daily").update(payload).eq("id", run_id).execute()
    except Exception:  # noqa: BLE001
        return


def _record_decision(
    client: Client,
    *,
    run_id: str | None,
    pipeline_task_id: str | None,
    evidence_pack: Dict[str, Any],
    llm_output: Dict[str, Any],
    summary_text: str,
) -> None:
    if not run_id:
        return
    if not _table_ready(client, "decisions"):
        return
    try:
        client.table("decisions").insert(
            {
                "run_id": run_id,
                "pipeline_task_id": pipeline_task_id,
                "evidence_pack": evidence_pack,
                "llm_output": llm_output,
                "summary_text": summary_text[:3000],
            }
        ).execute()
    except Exception:  # noqa: BLE001
        return


def _record_reflection_memory(
    client: Client,
    *,
    domain_id: str,
    account_id: str,
    run_id: str | None,
    reflection_result: Dict[str, Any] | None,
) -> None:
    if not _memory_ready(client):
        return
    if not isinstance(reflection_result, dict):
        return
    status_text = str(reflection_result.get("status") or "").strip().lower()
    if status_text != "ok":
        return
    note = f"反思结果：{json.dumps(reflection_result, ensure_ascii=False)[:1800]}"
    try:
        client.table("memory_items").insert(
            {
                "domain_id": domain_id,
                "account_id": account_id or None,
                "source_run_id": run_id,
                "type": "strategy_rule",
                "title": "系统建议（待确认）",
                "content": note,
                "tags": ["reflection", "pending_review"],
                "status": "pending",
                "confidence": 0.65,
                "created_by": "system:reflector",
            }
        ).execute()
    except Exception:  # noqa: BLE001
        return


def _split_queries(value: str) -> List[str]:
    return [q.strip() for q in value.split(",") if q.strip()]


def _render_profile_arg_candidates(
    *,
    query: str,
    limit: int,
    id_key: str,
    extra_args: Dict[str, Any] | None,
) -> List[Dict[str, Any]] | None:
    source = extra_args if isinstance(extra_args, dict) else {}
    profile_args = source.get("profile_args") if isinstance(source.get("profile_args"), dict) else {}
    raw_candidates = profile_args.get("arg_candidates") if isinstance(profile_args.get("arg_candidates"), list) else []
    if not raw_candidates:
        return None

    def _replace(v: Any) -> Any:
        if isinstance(v, str):
            return (
                v.replace("{{query}}", query)
                .replace("${query}", query)
                .replace("{{limit}}", str(limit))
                .replace("${limit}", str(limit))
            )
        if isinstance(v, list):
            return [_replace(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _replace(x) for k, x in v.items()}
        return v

    rendered: List[Dict[str, Any]] = []
    for row in raw_candidates:
        if not isinstance(row, dict):
            continue
        patched = _replace(row)
        if not isinstance(patched, dict):
            continue
        if id_key not in patched and "query" not in patched and "keyword" not in patched:
            patched[id_key] = query
        if "limit" not in patched and "count" not in patched:
            patched["limit"] = limit
        rendered.append({str(k): v for k, v in patched.items()})
        if len(rendered) >= 8:
            break
    return rendered or None


def _domain_hotspot_queries(domain: Dict[str, Any], account_strategy: Dict[str, Any] | None = None) -> List[str]:
    settings = get_settings()
    strategy = account_strategy if isinstance(account_strategy, dict) else {}
    strategy_queries = strategy.get("hotspot_queries") if isinstance(strategy.get("hotspot_queries"), list) else []
    if strategy_queries:
        values = [str(x).strip() for x in strategy_queries if str(x).strip()]
        if values:
            return values
    strategy_default = str(strategy.get("mcp_query_default") or "").strip()
    if strategy_default:
        return [strategy_default]
    cfg = domain.get("config_jsonb") or {}
    crawler_template = cfg.get("crawler_template") if isinstance(cfg.get("crawler_template"), dict) else {}
    keyword_groups = crawler_template.get("keyword_groups") if isinstance(crawler_template.get("keyword_groups"), dict) else {}
    template_queries = keyword_groups.get("hotspot_queries")
    if isinstance(template_queries, list):
        queries = [str(x).strip() for x in template_queries if str(x).strip()]
        if queries:
            return queries
    configured = cfg.get("hotspot_queries")
    if isinstance(configured, list):
        queries = [str(x).strip() for x in configured if str(x).strip()]
        if queries:
            return queries
    return _split_queries(settings.daily_hotspot_queries)


def _domain_viewpoint_queries(domain: Dict[str, Any], account_strategy: Dict[str, Any] | None = None) -> List[str]:
    settings = get_settings()
    strategy = account_strategy if isinstance(account_strategy, dict) else {}
    strategy_queries = strategy.get("viewpoint_queries") if isinstance(strategy.get("viewpoint_queries"), list) else []
    if strategy_queries:
        values = [str(x).strip() for x in strategy_queries if str(x).strip()]
        if values:
            return values
    cfg = domain.get("config_jsonb") or {}
    crawler_template = cfg.get("crawler_template") if isinstance(cfg.get("crawler_template"), dict) else {}
    keyword_groups = crawler_template.get("keyword_groups") if isinstance(crawler_template.get("keyword_groups"), dict) else {}
    template_queries = keyword_groups.get("viewpoint_queries")
    if isinstance(template_queries, list):
        queries = [str(x).strip() for x in template_queries if str(x).strip()]
        if queries:
            return queries
    configured = cfg.get("viewpoint_queries")
    if isinstance(configured, list):
        queries = [str(x).strip() for x in configured if str(x).strip()]
        if queries:
            return queries
    return _split_queries(settings.daily_viewpoint_queries)


def _crawler_template(domain: Dict[str, Any]) -> Dict[str, Any]:
    cfg = domain.get("config_jsonb") or {}
    template = cfg.get("crawler_template")
    if isinstance(template, dict):
        return template
    return {}


def _source_enabled(template: Dict[str, Any], key: str, default_value: bool) -> bool:
    sources = template.get("sources") if isinstance(template.get("sources"), dict) else {}
    source_cfg = sources.get(key) if isinstance(sources.get(key), dict) else {}
    if "enabled" in source_cfg:
        return bool(source_cfg.get("enabled"))
    return default_value


def _source_max(template: Dict[str, Any], key: str, default_value: int) -> int:
    sources = template.get("sources") if isinstance(template.get("sources"), dict) else {}
    source_cfg = sources.get(key) if isinstance(sources.get(key), dict) else {}
    value = source_cfg.get("max_per_query")
    if isinstance(value, int):
        return max(1, min(50, value))
    return default_value


def _collect_news_intel(
    *,
    domain: Dict[str, Any],
    queries: List[str],
    max_per_query: int,
    collect_limit: int,
    source_type: str,
    source_kind: str,
) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    keywords = _domain_keywords(domain, source_kind)
    for query in queries:
        if len(collected) >= collect_limit:
            break
        rss_url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        try:
            with urlopen(rss_url, timeout=20) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue

        parsed = parse_google_news_rss(body, limit=max_per_query)
        for item in parsed:
            if len(collected) >= collect_limit:
                break
            if item["link"] in seen_urls:
                continue

            title = _normalize_text(item["title"])
            description = _normalize_text(item["description"])
            text = f"{title} {description}".strip()
            if len(text) < 12:
                continue
            if not _is_relevant_item(text, keywords):
                continue

            seen_urls.add(item["link"])
            collected.append(
                {
                    "source_type": source_type,
                    "source_url": item["link"],
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "raw_text": text[:4000],
                    "meta_jsonb": {
                        "query": query,
                        "pub_date": item["pub_date"],
                        "provider": "google_news_rss",
                        "kind": source_kind,
                    },
                }
            )
    return collected


def _collect_xhs_search_intel(
    *,
    domain: Dict[str, Any],
    queries: List[str],
    max_per_query: int,
    collect_limit: int,
    source_type: str,
    source_kind: str,
) -> List[Dict[str, Any]]:
    if collect_limit <= 0 or max_per_query <= 0 or not queries:
        return []

    collected: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    keywords = _domain_keywords(domain, source_kind)
    scoped_task: Dict[str, Any] = {
        "id": f"daily-xhs-{source_kind}",
        "channel": "xiaohongshu",
        "meta_jsonb": {
            "playwright_strict_account_scope": False,
            "playwright_session_key": f"daily-xhs:{source_kind}",
        },
    }

    def _ensure_xhs_surface(page: Any) -> None:
        try:
            current = str(page.url or "").strip()
        except Exception:  # noqa: BLE001
            current = ""
        if "xiaohongshu.com" in current:
            return
        _open_url_in_current_page(page, "https://www.xiaohongshu.com/explore")

    def _open_url_in_current_page(page: Any, target_url: str) -> None:
        desired = str(target_url or "").strip()
        if not desired:
            return
        clicked = False
        try:
            clicked = bool(
                page.evaluate(
                    """(target) => {
                      const normalize = (value) => String(value || '').trim();
                      const links = Array.from(document.querySelectorAll("a[href]"));
                      const wanted = normalize(target);
                      const candidate = links.find((node) => {
                        const href = normalize(node.getAttribute('href') || node.href || '');
                        if (!href) return false;
                        if (href === wanted) return true;
                        if (wanted.startsWith('http') && href.startsWith('/')) {
                          return (`https://www.xiaohongshu.com${href}` === wanted);
                        }
                        return false;
                      });
                      if (!candidate) return false;
                      candidate.scrollIntoView({ behavior: 'instant', block: 'center' });
                      candidate.click();
                      return true;
                    }""",
                    desired,
                )
            )
        except Exception:  # noqa: BLE001
            clicked = False
        if clicked:
            paced_wait(page, 1200, 2200)
            return
        page.goto(desired, wait_until="domcontentloaded", timeout=60_000)
        paced_wait(page, 1200, 2200)

    def _search_keyword_via_page(page: Any, query: str) -> None:
        keyword = str(query or "").strip()
        if not keyword:
            return
        _ensure_xhs_surface(page)
        try:
            for selector in [
                "div[role='dialog'] button:has-text('关闭')",
                "div[role='dialog'] .close",
                "div[class*='login'] button:has-text('关闭')",
                "div[class*='login'] .close",
            ]:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    locator.click(timeout=1200)
                    paced_wait(page, 200, 500)
        except Exception:  # noqa: BLE001
            pass
        selectors = [
            "input[placeholder*='搜索']",
            "input[placeholder*='搜']",
            "input[type='search']",
            "header input",
        ]
        filled = False
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() <= 0:
                    continue
                locator.click(timeout=2000)
                locator.fill(keyword, timeout=3000)
                locator.press("Enter", timeout=2000)
                filled = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not filled:
            _open_url_in_current_page(
                page,
                f"https://www.xiaohongshu.com/search_result?keyword={quote_plus(keyword)}&source=web_explore_feed",
            )
        else:
            paced_wait(page, 1200, 2200)

    try:
        with playwright_page_scope(scoped_task, context_factory=_open_context) as page:
            for query in queries:
                if len(collected) >= collect_limit:
                    break
                try:
                    _search_keyword_via_page(page, query)
                    raw_items = page.evaluate(
                        """(maxItems) => {
                          const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                          const rows = [];
                          const seen = new Set();
                          const links = Array.from(document.querySelectorAll("a[href*='/explore/']"));
                          for (const link of links) {
                            const hrefRaw = link.getAttribute('href') || link.href || '';
                            if (!hrefRaw) continue;
                            const href = hrefRaw.startsWith('http') ? hrefRaw : `https://www.xiaohongshu.com${hrefRaw}`;
                            if (seen.has(href)) continue;
                            const text = normalize(link.textContent);
                            if (!text || text.length < 8) continue;
                            seen.add(href);
                            rows.push({ title: text.slice(0, 180), url: href });
                            if (rows.length >= maxItems) break;
                          }
                          return rows;
                        }""",
                        max(1, max_per_query),
                    )
                except Exception:  # noqa: BLE001
                    continue

                if not isinstance(raw_items, list):
                    continue
                for item in raw_items:
                    if len(collected) >= collect_limit:
                        break
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url") or "").strip()
                    title = _normalize_text(str(item.get("title") or ""))
                    if not url or not title:
                        continue
                    if url in seen_urls:
                        continue
                    if not _is_relevant_item(title, keywords):
                        continue

                    seen_urls.add(url)
                    collected.append(
                        {
                            "source_type": source_type,
                            "source_url": url,
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "raw_text": title[:4000],
                            "meta_jsonb": {
                                "query": query,
                                "provider": "xiaohongshu_search_playwright",
                                "kind": source_kind,
                            },
                        }
                    )
            paced_wait(page, 2000, 3000)
    except Exception:  # noqa: BLE001
        return collected
    return collected


def _collect_xhs_profile_intel(
    *,
    domain: Dict[str, Any],
    profile_hint: str,
    collect_limit: int,
    source_type: str,
    source_kind: str,
) -> List[Dict[str, Any]]:
    normalized_hint = str(profile_hint or "").strip()
    if not normalized_hint or collect_limit <= 0:
        return []
    # Current MVP implementation reuses search collector with profile_hint as query seed.
    return _collect_xhs_search_intel(
        domain=domain,
        queries=[normalized_hint],
        max_per_query=max(1, min(collect_limit, 12)),
        collect_limit=collect_limit,
        source_type=source_type,
        source_kind=source_kind,
    )


def _append_unique_items(
    collected: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    *,
    collect_limit: int,
) -> List[Dict[str, Any]]:
    deduped = list(collected)
    seen = {
        f"{str(item.get('source_url') or '').strip()}|{str(item.get('raw_text') or '').strip()[:120]}".lower()
        for item in deduped
    }
    for item in incoming:
        if len(deduped) >= collect_limit:
            break
        source_url = str(item.get("source_url") or "").strip()
        raw_text = str(item.get("raw_text") or "").strip()
        key = f"{source_url}|{raw_text[:120]}".lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _execute_collection_plan_steps(
    *,
    domain: Dict[str, Any],
    source_kind: str,
    collect_limit: int,
    default_queries: List[str],
    collection_plan: Dict[str, Any],
    strategy: Dict[str, Any],
    xhs_max_per_query: int,
) -> List[Dict[str, Any]]:
    steps = collection_plan.get("steps") if isinstance(collection_plan.get("steps"), list) else []
    fallback_steps = collection_plan.get("fallback") if isinstance(collection_plan.get("fallback"), list) else []
    if not steps and not fallback_steps:
        return []

    profile_tool = str(strategy.get("mcp_profile_tool") or "").strip()
    profile_id_key = str(strategy.get("mcp_profile_id_key") or "user_id").strip() or "user_id"
    mcp_extra_args = strategy.get("mcp_extra_args") if isinstance(strategy.get("mcp_extra_args"), dict) else {}

    def _run_steps(items: List[Dict[str, Any]], step_rows: List[Dict[str, Any]], *, fallback: bool = False) -> List[Dict[str, Any]]:
        merged = list(items)
        for step in step_rows:
            if len(merged) >= collect_limit:
                break
            if not isinstance(step, dict):
                continue
            tool = str(step.get("tool") or "").strip().lower()
            query = str(step.get("query") or "").strip()
            profile_hint = str(step.get("profile_hint") or "").strip()
            limit = max(1, min(collect_limit - len(merged), int(step.get("limit") or xhs_max_per_query or 8)))
            queries = [query] if query else default_queries[:1]
            if tool == "mcp_home":
                incoming = collect_xhs_home_intel_via_mcp(
                    collect_limit=limit,
                    source_type=f"{source_kind}_xiaohongshu_mcp_feed" if not fallback else f"{source_kind}_xiaohongshu_mcp_feed_fallback",
                    source_kind=source_kind,
                    tool_name_override=str(strategy.get("mcp_feed_tool") or "").strip() or None,
                )
            elif tool == "mcp_search":
                incoming = collect_xhs_search_intel_via_mcp(
                    queries=queries,
                    max_per_query=min(limit, xhs_max_per_query),
                    collect_limit=limit,
                    source_type=f"{source_kind}_xiaohongshu_mcp" if not fallback else f"{source_kind}_xiaohongshu_mcp_fallback",
                    source_kind=source_kind,
                    tool_name_override=str(strategy.get("mcp_search_tool") or "").strip() or None,
                )
            elif tool == "mcp_profile":
                profile_query = profile_hint or query or (default_queries[0] if default_queries else "")
                profile_args = _render_profile_arg_candidates(
                    query=profile_query,
                    limit=limit,
                    id_key=profile_id_key,
                    extra_args=mcp_extra_args,
                )
                incoming = collect_xhs_search_intel_via_mcp(
                    queries=[profile_query] if profile_query else default_queries[:1],
                    max_per_query=limit,
                    collect_limit=limit,
                    source_type=f"{source_kind}_xiaohongshu_mcp_profile" if not fallback else f"{source_kind}_xiaohongshu_mcp_profile_fallback",
                    source_kind=source_kind,
                    tool_name_override=profile_tool or str(strategy.get("mcp_search_tool") or "").strip() or None,
                    arg_candidates_override=profile_args,
                )
            elif tool == "playwright_account_search":
                incoming = _collect_xhs_search_intel(
                    domain=domain,
                    queries=queries,
                    max_per_query=min(limit, xhs_max_per_query),
                    collect_limit=limit,
                    source_type=f"{source_kind}_xiaohongshu_search" if not fallback else f"{source_kind}_xiaohongshu_search_fallback",
                    source_kind=source_kind,
                )
            elif tool == "playwright_profile":
                incoming = _collect_xhs_profile_intel(
                    domain=domain,
                    profile_hint=profile_hint or query or (default_queries[0] if default_queries else ""),
                    collect_limit=limit,
                    source_type=f"{source_kind}_xiaohongshu_profile" if not fallback else f"{source_kind}_xiaohongshu_profile_fallback",
                    source_kind=source_kind,
                )
            else:
                incoming = []
            merged = _append_unique_items(merged, incoming, collect_limit=collect_limit)
        return merged

    collected = _run_steps([], steps)
    if len(collected) < collect_limit and fallback_steps:
        collected = _run_steps(collected, fallback_steps, fallback=True)
    return collected[:collect_limit]


def _collect_hotspots(domain: Dict[str, Any], account_strategy: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    if ((account_strategy or {}).get("collection_plan") or {}).get("mode") in {"browser_ui", "xhs_cli"}:
        return []
    settings = get_settings()
    strategy = account_strategy if isinstance(account_strategy, dict) else {}
    template = _crawler_template(domain)
    queries = _domain_hotspot_queries(domain, account_strategy=strategy)
    collection_plan = _normalize_collection_plan(
        strategy.get("collection_plan") if isinstance(strategy.get("collection_plan"), dict) else {}
    )
    collection_steps = collection_plan.get("steps") if isinstance(collection_plan.get("steps"), list) else []
    planned_queries = [
        str(step.get("query") or "").strip()
        for step in collection_steps
        if isinstance(step, dict) and str(step.get("query") or "").strip()
    ]
    if planned_queries:
        queries = planned_queries[:8]
    strategy_limit = int(strategy.get("mcp_limit_default") or 0)
    collect_limit = max(1, min(100, strategy_limit)) if strategy_limit > 0 else settings.daily_collect_limit
    mode = str(strategy.get("mcp_mode") or "hotspot").strip().lower()
    mcp_flags = mcp_flags_from_mode(mode, include_home=True, include_search=True)
    planned_tools = {
        str(step.get("tool") or "").strip()
        for step in collection_steps
        if isinstance(step, dict) and str(step.get("tool") or "").strip()
    }
    allow_home = bool(mcp_flags.get("include_home", True))
    allow_search = bool(mcp_flags.get("include_search", True))
    allow_profile = bool(mcp_flags.get("include_profile", False))
    if planned_tools:
        allow_home = "mcp_home" in planned_tools
        allow_search = "mcp_search" in planned_tools or "playwright_account_search" in planned_tools
        allow_profile = "mcp_profile" in planned_tools or "playwright_profile" in planned_tools
    profile_tool = str(strategy.get("mcp_profile_tool") or "").strip()
    profile_id_key = str(strategy.get("mcp_profile_id_key") or "user_id").strip() or "user_id"
    mcp_extra_args = strategy.get("mcp_extra_args") if isinstance(strategy.get("mcp_extra_args"), dict) else {}
    collected: List[Dict[str, Any]] = []

    use_xhs = _source_enabled(template, "xiaohongshu_search", settings.daily_hotspot_enable_xhs)
    use_google = _source_enabled(template, "google_news_rss", True)
    xhs_max = _source_max(template, "xiaohongshu_search", settings.daily_xhs_max_per_query)
    google_max = _source_max(template, "google_news_rss", settings.daily_hotspot_max_per_query)

    if use_xhs:
        planned_collected = _execute_collection_plan_steps(
            domain=domain,
            source_kind="hotspot",
            collect_limit=collect_limit,
            default_queries=queries,
            collection_plan=collection_plan,
            strategy=strategy,
            xhs_max_per_query=xhs_max,
        )
        collected = _append_unique_items(collected, planned_collected, collect_limit=collect_limit)
        mcp_status = get_mcp_readonly_status()
        if mcp_status.get("enabled") and not collection_steps:
            if allow_home:
                home_limit = max(1, min(settings.daily_xhs_home_limit, collect_limit))
                collected.extend(
                    collect_xhs_home_intel_via_mcp(
                        collect_limit=home_limit,
                        source_type="hotspot_xiaohongshu_mcp_feed",
                        source_kind="hotspot",
                        tool_name_override=str(strategy.get("mcp_feed_tool") or "").strip() or None,
                    )
                )
            remaining_for_mcp_search = max(0, collect_limit - len(collected))
            if allow_search and remaining_for_mcp_search > 0:
                collected.extend(
                    collect_xhs_search_intel_via_mcp(
                        queries=queries,
                        max_per_query=xhs_max,
                        collect_limit=remaining_for_mcp_search,
                        source_type="hotspot_xiaohongshu_mcp",
                        source_kind="hotspot",
                        tool_name_override=str(strategy.get("mcp_search_tool") or "").strip() or None,
                    )
                )
            remaining_for_profile = max(0, collect_limit - len(collected))
            if allow_profile and remaining_for_profile > 0:
                profile_query = str(strategy.get("mcp_query_default") or "").strip() or (queries[0] if queries else "日本移民")
                profile_args = _render_profile_arg_candidates(
                    query=profile_query,
                    limit=remaining_for_profile,
                    id_key=profile_id_key,
                    extra_args=mcp_extra_args,
                )
                collected.extend(
                    collect_xhs_search_intel_via_mcp(
                        queries=[profile_query],
                        max_per_query=remaining_for_profile,
                        collect_limit=remaining_for_profile,
                        source_type="hotspot_xiaohongshu_mcp_profile",
                        source_kind="hotspot",
                        tool_name_override=profile_tool or str(strategy.get("mcp_search_tool") or "").strip() or None,
                        arg_candidates_override=profile_args,
                    )
                )
        remaining_for_playwright = max(0, collect_limit - len(collected))
        if allow_search and remaining_for_playwright > 0 and not collection_steps:
            collected.extend(
                _collect_xhs_search_intel(
                    domain=domain,
                    queries=queries,
                    max_per_query=xhs_max,
                    collect_limit=remaining_for_playwright,
                    source_type="hotspot_xiaohongshu_search",
                    source_kind="hotspot",
                )
            )

    remaining = max(0, collect_limit - len(collected))
    if remaining > 0 and use_google:
        collected.extend(
            _collect_news_intel(
                domain=domain,
                queries=queries,
                max_per_query=google_max,
                collect_limit=remaining,
                source_type="hotspot_google_news",
                source_kind="hotspot",
            )
        )

    # Dedupe merged feed/search/news results by URL + leading text.
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in collected:
        source_url = str(item.get("source_url") or "").strip()
        raw_text = str(item.get("raw_text") or "").strip()
        key = f"{source_url}|{raw_text[:120]}".lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= collect_limit:
            break
    return deduped[:collect_limit]


def _collect_viewpoints(domain: Dict[str, Any], account_strategy: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    if ((account_strategy or {}).get("collection_plan") or {}).get("mode") in {"browser_ui", "xhs_cli"}:
        return []
    settings = get_settings()
    strategy = account_strategy if isinstance(account_strategy, dict) else {}
    queries = _domain_viewpoint_queries(domain, account_strategy=strategy)
    collection_plan = _normalize_collection_plan(
        strategy.get("collection_plan") if isinstance(strategy.get("collection_plan"), dict) else {}
    )
    strategy_limit = int(strategy.get("mcp_limit_default") or 0)
    collect_limit = (
        max(1, min(100, strategy_limit))
        if strategy_limit > 0
        else settings.daily_viewpoint_collect_limit
    )
    collected = _execute_collection_plan_steps(
        domain=domain,
        source_kind="viewpoint",
        collect_limit=collect_limit,
        default_queries=queries,
        collection_plan=collection_plan,
        strategy=strategy,
        xhs_max_per_query=settings.daily_viewpoint_max_per_query,
    )
    mcp_status = get_mcp_readonly_status()
    if mcp_status.get("enabled") and not (collection_plan.get("steps") if isinstance(collection_plan.get("steps"), list) else []):
        collected.extend(
            collect_xhs_search_intel_via_mcp(
                queries=queries,
                max_per_query=settings.daily_viewpoint_max_per_query,
                collect_limit=collect_limit,
                source_type="viewpoint_xiaohongshu_mcp",
                source_kind="viewpoint",
                tool_name_override=str(strategy.get("mcp_search_tool") or "").strip() or None,
            )
        )
    remaining = max(0, collect_limit - len(collected))
    if remaining > 0:
        collected.extend(
            _collect_news_intel(
                domain=domain,
                queries=queries,
                max_per_query=settings.daily_viewpoint_max_per_query,
                collect_limit=remaining,
                source_type="viewpoint_google_news",
                source_kind="viewpoint",
            )
        )
    return collected[:collect_limit]


def _supports_intelligence_account_scope(client: Client) -> bool:
    try:
        client.table("intelligence_items").select("account_id").limit(1).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def _persist_intel_items(
    client: Client,
    domain_id: str,
    items: List[Dict[str, Any]],
    *,
    account_id: str = "",
) -> int:
    inserted = 0
    normalized_account_id = str(account_id or "").strip()
    scoped = _supports_intelligence_account_scope(client)
    for item in items:
        dedupe_query = (
            client.table("intelligence_items")
            .select("id")
            .eq("domain_id", domain_id)
            .eq("source_url", item["source_url"])
            .limit(1)
        )
        if scoped:
            if normalized_account_id:
                dedupe_query = dedupe_query.eq("account_id", normalized_account_id)
            else:
                dedupe_query = dedupe_query.is_("account_id", "null")
        dedupe = dedupe_query.execute()
        if dedupe.data:
            continue

        payload = {**item, "domain_id": domain_id}
        if scoped:
            payload["account_id"] = normalized_account_id or None
        created = client.table("intelligence_items").insert(payload).execute()
        if created.data:
            inserted += 1
    return inserted


def _recent_best_patterns(client: Client, domain_id: str) -> Dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = (
        client.table("pipeline_tasks")
        .select("payload_jsonb,metrics_jsonb")
        .eq("domain_id", domain_id)
        .in_("status", ["published", "done"])
        .gte("published_at", since)
        .order("published_at", desc=True)
        .limit(50)
        .execute()
    ).data or []

    if not rows:
        return {"best_title": "", "best_hint": ""}

    ranked: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload_jsonb") if isinstance(row.get("payload_jsonb"), dict) else {}
        metrics = row.get("metrics_jsonb") if isinstance(row.get("metrics_jsonb"), dict) else {}
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not title and not body:
            continue
        score = (
            int(metrics.get("likes") or 0)
            + int(metrics.get("collects") or 0)
            + int(metrics.get("comments_count") or 0)
            + int(metrics.get("leads_generated") or metrics.get("leads") or 0) * 5
        )
        ranked.append({"title": title, "body": body, "score": score})
    if not ranked:
        return {"best_title": "", "best_hint": ""}
    ranked.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    top = ranked[0]
    return {
        "best_title": (top.get("title") or "")[:300],
        "best_hint": (top.get("body") or "")[:400],
    }


def _select_topic(intel_items: List[Dict[str, Any]]) -> str:
    if not intel_items:
        return "日本赛道今日趋势与内容规划建议"
    top_line = intel_items[0].get("raw_text", "").strip()
    if not top_line:
        return "日本赛道今日趋势与内容规划建议"
    return top_line[:120]


def _select_viewpoint(viewpoint_items: List[Dict[str, Any]]) -> str:
    if not viewpoint_items:
        return "有人认为日本移民门槛太高，普通家庭没有机会。"
    top_line = viewpoint_items[0].get("raw_text", "").strip()
    if not top_line:
        return "有人认为日本移民门槛太高，普通家庭没有机会。"
    return top_line[:240]


def _automation_policy(domain: Dict[str, Any]) -> Dict[str, Any]:
    cfg = domain.get("config_jsonb") or {}
    if isinstance(cfg.get("automation_policy"), dict):
        return cfg.get("automation_policy") or {}
    return {}


def _daily_channel_and_selector(domain: Dict[str, Any]) -> tuple[str, str]:
    settings = get_settings()
    policy = _automation_policy(domain)
    daily_channel = str(policy.get("daily_channel") or settings.daily_default_channel).strip()
    publish_selector = str(policy.get("publish_selector") or "button:has-text('发布')").strip()
    return daily_channel, publish_selector


def _pick_active_channel_account(client: Client, channel: str) -> Dict[str, Any] | None:
    try:
        res = (
            client.table("channel_accounts")
            .select(
                "id,channel,account_name,account_handle,publish_selector,login_mode,storage_state_path,"
                "user_data_dir,cookies_json,login_username,login_password,config_jsonb"
            )
            .eq("channel", channel)
            .eq("is_active", True)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        return res.data[0]
    except Exception:  # noqa: BLE001
        return None


def _pick_channel_account(client: Client, channel: str, preferred_account_id: str = "") -> Dict[str, Any] | None:
    preferred = _load_channel_account_by_id(client, preferred_account_id)
    if preferred and str(preferred.get("channel") or "").strip().lower() == str(channel or "").strip().lower():
        return preferred
    return _pick_active_channel_account(client, channel)


def _load_channel_account_by_id(client: Client, account_id: str) -> Dict[str, Any] | None:
    normalized = str(account_id or "").strip()
    if not normalized:
        return None
    try:
        res = (
            client.table("channel_accounts")
            .select(
                "id,channel,account_name,account_handle,publish_selector,login_mode,storage_state_path,"
                "user_data_dir,cookies_json,login_username,login_password,config_jsonb"
            )
            .eq("id", normalized)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        return res.data[0]
    except Exception:  # noqa: BLE001
        return None


def _inject_account_auth(payload_jsonb: Dict[str, Any], account: Dict[str, Any] | None) -> None:
    if not account:
        return
    payload_jsonb["channel_account_id"] = account.get("id")
    if account.get("storage_state_path"):
        payload_jsonb["playwright_storage_state_path"] = account.get("storage_state_path")
    if account.get("user_data_dir"):
        payload_jsonb["playwright_user_data_dir"] = account.get("user_data_dir")
    if account.get("cookies_json"):
        payload_jsonb["playwright_session_cookies_json"] = account.get("cookies_json")


def _auto_publish_enabled(domain: Dict[str, Any]) -> bool:
    # Publishing is retired even when historical domain/env switches are enabled.
    return False


def _is_missing_pipeline_table(exc: APIError) -> bool:
    raw_text = str(exc).lower()
    try:
        payload = exc.args[0] if exc.args else {}
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = str(payload.get("code") or "")
    message = str(payload.get("message") or "").lower()
    if "pipeline_tasks" not in raw_text and "pipeline_tasks" not in message:
        return False
    if code == "PGRST205":
        return True
    return "could not find the table" in raw_text or "schema cache" in raw_text


def _pipeline_schema_ready(client: Client) -> bool:
    try:
        client.table("pipeline_tasks").select("id").limit(1).execute()
        return True
    except APIError as exc:
        if _is_missing_pipeline_table(exc):
            return False
        raise


def _create_daily_pipeline_task(
    client: Client,
    domain: Dict[str, Any],
    topic: str,
    optimization: Dict[str, Any],
) -> Dict[str, Any] | None:
    daily_channel, publish_selector = _daily_channel_and_selector(domain)
    account = _pick_active_channel_account(client, daily_channel)
    account_strategy = strategy_from_account(account)
    if account and str(account.get("publish_selector") or "").strip():
        publish_selector = str(account.get("publish_selector")).strip()

    payload_jsonb: Dict[str, Any] = {"publish_selector": publish_selector}
    _inject_account_auth(payload_jsonb, account)

    payload = {
        "domain_id": domain["id"],
        "channel": daily_channel,
        "content_type": "post",
        "status": "queued",
        "stage": "intake",
        "intent_jsonb": {
            "topic": topic,
            "track": "daily_auto_ops",
            "audience_tag": "japan_core",
            "optimization_hint": optimization,
            "created_by_flow": "daily_ops",
            "account_persona_name": account_strategy.get("persona_name"),
            "account_ip_positioning": account_strategy.get("ip_positioning"),
            "account_primary_goal": account_strategy.get("primary_goal"),
        },
        "payload_jsonb": payload_jsonb,
        "created_by": "system:daily_ops",
    }
    inserted = client.table("pipeline_tasks").insert(payload).execute()
    if not inserted.data:
        return None
    return inserted.data[0]


def _create_viewpoint_pipeline_task(
    client: Client,
    domain: Dict[str, Any],
    viewpoint: str,
    optimization: Dict[str, Any],
) -> Dict[str, Any] | None:
    daily_channel, publish_selector = _daily_channel_and_selector(domain)
    account = _pick_active_channel_account(client, daily_channel)
    account_strategy = strategy_from_account(account)
    if account and str(account.get("publish_selector") or "").strip():
        publish_selector = str(account.get("publish_selector")).strip()
    payload_jsonb: Dict[str, Any] = {"publish_selector": publish_selector}
    _inject_account_auth(payload_jsonb, account)

    payload = {
        "domain_id": domain["id"],
        "channel": daily_channel,
        "content_type": "post",
        "status": "queued",
        "stage": "intake",
        "intent_jsonb": {
            "topic": f"观点拆解：{viewpoint[:100]}",
            "track": "viewpoint_expansion",
            "audience_tag": "japan_core",
            "source_opinion": viewpoint,
            "generation_mode": "counterpoint_expansion",
            "optimization_hint": optimization,
            "created_by_flow": "daily_viewpoint_ops",
            "account_persona_name": account_strategy.get("persona_name"),
            "account_ip_positioning": account_strategy.get("ip_positioning"),
            "account_primary_goal": account_strategy.get("primary_goal"),
        },
        "payload_jsonb": payload_jsonb,
        "created_by": "system:daily_ops",
    }
    inserted = client.table("pipeline_tasks").insert(payload).execute()
    if not inserted.data:
        return None
    return inserted.data[0]


def _auto_approve_pipeline_task(client: Client, task: Dict[str, Any]) -> Dict[str, Any] | None:
    task_id = task["id"]
    latest = client.table("pipeline_tasks").select("*").eq("id", task_id).limit(1).execute()
    if not latest.data:
        return None
    row = latest.data[0]
    if row.get("status") != "pending_review":
        return row

    review_jsonb = dict(row.get("review_jsonb") or {})
    review_jsonb["auto_approved_by"] = "system:daily_ops"
    review_jsonb["auto_approved_at"] = datetime.now(timezone.utc).isoformat()

    updated = (
        client.table("pipeline_tasks")
        .update(
            {
                "status": "approved",
                "stage": "approved",
                "review_jsonb": review_jsonb,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", task_id)
        .execute()
    )
    if not updated.data:
        return row
    return updated.data[0]


def _run_pipeline_lifecycle(
    client: Client,
    domain: Dict[str, Any],
    created_task: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any] | None, bool]:
    draft_result = run_pipeline_task(client, created_task["id"])
    auto_publish_enabled = _auto_publish_enabled(domain)
    publish_result: Dict[str, Any] | None = None
    if auto_publish_enabled:
        _auto_approve_pipeline_task(client, created_task)
        publish_result = run_pipeline_task(client, created_task["id"])
    return draft_result, publish_result, auto_publish_enabled


def _browser_ui_handoff(account_strategy: Dict[str, Any], *, account_id: str, domain_slug: str, source_kind: str = "hotspot") -> Dict[str, Any] | None:
    plan = account_strategy.get("collection_plan") or {}
    if plan.get("mode") == "xhs_cli":
        return {
            "status": "awaiting_browser", "reason": "local_cli_requires_account_collect",
            "message": "此账号使用本地采集，请在账号页面点击开始采集；每日流程不会回退到旧采集接口或自动发布。",
            "account_id": account_id, "domain_slug": domain_slug, "source_kind": source_kind,
            "collected": 0, "inserted": 0, "preview": [], "collection_plan": plan,
        }
    if plan.get("mode") != "browser_ui":
        return None
    return {
        "status": "awaiting_browser", "reason": "browser_ui_requires_assistance",
        "message": "请让助手在已登录的 Chrome 中搜索并打开笔记，再导入可见页面摘要；后台不会自动接管 Chrome。",
        "account_id": account_id, "domain_slug": domain_slug,
        "source_kind": source_kind, "collected": 0, "inserted": 0, "preview": [],
        "collection_plan": plan,
    }


def daily_ops_uses_cli(client: Client, domain_slug: str, preferred_account_id: str = "") -> bool:
    domain = _load_domain(client, domain_slug)
    if not domain:
        return False
    channel, _ = _daily_channel_and_selector(domain)
    strategy = strategy_from_account(_pick_channel_account(client, channel, preferred_account_id))
    return (strategy.get("collection_plan") or {}).get("mode") == "xhs_cli"


def _delegate_cli_daily(
    *, account_id: str, domain_slug: str, flow: str, run_key: str | None, triggered_by: str,
) -> Dict[str, Any]:
    settings = get_settings()
    base = {"execution_owner": "api", "account_id": account_id, "domain_slug": domain_slug, "flow": flow}
    if not account_id:
        return {**base, "status": "failed", "reason": "missing_account_id"}
    headers = {"x-user-id": triggered_by.strip() or "system:daily_ops", "x-user-role": "operator"}
    token = str(settings.internal_service_token or "").strip()
    if token:
        headers["x-internal-token"] = token
    resolved_key = run_key or f"daily:{flow}:{domain_slug}:{datetime.now(timezone.utc).date().isoformat()}"
    try:
        with httpx.Client(timeout=300, trust_env=False) as remote:
            response = remote.post(
                f"{settings.browser_bridge_api_url.rstrip('/')}/api/accounts/{account_id}/loop/run-daily",
                headers=headers, json={"domain_slug": domain_slug, "flow": flow, "run_key": resolved_key},
            )
            response.raise_for_status()
            envelope = response.json()
    except httpx.HTTPError:
        # The API may already have saved a task. Only an explicit status check can resolve this.
        return {**base, "status": "awaiting_status", "reason": "cli_loop_api_unavailable", "run_key": resolved_key}
    except (ValueError, TypeError):
        return {**base, "status": "awaiting_status", "reason": "cli_loop_invalid_response", "run_key": resolved_key}
    if not isinstance(envelope, dict):
        return {**base, "status": "awaiting_status", "reason": "cli_loop_invalid_response", "run_key": resolved_key}
    actual = envelope.get("result")
    if not isinstance(actual, dict) or not actual.get("status"):
        actual = envelope.get("loop_report")
    if not isinstance(actual, dict) or not actual.get("status"):
        actual = envelope if envelope.get("status") not in {None, "", "ok"} else None
    if actual is None:
        return {**base, "status": "awaiting_status", "reason": "cli_loop_invalid_response", "run_key": resolved_key}
    return {**envelope, **actual, **base, "run_key": resolved_key}


def run_daily_ops(
    client: Client,
    domain_slug: str = "japan_immigration",
    *,
    run_key: str | None = None,
    triggered_by: str = "system:daily_ops",
    preferred_account_id: str = "",
) -> Dict[str, Any]:
    domain = _load_domain(client, domain_slug)
    if not domain:
        return {"status": "skipped", "reason": "domain_not_found", "domain_slug": domain_slug}

    daily_channel, _ = _daily_channel_and_selector(domain)
    account = _pick_channel_account(client, daily_channel, preferred_account_id)
    account_strategy = strategy_from_account(account)
    account_id = str(account_strategy.get("account_id") or "").strip()
    if (account_strategy.get("collection_plan") or {}).get("mode") == "xhs_cli":
        return _delegate_cli_daily(account_id=account_id, domain_slug=domain_slug, flow="full",
                                   run_key=run_key, triggered_by=triggered_by)
    handoff = _browser_ui_handoff(account_strategy, account_id=account_id, domain_slug=domain_slug)
    if handoff:
        return handoff
    runtime_metadata = {
        "channel": daily_channel,
        "login_mode": str((account or {}).get("login_mode") or ""),
        "storage_state_path": (account or {}).get("storage_state_path"),
        "user_data_dir": (account or {}).get("user_data_dir"),
        "cookies_json": (account or {}).get("cookies_json"),
        "proxy_config": {"enabled": bool(get_settings().playwright_proxy_server)},
    }
    if account_id:
        upsert_account_runtime(account_id, runtime_metadata)
    resolved_run_key = run_key or f"full:{domain_slug}:{datetime.now(timezone.utc).date().isoformat()}:{uuid4().hex[:8]}"
    with account_execution_scope(account_id, action="daily_ops.full", task_id=resolved_run_key, metadata=runtime_metadata):
        run_id = _start_run_ledger(
            client,
            run_key=resolved_run_key,
            domain_id=str(domain["id"]),
            account_id=account_id,
            flow="full",
            target_posts_min=1,
        )
        _update_run_ledger(client, run_id, status="COLLECTING")
        mark_account_runtime(account_id, current_action="daily_ops.collect_hotspots")
        _safe_upsert_account_memory(
            client,
            domain_id=str(domain["id"]),
            account_id=account_id,
            account_strategy=account_strategy,
            actor=triggered_by,
        )

        hotspot_items = _collect_hotspots(domain, account_strategy=account_strategy)
        inserted_hotspots = (
            _persist_intel_items(client, domain["id"], hotspot_items, account_id=account_id)
            if hotspot_items
            else 0
        )
        _update_run_ledger(
            client,
            run_id,
            status="COLLECTED",
            result_jsonb={"hotspots_collected": len(hotspot_items), "hotspots_inserted": inserted_hotspots},
        )

        optimization = _recent_best_patterns(client, domain["id"])
        topic = _select_topic(hotspot_items)
        evidence_pack = _build_evidence_pack(
            client,
            domain=domain,
            account_strategy=account_strategy,
            target_posts_min=1,
            topic=topic,
            hotspot_items=hotspot_items,
            optimization=optimization,
        )
        _update_run_ledger(client, run_id, status="EVIDENCE_READY", evidence_pack=evidence_pack)
        _update_run_ledger(client, run_id, status="PLANNING")

        mark_account_runtime(account_id, current_action="daily_ops.create_task")
        if not _pipeline_schema_ready(client):
            _update_run_ledger(
                client,
                run_id,
                status="FAILED",
                error_message="pipeline_schema_missing",
                finished=True,
            )
            return {"status": "failed", "reason": "pipeline_schema_missing", "domain_slug": domain_slug}
        created_task = _create_daily_pipeline_task(client, domain, topic, optimization)
        if not created_task:
            _update_run_ledger(
                client,
                run_id,
                status="FAILED",
                error_message="create_pipeline_task_failed",
                finished=True,
            )
            return {"status": "failed", "reason": "create_pipeline_task_failed", "domain_slug": domain_slug}
        mark_account_runtime(account_id, current_action="daily_ops.pipeline_lifecycle", current_task_id=str(created_task.get("id") or ""))
        draft_result, publish_result, auto_publish_enabled = _run_pipeline_lifecycle(client, domain, created_task)
        created_task_id = created_task["id"]
        compat_mode = "pipeline"

        llm_output = {
            "topic": topic,
            "draft_result": draft_result,
            "publish_result": publish_result,
            "optimization_hint": optimization,
            "account_strategy": {
                "persona_name": account_strategy.get("persona_name"),
                "ip_positioning": account_strategy.get("ip_positioning"),
                "primary_goal": account_strategy.get("primary_goal"),
            },
        }
        _record_decision(
            client,
            run_id=run_id,
            pipeline_task_id=str(created_task_id) if created_task_id else None,
            evidence_pack=evidence_pack,
            llm_output=llm_output,
            summary_text=f"主题={topic}；账号={account_strategy.get('account_name') or '-'}；模式={compat_mode}",
        )
        _update_run_ledger(client, run_id, status="DRAFTED", llm_plan=llm_output)

        settings = get_settings()
        viewpoint_flow: Dict[str, Any] | None = None
        if settings.daily_viewpoint_enabled:
            mark_account_runtime(account_id, current_action="daily_ops.viewpoint_flow")
            viewpoint_flow = run_daily_viewpoint_ops(
                client,
                domain_slug=domain_slug,
                run_key=f"{resolved_run_key}:viewpoint",
                triggered_by=triggered_by,
            )

        run_status = "AWAITING_APPROVAL"
        if auto_publish_enabled:
            run_status = "PUBLISHED"
            mark_account_runtime(account_id, current_action="daily_ops.publish_result", current_task_id=str(created_task_id) if created_task_id else "")
            _update_run_ledger(client, run_id, status="PUBLISHING")
            _update_run_ledger(client, run_id, status="PUBLISHED")
            reflection_result = (
                publish_result.get("reflection_result")
                if isinstance(publish_result, dict) and isinstance(publish_result.get("reflection_result"), dict)
                else None
            )
            if reflection_result:
                mark_account_runtime(account_id, current_action="daily_ops.reflection", current_task_id=str(created_task_id) if created_task_id else "")
                _update_run_ledger(client, run_id, status="RETRO_DOING")
                _record_reflection_memory(
                    client,
                    domain_id=str(domain["id"]),
                    account_id=account_id,
                    run_id=run_id,
                    reflection_result=reflection_result,
                )
                _update_run_ledger(
                    client,
                    run_id,
                    status="RETRO_DONE",
                    retro_report=f"反思状态：{reflection_result.get('status')}",
                    result_jsonb={
                        "reflection_result": reflection_result,
                        "viewpoint_status": (viewpoint_flow or {}).get("status") if viewpoint_flow else "disabled",
                    },
                    finished=True,
                )
            else:
                _update_run_ledger(client, run_id, status=run_status, finished=True)
        else:
            _update_run_ledger(client, run_id, status=run_status, finished=True)

        client.table("audit_logs").insert(
            {
                "actor": "system:daily_ops",
                "action": "daily.ops_run",
                "target_type": "domain",
                "target_id": str(domain["id"]),
                "diff_jsonb": {
                    "domain_slug": domain_slug,
                    "run_key": resolved_run_key,
                    "run_id": run_id,
                    "topic": topic,
                    "hotspots_collected": len(hotspot_items),
                    "hotspots_inserted": inserted_hotspots,
                    "pipeline_task_id": created_task_id,
                    "account_id": account_strategy.get("account_id"),
                    "account_name": account_strategy.get("account_name"),
                    "account_persona_name": account_strategy.get("persona_name"),
                    "auto_publish_enabled": auto_publish_enabled,
                    "viewpoint_status": (viewpoint_flow or {}).get("status") if viewpoint_flow else "disabled",
                    "compat_mode": compat_mode,
                },
            }
        ).execute()

        return {
            "status": "ok",
            "flow": "full",
            "domain_slug": domain_slug,
            "run_key": resolved_run_key,
            "run_id": run_id,
            "topic": topic,
            "hotspots_collected": len(hotspot_items),
            "hotspots_inserted": inserted_hotspots,
            "account_id": account_strategy.get("account_id"),
            "account_name": account_strategy.get("account_name"),
            "account_persona_name": account_strategy.get("persona_name"),
            "pipeline_task_id": created_task_id,
            "draft_result": draft_result,
            "publish_result": publish_result,
            "auto_publish_enabled": auto_publish_enabled,
            "viewpoint_flow": viewpoint_flow,
            "compat_mode": compat_mode,
        }


def run_daily_viewpoint_ops(
    client: Client,
    domain_slug: str = "japan_immigration",
    *,
    force: bool = False,
    run_key: str | None = None,
    triggered_by: str = "system:daily_ops",
    preferred_account_id: str = "",
) -> Dict[str, Any]:
    settings = get_settings()
    if not force and not settings.daily_viewpoint_enabled:
        return {
            "status": "skipped",
            "flow": "viewpoint",
            "reason": "viewpoint_disabled",
            "domain_slug": domain_slug,
        }

    domain = _load_domain(client, domain_slug)
    if not domain:
        return {"status": "skipped", "flow": "viewpoint", "reason": "domain_not_found", "domain_slug": domain_slug}

    daily_channel, _ = _daily_channel_and_selector(domain)
    account = _pick_channel_account(client, daily_channel, preferred_account_id)
    account_strategy = strategy_from_account(account)
    account_id = str(account_strategy.get("account_id") or "").strip()
    if (account_strategy.get("collection_plan") or {}).get("mode") == "xhs_cli":
        return _delegate_cli_daily(account_id=account_id, domain_slug=domain_slug, flow="viewpoint",
                                   run_key=run_key, triggered_by=triggered_by)
    handoff = _browser_ui_handoff(account_strategy, account_id=account_id, domain_slug=domain_slug, source_kind="viewpoint")
    if handoff:
        return {**handoff, "flow": "viewpoint"}
    runtime_metadata = {
        "channel": daily_channel,
        "login_mode": str((account or {}).get("login_mode") or ""),
        "storage_state_path": (account or {}).get("storage_state_path"),
        "user_data_dir": (account or {}).get("user_data_dir"),
        "cookies_json": (account or {}).get("cookies_json"),
        "proxy_config": {"enabled": bool(get_settings().playwright_proxy_server)},
    }
    if account_id:
        upsert_account_runtime(account_id, runtime_metadata)
    resolved_run_key = run_key or f"viewpoint:{domain_slug}:{datetime.now(timezone.utc).date().isoformat()}:{uuid4().hex[:8]}"
    with account_execution_scope(account_id, action="daily_ops.viewpoint", task_id=resolved_run_key, metadata=runtime_metadata):
        run_id = _start_run_ledger(
            client,
            run_key=resolved_run_key,
            domain_id=str(domain["id"]),
            account_id=account_id,
            flow="viewpoint",
            target_posts_min=1,
        )
        _update_run_ledger(client, run_id, status="COLLECTING")
        mark_account_runtime(account_id, current_action="daily_ops.collect_viewpoints")
        _safe_upsert_account_memory(
            client,
            domain_id=str(domain["id"]),
            account_id=account_id,
            account_strategy=account_strategy,
            actor=triggered_by,
        )
        viewpoint_items = _collect_viewpoints(domain, account_strategy=account_strategy)
        inserted_viewpoints = (
            _persist_intel_items(client, domain["id"], viewpoint_items, account_id=account_id)
            if viewpoint_items
            else 0
        )
        _update_run_ledger(
            client,
            run_id,
            status="COLLECTED",
            result_jsonb={"viewpoints_collected": len(viewpoint_items), "viewpoints_inserted": inserted_viewpoints},
        )
        if not viewpoint_items:
            _update_run_ledger(
                client,
                run_id,
                status="FAILED",
                error_message="no_viewpoints_collected",
                finished=True,
            )
            return {
                "status": "skipped",
                "flow": "viewpoint",
                "reason": "no_viewpoints_collected",
                "domain_slug": domain_slug,
                "viewpoints_collected": 0,
                "viewpoints_inserted": inserted_viewpoints,
            }

        optimization = _recent_best_patterns(client, domain["id"])
        viewpoint = _select_viewpoint(viewpoint_items)
        evidence_pack = _build_evidence_pack(
            client,
            domain=domain,
            account_strategy=account_strategy,
            target_posts_min=1,
            topic=viewpoint[:120],
            hotspot_items=viewpoint_items,
            optimization=optimization,
        )
        _update_run_ledger(client, run_id, status="EVIDENCE_READY", evidence_pack=evidence_pack)
        _update_run_ledger(client, run_id, status="PLANNING")
        mark_account_runtime(account_id, current_action="daily_ops.create_viewpoint_task")
        if not _pipeline_schema_ready(client):
            _update_run_ledger(
                client,
                run_id,
                status="FAILED",
                error_message="pipeline_schema_missing",
                finished=True,
            )
            return {
                "status": "failed",
                "flow": "viewpoint",
                "reason": "pipeline_schema_missing",
                "domain_slug": domain_slug,
            }
        created_task = _create_viewpoint_pipeline_task(client, domain, viewpoint, optimization)
        if not created_task:
            _update_run_ledger(
                client,
                run_id,
                status="FAILED",
                error_message="create_viewpoint_pipeline_task_failed",
                finished=True,
            )
            return {
                "status": "failed",
                "flow": "viewpoint",
                "reason": "create_viewpoint_pipeline_task_failed",
                "domain_slug": domain_slug,
            }
        mark_account_runtime(account_id, current_action="daily_ops.viewpoint_pipeline_lifecycle", current_task_id=str(created_task.get("id") or ""))
        draft_result, publish_result, auto_publish_enabled = _run_pipeline_lifecycle(client, domain, created_task)
        created_task_id = created_task["id"]
        compat_mode = "pipeline"

        llm_output = {
            "viewpoint": viewpoint,
            "draft_result": draft_result,
            "publish_result": publish_result,
            "optimization_hint": optimization,
        }
        _record_decision(
            client,
            run_id=run_id,
            pipeline_task_id=str(created_task_id) if created_task_id else None,
            evidence_pack=evidence_pack,
            llm_output=llm_output,
            summary_text=f"观点拆解任务；账号={account_strategy.get('account_name') or '-'}；模式={compat_mode}",
        )
        _update_run_ledger(client, run_id, status="DRAFTED", llm_plan=llm_output)
        if auto_publish_enabled:
            _update_run_ledger(client, run_id, status="PUBLISHED", finished=True)
        else:
            _update_run_ledger(client, run_id, status="AWAITING_APPROVAL", finished=True)

        client.table("audit_logs").insert(
            {
                "actor": "system:daily_ops",
                "action": "daily.viewpoint_ops_run",
                "target_type": "domain",
                "target_id": str(domain["id"]),
                "diff_jsonb": {
                    "domain_slug": domain_slug,
                    "run_key": resolved_run_key,
                    "run_id": run_id,
                    "viewpoint": viewpoint,
                    "viewpoints_collected": len(viewpoint_items),
                    "viewpoints_inserted": inserted_viewpoints,
                    "pipeline_task_id": created_task_id,
                    "account_id": account_strategy.get("account_id"),
                    "account_name": account_strategy.get("account_name"),
                    "account_persona_name": account_strategy.get("persona_name"),
                    "auto_publish_enabled": auto_publish_enabled,
                    "compat_mode": compat_mode,
                },
            }
        ).execute()

        return {
            "status": "ok",
            "flow": "viewpoint",
            "domain_slug": domain_slug,
            "run_key": resolved_run_key,
            "run_id": run_id,
            "viewpoint": viewpoint,
            "viewpoints_collected": len(viewpoint_items),
            "viewpoints_inserted": inserted_viewpoints,
            "account_id": account_strategy.get("account_id"),
            "account_name": account_strategy.get("account_name"),
            "account_persona_name": account_strategy.get("persona_name"),
            "pipeline_task_id": created_task_id,
            "draft_result": draft_result,
            "publish_result": publish_result,
            "auto_publish_enabled": auto_publish_enabled,
            "compat_mode": compat_mode,
        }


def _parse_iso(iso_text: str | None) -> datetime | None:
    if not iso_text:
        return None
    normalized = iso_text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _safe_insert_report(
    client: Client,
    *,
    domain_slug: str,
    run_key: str,
    attempt: int,
    triggered_by: str,
    flow: str,
) -> str | None:
    try:
        inserted = (
            client.table("daily_ops_reports")
            .insert(
                {
                    "domain_slug": domain_slug,
                    "run_key": run_key,
                    "attempt": attempt,
                    "triggered_by": triggered_by,
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "result_jsonb": {"flow": flow},
                }
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None

    if not inserted.data:
        return None
    return str(inserted.data[0].get("id") or "")


def _safe_finish_report(
    client: Client,
    report_id: str | None,
    *,
    status: str,
    result_jsonb: Dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    if not report_id:
        return

    payload: Dict[str, Any] = {
        "status": status,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if result_jsonb is not None:
        payload["result_jsonb"] = result_jsonb
    if error_message:
        payload["error_message"] = error_message[:3000]

    try:
        client.table("daily_ops_reports").update(payload).eq("id", report_id).execute()
    except Exception:  # noqa: BLE001
        return


def _query_reports(client: Client, domain_slug: str | None, limit: int) -> List[Dict[str, Any]]:
    query = client.table("daily_ops_reports").select("*").order("started_at", desc=True).limit(limit)
    if domain_slug:
        query = query.eq("domain_slug", domain_slug)
    rows = query.execute()
    return rows.data or []


def _safe_query_reports(client: Client, domain_slug: str | None, limit: int) -> List[Dict[str, Any]]:
    try:
        return _query_reports(client, domain_slug, limit=max(1, min(limit, 200)))
    except Exception:  # noqa: BLE001
        return []


def _filter_reports_by_flow(rows: List[Dict[str, Any]], flow: str | None, limit: int) -> List[Dict[str, Any]]:
    if not flow:
        return rows[:limit]
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("result_jsonb") or {}
        if isinstance(payload, dict) and str(payload.get("flow") or "") == flow:
            filtered.append(row)
        if len(filtered) >= limit:
            break
    return filtered


def run_daily_ops_with_retry(
    client: Client,
    domain_slug: str = "japan_immigration",
    *,
    run_key: str | None = None,
    triggered_by: str = "api:manual",
    max_attempts: int | None = None,
    retry_backoff_seconds: int | None = None,
    flow: str = "full",
    preferred_account_id: str = "",
) -> Dict[str, Any]:
    settings = get_settings()
    attempts = max(1, max_attempts if max_attempts is not None else settings.daily_run_max_attempts)
    backoff_seconds = max(
        0,
        retry_backoff_seconds if retry_backoff_seconds is not None else settings.daily_retry_backoff_seconds,
    )
    resolved_flow = "viewpoint" if flow == "viewpoint" else "full"
    resolved_run_key = (
        run_key
        or f"{resolved_flow}:{domain_slug}:{datetime.now(timezone.utc).date().isoformat()}:{uuid4().hex[:8]}"
    )
    actor = triggered_by.strip() or "api:manual"
    attempt_details: List[Dict[str, Any]] = []
    last_error = ""
    try:
        cli_owned = daily_ops_uses_cli(client, domain_slug, preferred_account_id)
    except Exception:  # noqa: BLE001
        cli_owned = False

    for attempt in range(1, attempts + 1):
        report_id = _safe_insert_report(
            client,
            domain_slug=domain_slug,
            run_key=resolved_run_key,
            attempt=attempt,
            triggered_by=actor,
            flow=resolved_flow,
        )
        try:
            if resolved_flow == "viewpoint":
                result = run_daily_viewpoint_ops(
                    client,
                    domain_slug,
                    force=True,
                    run_key=resolved_run_key if cli_owned else f"{resolved_run_key}:attempt{attempt}",
                    triggered_by=actor,
                    preferred_account_id=preferred_account_id,
                )
            else:
                result = run_daily_ops(
                    client,
                    domain_slug,
                    run_key=resolved_run_key if cli_owned else f"{resolved_run_key}:attempt{attempt}",
                    triggered_by=actor,
                    preferred_account_id=preferred_account_id,
                )
            result["flow"] = resolved_flow
            current_status = str(result.get("status") or "").lower()
            detail = {"attempt": attempt, "result": result}
            attempt_details.append(detail)

            if result.get("execution_owner") == "api":
                report_status = "success" if current_status in {"ok", "pending_review", "approved"} else (
                    "failed" if current_status == "failed" else "skipped"
                )
                _safe_finish_report(client, report_id, status=report_status, result_jsonb=result)
                return {**result, "run_key": resolved_run_key, "attempt": attempt,
                        "attempts": attempts, "results": attempt_details, "result": result}

            if current_status == "awaiting_browser":
                _safe_finish_report(client, report_id, status="skipped", result_jsonb=result)
                return {**result, "flow": resolved_flow, "run_key": resolved_run_key,
                        "attempt": attempt, "attempts": attempts, "results": attempt_details, "result": result}

            if current_status == "ok":
                _safe_finish_report(client, report_id, status="success", result_jsonb=result)
                return {
                    "status": "ok",
                    "flow": resolved_flow,
                    "domain_slug": domain_slug,
                    "run_key": resolved_run_key,
                    "attempt": attempt,
                    "attempts": attempts,
                    "results": attempt_details,
                    "result": result,
                }

            if current_status == "skipped":
                _safe_finish_report(client, report_id, status="skipped", result_jsonb=result)
                return {
                    "status": "skipped",
                    "flow": resolved_flow,
                    "domain_slug": domain_slug,
                    "run_key": resolved_run_key,
                    "attempt": attempt,
                    "attempts": attempts,
                    "results": attempt_details,
                    "result": result,
                }

            reason = str(result.get("reason") or "daily_ops_failed")
            last_error = reason
            _safe_finish_report(
                client,
                report_id,
                status="failed",
                result_jsonb=result,
                error_message=reason,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            error_result = {"status": "failed", "flow": resolved_flow, "reason": "exception", "error": last_error}
            attempt_details.append({"attempt": attempt, "result": error_result})
            _safe_finish_report(
                client,
                report_id,
                status="failed",
                result_jsonb=error_result,
                error_message=last_error,
            )

        if attempt < attempts and backoff_seconds > 0:
            sleep(backoff_seconds)

    return {
        "status": "failed",
        "flow": resolved_flow,
        "domain_slug": domain_slug,
        "run_key": resolved_run_key,
        "attempts": attempts,
        "results": attempt_details,
        "last_error": last_error,
    }


def get_daily_ops_reports(
    client: Client,
    domain_slug: str | None = None,
    limit: int = 20,
    flow: str | None = None,
) -> List[Dict[str, Any]]:
    normalized_limit = max(1, min(limit, 200))
    raw_limit = min(200, normalized_limit * 3) if flow else normalized_limit
    rows = _safe_query_reports(client, domain_slug, raw_limit)
    return _filter_reports_by_flow(rows, flow, normalized_limit)


def get_daily_ops_status(
    client: Client,
    domain_slug: str = "japan_immigration",
    flow: str | None = None,
) -> Dict[str, Any]:
    settings = get_settings()
    raw_reports = _safe_query_reports(client, domain_slug, limit=200 if flow else 50)
    reports = _filter_reports_by_flow(raw_reports, flow, 50)
    latest_report = reports[0] if reports else None
    latest_success = next((row for row in reports if row.get("status") == "success"), None)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)

    total_24h = 0
    success_24h = 0
    failed_24h = 0
    skipped_24h = 0
    running_24h = 0
    for row in reports:
        started_at = _parse_iso(row.get("started_at"))
        if not started_at or started_at < since:
            continue
        total_24h += 1
        status = str(row.get("status") or "")
        if status == "success":
            success_24h += 1
        elif status == "failed":
            failed_24h += 1
        elif status == "skipped":
            skipped_24h += 1
        elif status == "running":
            running_24h += 1

    return {
        "status": "ok",
        "flow": flow or "all",
        "domain_slug": domain_slug,
        "configured_runtime": {
            "enabled": settings.daily_scheduler_enabled,
            "domain_slug": settings.daily_scheduler_domain_slug,
            "time": f"{settings.daily_scheduler_hour:02d}:{settings.daily_scheduler_minute:02d}",
            "utc_offset": settings.daily_scheduler_utc_offset,
            "max_attempts": settings.daily_run_max_attempts,
            "retry_backoff_seconds": settings.daily_retry_backoff_seconds,
            "viewpoint_enabled": settings.daily_viewpoint_enabled,
        },
        "latest_report": latest_report,
        "latest_success_at": latest_success.get("finished_at") if latest_success else None,
        "last_24h": {
            "total": total_24h,
            "success": success_24h,
            "failed": failed_24h,
            "skipped": skipped_24h,
            "running": running_24h,
        },
    }


def run_account_collection(
    client: Client,
    domain_slug: str,
    *,
    account_id: str,
    source_kind: str = "hotspot",
    triggered_by: str = "api:manual",
) -> Dict[str, Any]:
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        return {"status": "failed", "reason": "missing_account_id", "domain_slug": domain_slug}

    domain = _load_domain(client, domain_slug)
    if not domain:
        return {"status": "failed", "reason": "domain_not_found", "domain_slug": domain_slug}

    account = _load_channel_account_by_id(client, normalized_account_id)
    if not account:
        return {
            "status": "failed",
            "reason": "account_not_found",
            "domain_slug": domain_slug,
            "account_id": normalized_account_id,
        }

    resolved_kind = "viewpoint" if str(source_kind or "").strip().lower() == "viewpoint" else "hotspot"
    account_strategy = strategy_from_account(account)
    handoff = _browser_ui_handoff(account_strategy, account_id=normalized_account_id, domain_slug=domain_slug, source_kind=resolved_kind)
    if handoff:
        return handoff
    runtime_metadata = {
        "channel": str(account.get("channel") or ""),
        "login_mode": str(account.get("login_mode") or ""),
        "storage_state_path": account.get("storage_state_path"),
        "user_data_dir": account.get("user_data_dir"),
        "cookies_json": account.get("cookies_json"),
        "proxy_config": {"enabled": bool(get_settings().playwright_proxy_server)},
    }
    upsert_account_runtime(normalized_account_id, runtime_metadata)
    run_key = f"collect:{resolved_kind}:{domain_slug}:{datetime.now(timezone.utc).date().isoformat()}:{uuid4().hex[:8]}"

    with account_execution_scope(
        normalized_account_id,
        action=f"account_collection.{resolved_kind}",
        task_id=run_key,
        metadata=runtime_metadata,
    ):
        mark_account_runtime(normalized_account_id, current_action=f"account_collection.{resolved_kind}")
        _safe_upsert_account_memory(
            client,
            domain_id=str(domain["id"]),
            account_id=normalized_account_id,
            account_strategy=account_strategy,
            actor=triggered_by,
        )

        items = (
            _collect_viewpoints(domain, account_strategy=account_strategy)
            if resolved_kind == "viewpoint"
            else _collect_hotspots(domain, account_strategy=account_strategy)
        )
        inserted = _persist_intel_items(client, str(domain["id"]), items, account_id=normalized_account_id) if items else 0
        mark_account_runtime(
            normalized_account_id,
            status="idle",
            busy=False,
            current_action="",
            current_task_id="",
        )
        return {
            "status": "ok",
            "domain_slug": domain_slug,
            "account_id": normalized_account_id,
            "account_name": str(account.get("account_name") or ""),
            "source_kind": resolved_kind,
            "collected": len(items),
            "inserted": inserted,
            "collection_plan": account_strategy.get("collection_plan") if isinstance(account_strategy.get("collection_plan"), dict) else {},
            "preview": [
                {
                    "title": str(item.get("raw_text") or "").strip()[:80],
                    "source_type": str(item.get("source_type") or ""),
                    "source_url": str(item.get("source_url") or "").strip(),
                    "captured_at": str(item.get("captured_at") or "").strip(),
                }
                for item in items[:8]
            ],
        }

