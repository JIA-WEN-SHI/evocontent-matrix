from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Tuple

from openai import OpenAI
from postgrest.exceptions import APIError
from supabase import Client

from app.config import get_settings
from app.governance.execution_control import build_policy_snapshot, is_action_allowed, resolve_actor_audience, resolve_tool_policies
from app.governance.execution_ledger import record_action_run
from app.orchestration.daily_ops import run_account_collection, run_daily_ops_with_retry
from app.orchestration.pipeline_runner import run_pipeline_task
from app.orchestration.reflection import reflect_and_upgrade
from app.runtime.accounts.account_strategy import mcp_flags_from_mode, strategy_from_account
from app.tools.integrations.xhs_mcp_readonly import collect_intel_bundle_via_mcp, custom_call_readonly_via_mcp

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_COUNT_RE = re.compile(r"(\d{1,2})\s*条")
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_NO_PUBLISH_KEYWORDS = (
    "不要发布",
    "不发布",
    "先别发布",
    "禁发",
    "只出草稿",
    "仅草稿",
    "不执行发布",
    "先不发",
    "不发文",
)


def _extract_json(text: str) -> Dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:  # noqa: BLE001
        pass

    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


def _openai_client() -> OpenAI | None:
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    headers: Dict[str, str] = {}
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_name:
        headers["X-Title"] = settings.openrouter_app_name
    return OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        default_headers=headers or None,
    )


def _pipeline_schema_ready(client: Client) -> bool:
    try:
        client.table("pipeline_tasks").select("id").limit(1).execute()
        return True
    except APIError:
        return False
    except Exception:  # noqa: BLE001
        return False


def _load_domain(client: Client, domain_slug: str) -> Dict[str, Any] | None:
    res = client.table("domains").select("id,slug,name").eq("slug", domain_slug).limit(1).execute()
    if not res.data:
        return None
    return res.data[0]


def _recent_tasks_snapshot(client: Client, domain_id: str, limit: int = 6) -> List[Dict[str, Any]]:
    rows = (
        client.table("pipeline_tasks")
        .select("id,channel,status,stage,intent_jsonb,payload_jsonb,created_at,published_at")
        .eq("domain_id", domain_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return rows.data or []


def _recent_intel_sources(client: Client, domain_id: str, limit: int = 8) -> List[Dict[str, Any]]:
    rows = (
        client.table("intelligence_items")
        .select("id,source_type,source_url,captured_at")
        .eq("domain_id", domain_id)
        .order("captured_at", desc=True)
        .limit(limit)
        .execute()
    )
    return rows.data or []


def _default_selector(channel: str) -> str:
    mapping = {
        "xiaohongshu": "button:has-text('发布')",
        "wechat_mp": "button:has-text('发表')",
        "douyin": "button:has-text('发布')",
        "video": "button:has-text('发布')",
    }
    return mapping.get(channel, "button:has-text('发布')")


def _active_channel_account(client: Client, channel: str) -> Dict[str, Any] | None:
    try:
        rows = (
            client.table("channel_accounts")
            .select(
                "id,channel,account_name,account_handle,publish_selector,storage_state_path,user_data_dir,cookies_json,"
                "login_username,login_password,config_jsonb"
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


def _channel_account_by_id(client: Client, account_id: str) -> Dict[str, Any] | None:
    normalized = str(account_id or "").strip()
    if not normalized:
        return None
    try:
        rows = (
            client.table("channel_accounts")
            .select(
                "id,channel,account_name,account_handle,publish_selector,storage_state_path,user_data_dir,cookies_json,"
                "login_username,login_password,config_jsonb,is_active"
            )
            .eq("id", normalized)
            .limit(1)
            .execute()
        ).data or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


def _preferred_channel_account(client: Client, channel: str, selected_account_id: str = "") -> Dict[str, Any] | None:
    selected = _channel_account_by_id(client, selected_account_id)
    if selected and str(selected.get("channel") or "").strip().lower() == channel:
        return selected
    return _active_channel_account(client, channel)


def _inject_account_to_payload(payload: Dict[str, Any], account: Dict[str, Any] | None) -> None:
    if not account:
        return
    payload["channel_account_id"] = account.get("id")
    if account.get("publish_selector"):
        payload["publish_selector"] = account.get("publish_selector")
    if account.get("storage_state_path"):
        payload["playwright_storage_state_path"] = account.get("storage_state_path")
    if account.get("user_data_dir"):
        payload["playwright_user_data_dir"] = account.get("user_data_dir")
    if account.get("cookies_json"):
        payload["playwright_session_cookies_json"] = account.get("cookies_json")
    if account.get("login_username"):
        payload["playwright_login_username"] = account.get("login_username")
    if account.get("login_password"):
        payload["playwright_login_password"] = account.get("login_password")


def _to_utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt.astimezone(timezone.utc).isoformat()


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        return dt
    except ValueError:
        return None


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _prepare_history(
    raw_history: List[Dict[str, Any]] | None,
    *,
    max_turns: int = 12,
    max_chars: int = 3200,
) -> Tuple[List[Dict[str, str]], bool]:
    cleaned: List[Dict[str, str]] = []
    for item in raw_history or []:
        if not isinstance(item, dict):
            continue
        role_raw = str(item.get("role") or "user").strip().lower()
        role = "assistant" if role_raw == "assistant" else "user"
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        cleaned.append({"role": role, "content": content[:1200]})

    cleaned = cleaned[-max_turns:]
    selected_reversed: List[Dict[str, str]] = []
    total = 0
    truncated = False
    for item in reversed(cleaned):
        length = len(item["content"])
        if total + length > max_chars and selected_reversed:
            truncated = True
            break
        if length > max_chars:
            selected_reversed = [{"role": item["role"], "content": item["content"][:max_chars]}]
            truncated = True
            break
        selected_reversed.append(item)
        total += length

    selected = list(reversed(selected_reversed))
    if len(selected) < len(cleaned):
        truncated = True
    return selected, truncated


def _fallback_plan(message: str) -> Dict[str, Any]:
    text = (message or "").strip()
    count = 1
    count_match = _COUNT_RE.search(text)
    if count_match:
        count = max(1, min(10, int(count_match.group(1))))

    if any(token in text for token in ("发布", "发文", "安排", "排期", "定时")):
        now = datetime.now(timezone(timedelta(hours=9)))
        base_day = now.date() + timedelta(days=1) if "明天" in text else now.date()
        hh, mm = 10, 0
        time_match = _TIME_RE.search(text)
        if time_match:
            hh = max(0, min(23, int(time_match.group(1))))
            mm = max(0, min(59, int(time_match.group(2))))
        first_dt = datetime.combine(base_day, time(hour=hh, minute=mm), tzinfo=timezone(timedelta(hours=9)))
        return {
            "assistant_reply": "我已理解你的意图：按指定时间和条数创建并执行任务。",
            "requires_confirmation": False,
            "actions": [
                {
                    "type": "schedule_posts",
                    "channel": "xiaohongshu",
                    "topic": "日本赛道内容计划",
                    "count": count,
                    "first_publish_at": first_dt.isoformat(),
                    "interval_minutes": 120,
                    "run_now": True,
                }
            ],
        }

    if "MCP主页采集" in text or "MCP账号采集" in text or "账号主页采集" in text:
        query = (
            text.replace("MCP主页采集", "")
            .replace("MCP账号采集", "")
            .replace("账号主页采集", "")
            .strip()
        ) or "日本移民"
        return {
            "assistant_reply": f"我将执行只读 MCP 账号主页采集：标识“{query}”，并写入情报库。",
            "requires_confirmation": False,
            "actions": [
                {
                    "type": "collect_mcp_intel",
                    "mode": "profile",
                    "query": query,
                    "limit": 16,
                    "include_profile": True,
                    "include_home": False,
                    "include_search": False,
                    "include_detail_metrics": False,
                    "persist": True,
                }
            ],
        }

    if "MCP采集" in text:
        query = text.split("MCP采集", 1)[1].strip() if "MCP采集" in text else ""
        query = query or "日本移民"
        return {
            "assistant_reply": f"我将执行只读 MCP 采集：主页+关键词“{query}”，并写入情报库。",
            "requires_confirmation": False,
            "actions": [
                {
                    "type": "collect_mcp_intel",
                    "query": query,
                    "limit": 16,
                    "include_home": True,
                    "include_search": True,
                    "include_detail_metrics": False,
                    "persist": True,
                }
            ],
        }

    lower_text = text.lower()
    if ("mcp" in lower_text and "tool" in lower_text) or "MCP工具" in text or "MCP自定义" in text:
        tool_match = re.search(r"(?:tool|工具)\s*[:=：]\s*([a-zA-Z0-9_]+)", text)
        tool_name = tool_match.group(1).strip() if tool_match else "search_feeds"
        args_match = _JSON_OBJECT_RE.search(text)
        arguments: Dict[str, Any] = {}
        if args_match:
            try:
                parsed_args = json.loads(args_match.group(0))
                if isinstance(parsed_args, dict):
                    arguments = parsed_args
            except Exception:  # noqa: BLE001
                arguments = {}
        if not arguments and tool_name == "search_feeds":
            arguments = {"keyword": "日本移民"}
        return {
            "assistant_reply": f"我会用 MCP 自定义工具 `{tool_name}` 拉取数据，并把结果回传给你。",
            "requires_confirmation": False,
            "actions": [
                {
                    "type": "custom_mcp_call",
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "source_kind": "hotspot",
                    "limit": 10,
                }
            ],
        }

    return {
        "assistant_reply": "我可以根据聊天内容直接做决策并执行流程。你可以说：明天10:00发2条小红书。",
        "requires_confirmation": False,
        "actions": [],
    }


def _apply_message_action_guards(
    *,
    message: str,
    actions: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not actions:
        return [], []

    warnings: List[str] = []
    filtered = [item for item in actions if isinstance(item, dict)]

    normalized_message = str(message or "").strip()
    if normalized_message and any(keyword in normalized_message for keyword in _NO_PUBLISH_KEYWORDS):
        before = len(filtered)
        filtered = [
            item
            for item in filtered
            if str(item.get("type") or "").strip() not in {"approve_and_publish_pending"}
        ]
        removed = before - len(filtered)
        if removed > 0:
            warnings.append("已按你的要求禁用发布动作，仅保留非发布链路。")

    return filtered, warnings


def _llm_plan(
    *,
    message: str,
    domain_slug: str,
    snapshot: Dict[str, Any],
    conversation_history: List[Dict[str, str]],
) -> Dict[str, Any]:
    client = _openai_client()
    if not client:
        return _fallback_plan(message)

    settings = get_settings()
    prompt = {
        "domain_slug": domain_slug,
        "message": message,
        "conversation_history": conversation_history,
        "snapshot": snapshot,
        "supported_actions": [
            {
                "type": "schedule_posts",
                "description": "Create content tasks and optionally generate drafts; never publish to platforms.",
                "fields": {
                    "channel": "xiaohongshu|wechat_mp|douyin|video",
                    "topic": "string",
                    "count": "1-10",
                    "first_publish_at": "ISO8601",
                    "interval_minutes": "integer",
                    "run_now": "bool",
                },
            },
            {"type": "daily_run", "fields": {"flow": "full|viewpoint"}},
            {"type": "run_task", "fields": {"task_id": "string"}},
            {"type": "reflect", "fields": {}},
            {
                "type": "collect_mcp_intel",
                "fields": {
                    "mode": "home|keyword|hotspot|profile",
                    "query": "string",
                    "limit": "1-100",
                    "include_home": "bool",
                    "include_search": "bool",
                    "include_profile": "bool",
                    "include_detail_metrics": "bool",
                    "persist": "bool",
                    "tool_profile": {
                        "feed_tool": "string",
                        "search_tool": "string",
                        "metrics_tool": "string",
                        "profile_tool": "string",
                        "profile_id_key": "string",
                        "profile_args": "object",
                    },
                },
            },
            {
                "type": "custom_mcp_call",
                "fields": {
                    "tool_name": "string",
                    "arguments": {"k": "v"},
                    "source_kind": "hotspot|viewpoint|custom",
                    "limit": "1-100",
                },
            },
        ],
        "output_schema": {
            "assistant_reply": "string",
            "requires_confirmation": False,
            "actions": [{"type": "string"}],
        },
    }

    try:
        res = client.responses.create(
            model=settings.openai_model_draft,
            input=[
                {
                    "role": "system",
                    "content": "你是数字员工调度官。你要根据对话和当前状态自动决策并输出严格 JSON，不要输出 Markdown。",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        )
        parsed = _extract_json(res.output_text or "")
        if parsed and isinstance(parsed.get("actions"), list):
            return parsed
    except Exception:  # noqa: BLE001
        pass

    return _fallback_plan(message)


def _create_pipeline_task(
    client: Client,
    *,
    domain_id: str,
    channel: str,
    topic: str,
    scheduled_at: str | None,
    actor: str,
    selected_account_id: str = "",
) -> str | None:
    duplicate_id = _find_recent_duplicate_pipeline_task(
        client=client,
        domain_id=domain_id,
        channel=channel,
        topic=topic,
        scheduled_at=scheduled_at,
    )
    if duplicate_id:
        return duplicate_id

    account = _preferred_channel_account(client, channel, selected_account_id)
    account_strategy = strategy_from_account(account)
    payload_jsonb: Dict[str, Any] = {"publish_selector": _default_selector(channel)}
    _inject_account_to_payload(payload_jsonb, account)
    payload = {
        "domain_id": domain_id,
        "channel": channel,
        "content_type": "post",
        "status": "queued",
        "stage": "intake",
        "intent_jsonb": {
            "topic": topic,
            "track": "assistant_custom",
            "created_by_flow": "assistant_chat",
            "account_persona_name": account_strategy.get("persona_name"),
            "account_ip_positioning": account_strategy.get("ip_positioning"),
            "account_primary_goal": account_strategy.get("primary_goal"),
        },
        "payload_jsonb": payload_jsonb,
        "scheduled_at": scheduled_at,
        "created_by": actor,
    }
    res = client.table("pipeline_tasks").insert(payload).execute()
    if not res.data:
        return None
    return str(res.data[0].get("id") or "")


def _is_recent_window(created_at: str | None, *, hours: int = 12) -> bool:
    if not created_at:
        return True
    dt = _parse_datetime(created_at)
    if not dt:
        return True
    now = datetime.now(timezone.utc)
    return (now - dt.astimezone(timezone.utc)) <= timedelta(hours=hours)


def _same_schedule_bucket(lhs: str | None, rhs: str | None, *, max_minutes: int = 20) -> bool:
    left = _parse_datetime(lhs)
    right = _parse_datetime(rhs)
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs((left - right).total_seconds()) <= max_minutes * 60


def _find_recent_duplicate_pipeline_task(
    client: Client,
    *,
    domain_id: str,
    channel: str,
    topic: str,
    scheduled_at: str | None,
) -> str | None:
    try:
        rows = (
            client.table("pipeline_tasks")
            .select("id,status,created_at,scheduled_at,intent_jsonb")
            .eq("domain_id", domain_id)
            .eq("channel", channel)
            .order("created_at", desc=True)
            .limit(80)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return None

    target_topic = topic.strip().lower()
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        if status not in {"queued", "intel_ready", "drafting", "pending_review", "approved", "publishing"}:
            continue
        if not _is_recent_window(str(row.get("created_at") or ""), hours=12):
            continue
        intent = row.get("intent_jsonb") if isinstance(row.get("intent_jsonb"), dict) else {}
        existing_topic = str(intent.get("topic") or "").strip().lower()
        if existing_topic != target_topic:
            continue
        if _same_schedule_bucket(str(row.get("scheduled_at") or ""), scheduled_at, max_minutes=20):
            duplicate_id = str(row.get("id") or "").strip()
            if duplicate_id:
                return duplicate_id
    return None


def _approve_and_publish_pending(client: Client, limit: int) -> Dict[str, Any]:
    settings = get_settings()
    guard_mode = str(settings.publish_guard_mode or "manual_only").strip().lower()
    if guard_mode in {"manual_only", "disabled", "off"} or not bool(settings.allow_auto_publish):
        return {
            "status": "error",
            "error_code": "publish_guard_blocked",
            "message": "发布已被策略门禁拦截，当前仅允许人工单条放行。",
            "processed": [],
            "failed": [],
            "mode": "guarded",
        }

    capped = max(1, min(20, limit))
    processed: List[str] = []
    failed: List[str] = []

    if not _pipeline_schema_ready(client):
        return {
            "status": "error",
            "error_code": "pipeline_schema_missing",
            "message": "pipeline_tasks schema not ready; legacy task execution path has been removed.",
            "processed": [],
            "failed": [],
            "mode": "pipeline_only",
        }

    rows = (
        client.table("pipeline_tasks")
        .select("id,status")
        .eq("status", "pending_review")
        .order("created_at", desc=False)
        .limit(capped)
        .execute()
    ).data or []
    for row in rows:
        task_id = str(row.get("id") or "")
        if not task_id:
            continue
        client.table("pipeline_tasks").update({"status": "approved", "stage": "approved"}).eq("id", task_id).execute()
        result = run_pipeline_task(client, task_id)
        if str(result.get("status")) == "ok":
            processed.append(task_id)
        else:
            failed.append(task_id)
    return {"status": "ok", "processed": processed, "failed": failed, "mode": "pipeline"}


def _audit_decision(
    client: Client,
    *,
    actor: str,
    domain_id: str,
    message: str,
    plan: Dict[str, Any],
    executed: List[Dict[str, Any]],
) -> None:
    try:
        client.table("audit_logs").insert(
            {
                "actor": actor,
                "action": "assistant.chat_decision",
                "target_type": "domain",
                "target_id": domain_id,
                "diff_jsonb": {
                    "message": message,
                    "plan": plan,
                    "executed": executed,
                },
            }
        ).execute()
    except Exception:  # noqa: BLE001
        return


def _collect_task_paths(executed: List[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    for item in executed:
        if not isinstance(item, dict):
            continue
        ids = item.get("created_task_ids")
        if isinstance(ids, list):
            for task_id in ids:
                if isinstance(task_id, str) and task_id:
                    paths.append(f"pipeline_tasks/{task_id}")
        task_id = item.get("task_id")
        if isinstance(task_id, str) and task_id:
            paths.append(f"pipeline_tasks/{task_id}")
        result = item.get("result")
        if isinstance(result, dict):
            preview_rows = result.get("preview")
            if isinstance(preview_rows, list):
                for row in preview_rows[:8]:
                    if not isinstance(row, dict):
                        continue
                    source_url = str(row.get("source_url") or "").strip()
                    source_type = str(row.get("source_type") or "account_collect")
                    if source_url:
                        paths.append(f"intelligence_items [account:{source_type}] -> {source_url}")
            rows = result.get("items")
            if isinstance(rows, list):
                for row in rows[:8]:
                    if not isinstance(row, dict):
                        continue
                    source_url = str(row.get("source_url") or "").strip()
                    source_type = str(row.get("source_type") or "mcp")
                    if source_url:
                        paths.append(f"intelligence_items [mcp:{source_type}] -> {source_url}")
    return paths


def _collect_intel_paths(intel_rows: List[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    for row in intel_rows:
        row_id = str(row.get("id") or "")
        source_url = str(row.get("source_url") or "").strip()
        source_type = str(row.get("source_type") or "")
        base = f"intelligence_items/{row_id}" if row_id else "intelligence_items"
        if source_url:
            paths.append(f"{base} [{source_type}] -> {source_url}")
        else:
            paths.append(f"{base} [{source_type}]")
    return paths


def _action_label(action_type: str) -> str:
    mapping = {
        "account_collect": "账号采集",
        "daily_run": "今日运营",
        "schedule_posts": "批量创建草稿任务",
        "approve_and_publish_pending": "审批并发布",
        "run_task": "执行单任务",
        "reflect": "触发反思",
        "collect_mcp_intel": "MCP采集",
        "custom_mcp_call": "MCP自定义调用",
    }
    return mapping.get(action_type, action_type or "未知动作")


def _action_risk_level(action_type: str) -> str:
    if action_type in {"approve_and_publish_pending"}:
        return "high"
    if action_type in {"schedule_posts"}:
        return "medium"
    return "low"


def _action_step(action_type: str) -> str:
    mapping = {
        "account_collect": "collect",
        "collect_mcp_intel": "collect",
        "custom_mcp_call": "collect",
        "daily_run": "collect",
        "schedule_posts": "draft",
        "run_task": "publish",
        "approve_and_publish_pending": "publish",
        "reflect": "feedback",
    }
    return mapping.get(action_type, "collect")


def _action_scope(action_type: str) -> str:
    if action_type == "collect_mcp_intel":
        return "global"
    if action_type in {"account_collect", "run_task", "approve_and_publish_pending"}:
        return "account"
    return "global"


def _build_task_plan_preview(
    *,
    domain_slug: str,
    account_id: str,
    actions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    for idx, raw_action in enumerate(actions):
        if not isinstance(raw_action, dict):
            continue
        action_type = str(raw_action.get("type") or "").strip()
        if not action_type:
            continue
        step = _action_step(action_type)
        risk_level = _action_risk_level(action_type)
        scope = _action_scope(action_type)
        reason = str(raw_action.get("reason") or f"执行{_action_label(action_type)}，推进闭环。").strip()
        fallback: str | None = None
        if action_type == "collect_mcp_intel":
            fallback = "account_collect"
        elif action_type == "account_collect":
            fallback = "collect_mcp_intel"
        elif action_type == "approve_and_publish_pending":
            fallback = "保持待审核，人工单条放行"
        steps.append(
            {
                "step": step,
                "tool": action_type,
                "scope": scope,
                "input": {k: v for k, v in raw_action.items() if k != "type"},
                "reason": reason,
                "fallback": fallback,
                "risk_level": risk_level,
                "index": idx + 1,
            }
        )
    high_count = sum(1 for s in steps if str(s.get("risk_level")) == "high")
    medium_count = sum(1 for s in steps if str(s.get("risk_level")) == "medium")
    if high_count > 0:
        risk = "high"
    elif medium_count > 0:
        risk = "medium"
    else:
        risk = "low"
    return {
        "plan_id": f"plan_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "created_by": "orchestrator",
        "requires_confirmation": len(steps) > 0,
        "risk_level": risk,
        "status": "draft",
        "steps": steps,
    }


def _collect_required_confirmations(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    confirmations: List[Dict[str, Any]] = []
    for raw_action in actions:
        if not isinstance(raw_action, dict):
            continue
        action_type = str(raw_action.get("type") or raw_action.get("tool") or "").strip()
        if not action_type:
            continue
        risk = _action_risk_level(action_type)
        if risk == "high":
            confirmations.append(
                {
                    "type": "second_confirm",
                    "risk_level": "high",
                    "action": action_type,
                    "message": "高风险动作，需要二次确认后执行。",
                }
            )
        elif risk == "medium":
            confirmations.append(
                {
                    "type": "confirm",
                    "risk_level": "medium",
                    "action": action_type,
                    "message": "该动作会改动计划或新增任务，请确认。",
                }
            )
    return confirmations


def _is_ok_status(value: Any) -> bool:
    return str(value or "").strip().lower() in {"ok", "success"}


def _execution_success(item: Dict[str, Any]) -> bool:
    action_type = str(item.get("type") or "").strip()
    if action_type == "schedule_posts":
        return len(item.get("created_task_ids") or []) > 0
    if action_type == "account_collect":
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        return _is_ok_status(result.get("status"))
    if action_type in {"daily_run", "run_task", "reflect"}:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        return _is_ok_status(result.get("status"))
    if action_type in {"collect_mcp_intel", "custom_mcp_call"}:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        return str(result.get("status") or "").strip().lower() == "ok"
    if action_type == "approve_and_publish_pending":
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        failed = len(result.get("failed") or [])
        processed = len(result.get("processed") or [])
        return failed == 0 and processed >= 0
    return True


def _summarize_executed_item(item: Dict[str, Any]) -> str:
    action_type = str(item.get("type") or "").strip()
    if action_type == "schedule_posts":
        created = len(item.get("created_task_ids") or [])
        run_results = item.get("run_results") if isinstance(item.get("run_results"), list) else []
        if run_results:
            run_ok = sum(
                1
                for row in run_results
                if isinstance(row, dict) and _is_ok_status((row.get("status") if isinstance(row, dict) else ""))
            )
            return f"排了{created}条任务，并立即跑了{run_ok}/{len(run_results)}条"
        return f"排了{created}条待执行任务"
    if action_type == "account_collect":
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if _is_ok_status(result.get("status")):
            collected = int(result.get("collected") or 0)
            inserted = int(result.get("inserted") or 0)
            account_name = str(result.get("account_name") or "").strip()
            prefix = f"{account_name} " if account_name else ""
            return f"{prefix}账号采集拿到{collected}条，入库{inserted}条"
        reason = str(result.get("reason") or result.get("error") or result.get("status") or "未完成")
        return f"账号采集未完成（{reason}）"
    if action_type == "collect_mcp_intel":
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        status = str(result.get("status") or "").strip().lower()
        if status == "ok":
            collected = int(result.get("collected") or 0)
            inserted = int(result.get("inserted") or 0)
            return f"MCP采集拿到{collected}条，入库{inserted}条"
        reason = str(result.get("reason") or result.get("error") or status or "未完成")
        return f"MCP采集未完成（{reason}）"
    if action_type == "custom_mcp_call":
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        status = str(result.get("status") or "").strip().lower()
        tool_name = str(item.get("tool_name") or result.get("tool_name") or "tool")
        if status == "ok":
            count = int(result.get("count") or 0)
            return f"MCP工具 {tool_name} 返回{count}条"
        reason = str(result.get("reason") or result.get("error") or status or "未完成")
        return f"MCP工具 {tool_name} 未完成（{reason}）"
    if action_type == "approve_and_publish_pending":
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        processed = len(result.get("processed") or [])
        failed = len(result.get("failed") or [])
        return f"审批发布完成：成功{processed}条，失败{failed}条"
    if action_type == "run_task":
        task_id = str(item.get("task_id") or "").strip()
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        return f"任务{task_id[:8] or '-'}执行{'成功' if _is_ok_status(result.get('status')) else '失败'}"
    if action_type == "daily_run":
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        status = "完成" if _is_ok_status(result.get("status")) else "未完成"
        return f"今日运营流程已触发（{status}）"
    if action_type == "reflect":
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        version = str(result.get("new_version") or "").strip()
        return f"反思流程已触发（新版本{version if version else '未产出'}）"
    return f"{_action_label(action_type)}已执行"


def _build_execution_envelopes(executed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    envelopes: List[Dict[str, Any]] = []
    for item in executed:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("type") or "").strip()
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        success = _execution_success(item)
        error_code = None
        if isinstance(result, dict):
            error_code = str(result.get("error_code") or "").strip() or None
        step = _action_step(action_type)
        stage = step
        if step == "collect":
            stage = "collecting"
        elif step == "publish":
            stage = "publishing"
        elif step == "feedback":
            stage = "reflecting"
        envelopes.append(
            {
                "status": "ok" if success else "error",
                "step": step,
                "tool": action_type,
                "account_id": item.get("account_id"),
                "state_patch": {
                    "status": "done" if success else "failed",
                    "stage": stage,
                    "feedback_state": result.get("feedback_state") if isinstance(result, dict) else None,
                },
                "summary": _summarize_executed_item(item),
                "diagnostics": [str(x) for x in (result.get("diagnostics") or [])] if isinstance(result, dict) else [],
                "retry_action": action_type if not success else None,
                "fallback_action": "collect_mcp_intel" if action_type == "account_collect" else None,
                "error_code": error_code,
            }
        )
    return envelopes


def _friendly_warning_lines(warnings: List[str]) -> List[str]:
    mapped: List[str] = []
    for row in warnings[:3]:
        text = str(row or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if "未支持动作" in text:
            mapped.append("有一个动作目前系统还不支持，已跳过。")
        elif "缺少 task_id" in text or "task_id" in lowered:
            mapped.append("有一个动作缺少任务ID，未执行。")
        elif "需要确认" in text:
            mapped.append("这一步需要你确认后才会继续执行。")
        else:
            mapped.append(text[:80])
    return mapped


def _compact_text(text: str, limit: int = 100) -> str:
    clean = re.sub(r"\s+", " ", str(text or "").replace("\n", " ")).strip()
    if not clean:
        return ""
    return clean[:limit] + ("..." if len(clean) > limit else "")


def _reason_by_actions(action_types: List[str]) -> str:
    if "account_collect" in action_types:
        return "先补齐当前账号自己的输入层，后面的分析、草稿和发布才不会脱离这个号。"
    if "schedule_posts" in action_types:
        return "你已经明确了时间、条数和主题，我先把任务排好，避免漏发。"
    if "collect_mcp_intel" in action_types:
        return "先拿到最新外部信号，后面选题和改稿才有依据。"
    if "custom_mcp_call" in action_types:
        return "你给了自定义工具入口，我先按你的参数取回可用数据。"
    if "daily_run" in action_types:
        return "你要的是完整闭环，我先把今天的主流程跑起来。"
    if "approve_and_publish_pending" in action_types:
        return "你已经给了发布意图，我先把待审核内容往前推进。"
    if "reflect" in action_types:
        return "你在意持续优化，我先触发复盘来更新下一轮策略。"
    return "我先执行当前最关键的一步，保证流程不堵住。"


def _build_user_friendly_reply(
    *,
    plan_reply: str,
    actions: List[Dict[str, Any]],
    executed: List[Dict[str, Any]],
    warnings: List[str],
    requires_confirmation: bool,
) -> str:
    action_types = [str(item.get("type") or "").strip() for item in actions if isinstance(item, dict)]
    executed_summaries = [_summarize_executed_item(item) for item in executed if isinstance(item, dict)]
    if executed_summaries:
        line1 = f"进度更新：{'；'.join(executed_summaries[:2])}"
    elif actions:
        action_names = [_action_label(x) for x in action_types]
        action_names = [name for name in action_names if name]
        line1 = f"我先把这几步排好了：{'、'.join(action_names[:3]) or '待执行动作'}"
    else:
        line1 = "我已经看过你的指令和当前状态，随时可以开始执行。"

    reason = _reason_by_actions(action_types)
    if not action_types:
        hint = _compact_text(plan_reply, 64)
        if hint:
            reason = f"你的目标我理解为：{hint}"
    line2 = f"我这么做是因为：{reason}"

    if requires_confirmation:
        line3 = "现在进度：计划已生成，等你确认后我再执行。"
    else:
        success_count = sum(1 for item in executed if isinstance(item, dict) and _execution_success(item))
        total = len(executed)
        pending_count = max(0, total - success_count)
        warning_count = len(warnings)
        line3 = f"现在进度：已执行{total}项，顺利{success_count}项，待关注{pending_count + warning_count}项。"

    if requires_confirmation:
        next_step = "先确认计划，或者直接改时间、条数、主题。"
    elif "schedule_posts" in action_types:
        next_step = "先去“待审核”看草稿，通过后我继续推进发布。"
    elif "account_collect" in action_types:
        next_step = "你可以直接说“基于当前账号采集结果给我3个选题方向”。"
    elif "collect_mcp_intel" in action_types:
        next_step = "你可以直接说“基于这轮采集给我3个选题方向”。"
    elif "custom_mcp_call" in action_types:
        next_step = "如果结果符合预期，我可以把它并入今天的选题与草稿生产。"
    elif "reflect" in action_types:
        next_step = "你可以直接说“按新方法论重写一版标题和正文”。"
    else:
        next_step = "告诉我你的目标（例如“明天10点发2条日本移民”），我继续往下跑。"
    line4 = f"下一步你只要：{next_step}"

    warning_lines = _friendly_warning_lines(warnings)
    if warning_lines:
        return "\n".join([line1, line2, line3, line4, f"风险提醒：{'；'.join(warning_lines)}"])
    return "\n".join([line1, line2, line3, line4])


def _execute_actions(
    client: Client,
    *,
    actions: List[Dict[str, Any]],
    domain_slug: str,
    domain_id: str,
    account_id: str,
    triggered_by: str,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    executed: List[Dict[str, Any]] = []
    warnings: List[str] = []
    precollect_done = False
    audience = resolve_actor_audience(triggered_by)
    policies = resolve_tool_policies(client, domain_id=domain_id)

    def _record(
        *,
        action_type: str,
        action_input: Dict[str, Any],
        result: Dict[str, Any],
        source: str,
        allowed: bool = True,
        status_override: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        normalized_status = str(status_override or "").strip().lower()
        if not normalized_status:
            normalized_status = "success" if _is_ok_status(result.get("status")) else "failed"
        snapshot = build_policy_snapshot(
            policies=policies,
            audience=audience,
            action_type=action_type,
            allowed=allowed,
            source=source,
        )
        record_action_run(
            client,
            domain_id=domain_id,
            domain_slug=domain_slug,
            account_id=account_id,
            actor=f"assistant:{triggered_by}",
            source=source,
            action_type=action_type,
            input_jsonb=action_input,
            result_jsonb=result,
            policy_snapshot=snapshot,
            status=normalized_status,
            retryable=normalized_status in {"retry_later"},
            error_code=error_code or str(result.get("error_code") or "").strip(),
            error_message=error_message or str(result.get("error") or result.get("reason") or "").strip(),
            idempotency_key=str(action_input.get("idempotency_key") or "").strip(),
            trace_id=str(action_input.get("trace_id") or "").strip(),
        )

    for raw_action in actions:
        if not isinstance(raw_action, dict):
            continue
        action_type = str(raw_action.get("type") or "").strip()
        if not action_type:
            continue
        allowed = is_action_allowed(policies=policies, audience=audience, action_type=action_type)
        if not allowed:
            blocked_result = {"status": "blocked", "error_code": "tool_blocked_by_policy", "action_type": action_type}
            executed.append({"type": action_type, "result": blocked_result})
            warnings.append(f"{_action_label(action_type)} 被策略权限阻止（audience={audience}）。")
            _record(
                action_type=action_type,
                action_input=raw_action,
                result=blocked_result,
                source="assistant.execute",
                allowed=False,
                status_override="blocked",
                error_code="tool_blocked_by_policy",
                error_message=f"audience={audience}",
            )
            continue

        should_precollect = action_type in {"daily_run", "schedule_posts"}
        if should_precollect and not precollect_done:
            preferred_account = _preferred_channel_account(client, "xiaohongshu", account_id)
            if preferred_account:
                collect_result = run_account_collection(
                    client,
                    domain_slug,
                    account_id=str(preferred_account.get("id") or ""),
                    source_kind="hotspot",
                    triggered_by=f"assistant:{triggered_by}",
                )
                executed.append(
                    {
                        "type": "account_collect",
                        "account_id": preferred_account.get("id"),
                        "account_name": preferred_account.get("account_name"),
                        "result": collect_result,
                    }
                )
                _record(
                    action_type="account_collect",
                    action_input={
                        "account_id": preferred_account.get("id"),
                        "source_kind": "hotspot",
                        "triggered_by": triggered_by,
                    },
                    result=collect_result if isinstance(collect_result, dict) else {"status": "error", "raw": collect_result},
                    source="assistant.precollect",
                )
                if not _is_ok_status(collect_result.get("status")):
                    warnings.append(
                        f"账号采集未完成: {str(collect_result.get('reason') or collect_result.get('error') or collect_result.get('status') or 'unknown')}"
                    )
            precollect_done = True

        if action_type == "daily_run":
            flow = str(raw_action.get("flow") or "full")
            flow = "viewpoint" if flow == "viewpoint" else "full"
            result = run_daily_ops_with_retry(
                client,
                domain_slug=domain_slug,
                triggered_by=f"assistant:{triggered_by}",
                flow=flow,
                preferred_account_id=account_id,
            )
            executed.append({"type": action_type, "result": result})
            _record(
                action_type=action_type,
                action_input=raw_action,
                result=result if isinstance(result, dict) else {"status": "error", "raw": result},
                source="assistant.execute",
            )
            continue

        if action_type == "schedule_posts":
            channel = str(raw_action.get("channel") or "xiaohongshu").strip().lower()
            if channel not in {"xiaohongshu", "wechat_mp", "douyin", "video"}:
                channel = "xiaohongshu"
            topic = str(raw_action.get("topic") or "日本赛道内容计划").strip()
            count = max(1, min(10, int(raw_action.get("count") or 1)))
            first_publish_at = _parse_datetime(str(raw_action.get("first_publish_at") or "")) or datetime.now(
                timezone(timedelta(hours=9))
            )
            interval_minutes = max(10, min(720, int(raw_action.get("interval_minutes") or 120)))
            run_now_raw = raw_action.get("run_now")
            if isinstance(run_now_raw, bool):
                run_now = run_now_raw
            else:
                now_jst = datetime.now(timezone(timedelta(hours=9)))
                run_now = first_publish_at <= (now_jst + timedelta(minutes=30))
            created_ids: List[str] = []
            run_results: List[Dict[str, Any]] = []

            for idx in range(count):
                when = first_publish_at + timedelta(minutes=idx * interval_minutes)
                scheduled_at = _to_utc_iso(when)
                task_id = _create_pipeline_task(
                    client,
                    domain_id=domain_id,
                    channel=channel,
                    topic=f"{topic} #{idx + 1}" if count > 1 else topic,
                    scheduled_at=scheduled_at,
                    actor=f"assistant:{triggered_by}",
                    selected_account_id=account_id,
                )
                if not task_id:
                    warnings.append(f"创建任务失败: index={idx}")
                    continue
                created_ids.append(task_id)
                if run_now:
                    run_results.append(run_pipeline_task(client, task_id))

            executed.append({"type": action_type, "created_task_ids": created_ids, "run_results": run_results})
            run_ok = sum(
                1
                for row in run_results
                if isinstance(row, dict) and _is_ok_status(row.get("status"))
            )
            schedule_result = {
                "status": "ok" if created_ids else "failed",
                "created_count": len(created_ids),
                "run_now_count": len(run_results),
                "run_ok_count": run_ok,
            }
            _record(
                action_type=action_type,
                action_input=raw_action,
                result=schedule_result,
                source="assistant.execute",
            )
            continue

        if action_type == "approve_and_publish_pending":
            limit = int(raw_action.get("limit") or 3)
            result = _approve_and_publish_pending(client, limit=limit)
            executed.append({"type": action_type, "result": result})
            _record(
                action_type=action_type,
                action_input=raw_action,
                result=result if isinstance(result, dict) else {"status": "error", "raw": result},
                source="assistant.execute",
            )
            if str(result.get("status") or "").strip().lower() == "error":
                warnings.append(str(result.get("message") or "发布被门禁拦截"))
            continue

        if action_type == "run_task":
            task_id = str(raw_action.get("task_id") or "").strip()
            if not task_id:
                warnings.append("run_task 缺少 task_id")
            else:
                result = run_pipeline_task(client, task_id)
                executed.append({"type": action_type, "task_id": task_id, "result": result})
                _record(
                    action_type=action_type,
                    action_input=raw_action,
                    result=result if isinstance(result, dict) else {"status": "error", "raw": result},
                    source="assistant.execute",
                )
            continue

        if action_type == "reflect":
            result = reflect_and_upgrade(client, domain_slug)
            executed.append({"type": action_type, "result": result})
            _record(
                action_type=action_type,
                action_input=raw_action,
                result=result if isinstance(result, dict) else {"status": "error", "raw": result},
                source="assistant.execute",
            )
            continue

        if action_type == "collect_mcp_intel":
            account = _preferred_channel_account(client, "xiaohongshu", account_id)
            account_strategy = strategy_from_account(account)
            mode = str(raw_action.get("mode") or account_strategy.get("mcp_mode") or "hotspot").strip().lower()
            query = (
                str(raw_action.get("query") or "").strip()
                or str(account_strategy.get("mcp_query_default") or "").strip()
                or "日本移民"
            )
            limit = max(
                1,
                min(
                    100,
                    int(
                        raw_action.get("limit")
                        or account_strategy.get("mcp_limit_default")
                        or 16
                    ),
                ),
            )
            mode_flags = mcp_flags_from_mode(mode, include_home=True, include_search=True)
            include_home = _to_bool(
                raw_action.get("include_home"),
                bool(mode_flags.get("include_home", True)),
            )
            include_search = _to_bool(
                raw_action.get("include_search"),
                bool(mode_flags.get("include_search", True)),
            )
            include_profile = _to_bool(
                raw_action.get("include_profile"),
                bool(mode_flags.get("include_profile", False)),
            )
            include_detail_metrics = _to_bool(
                raw_action.get("include_detail_metrics"),
                _to_bool(account_strategy.get("include_detail_metrics"), False),
            )
            persist = _to_bool(raw_action.get("persist"), True)
            tool_profile = raw_action.get("tool_profile") if isinstance(raw_action.get("tool_profile"), dict) else {}
            if not tool_profile:
                extra_args = (
                    account_strategy.get("mcp_extra_args")
                    if isinstance(account_strategy.get("mcp_extra_args"), dict)
                    else {}
                )
                tool_profile = {
                    "feed_tool": str(account_strategy.get("mcp_feed_tool") or "").strip(),
                    "search_tool": str(account_strategy.get("mcp_search_tool") or "").strip(),
                    "metrics_tool": str(account_strategy.get("mcp_metrics_tool") or "").strip(),
                    "profile_tool": str(account_strategy.get("mcp_profile_tool") or "").strip(),
                    "profile_id_key": str(account_strategy.get("mcp_profile_id_key") or "user_id").strip() or "user_id",
                    "profile_args": extra_args.get("profile_args") if isinstance(extra_args.get("profile_args"), dict) else {},
                    "home_args": extra_args.get("home_args") if isinstance(extra_args.get("home_args"), list) else [],
                    "search_args": extra_args.get("search_args") if isinstance(extra_args.get("search_args"), list) else [],
                }
            tool_profile["include_profile"] = include_profile
            result = collect_intel_bundle_via_mcp(
                client,
                domain_slug=domain_slug,
                query=query,
                account_id=str(account_strategy.get("account_id") or "").strip(),
                limit=limit,
                include_home=include_home,
                include_search=include_search,
                include_detail_metrics=include_detail_metrics,
                persist=persist,
                tool_profile=tool_profile,
            )
            executed.append(
                {
                    "type": action_type,
                    "mode": mode,
                    "include_profile": include_profile,
                    "account_id": account_strategy.get("account_id"),
                    "account_name": account_strategy.get("account_name"),
                    "result": result,
                }
            )
            _record(
                action_type=action_type,
                action_input=raw_action,
                result=result if isinstance(result, dict) else {"status": "error", "raw": result},
                source="assistant.execute",
            )
            continue

        if action_type == "custom_mcp_call":
            tool_name = str(raw_action.get("tool_name") or "").strip()
            if not tool_name:
                warnings.append("custom_mcp_call 缺少 tool_name")
                continue
            arguments = raw_action.get("arguments") if isinstance(raw_action.get("arguments"), dict) else {}
            source_kind = str(raw_action.get("source_kind") or "hotspot").strip() or "hotspot"
            limit = max(1, min(100, int(raw_action.get("limit") or 20)))
            result = custom_call_readonly_via_mcp(
                tool_name=tool_name,
                arguments=arguments,
                source_kind=source_kind,
                limit=limit,
            )
            executed.append(
                {
                    "type": action_type,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "result": result,
                }
            )
            _record(
                action_type=action_type,
                action_input=raw_action,
                result=result if isinstance(result, dict) else {"status": "error", "raw": result},
                source="assistant.execute",
            )
            continue

        warnings.append(f"未支持动作: {action_type}")
        _record(
            action_type=action_type,
            action_input=raw_action,
            result={"status": "failed", "error_code": "unsupported_action_type"},
            source="assistant.execute",
            status_override="failed",
            error_code="unsupported_action_type",
            error_message="not_implemented",
        )
    return executed, warnings


def run_assistant_chat(
    client: Client,
    *,
    message: str,
    domain_slug: str,
    account_id: str = "",
    triggered_by: str,
    auto_execute: bool = True,
    conversation_history: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    domain = _load_domain(client, domain_slug)
    if not domain:
        return {"status": "error", "reason": "domain_not_found", "domain_slug": domain_slug}

    prepared_history, history_truncated = _prepare_history(conversation_history)
    recent_tasks = _recent_tasks_snapshot(client, domain["id"], limit=6)
    recent_intel = _recent_intel_sources(client, domain["id"], limit=6)
    active_accounts: Dict[str, Dict[str, Any]] = {}
    for channel in ["xiaohongshu", "wechat_mp", "douyin", "video"]:
        row = _preferred_channel_account(client, channel, account_id if channel == "xiaohongshu" else "")
        if not row:
            continue
        strategy = strategy_from_account(row)
        active_accounts[channel] = {
            "id": row.get("id"),
            "name": row.get("account_name"),
            "persona_name": strategy.get("persona_name"),
            "ip_positioning": strategy.get("ip_positioning"),
            "mcp_mode": strategy.get("mcp_mode"),
            "mcp_query_default": strategy.get("mcp_query_default"),
            "mcp_limit_default": strategy.get("mcp_limit_default"),
            "mcp_profile_tool": strategy.get("mcp_profile_tool"),
            "mcp_profile_id_key": strategy.get("mcp_profile_id_key"),
            "collection_plan": strategy.get("collection_plan"),
        }

    snapshot = {
        "pipeline_mode": "native" if _pipeline_schema_ready(client) else "schema_missing",
        "recent_tasks": recent_tasks,
        "recent_intel_sources": recent_intel,
        "active_accounts": active_accounts,
        "selected_account_id": str(account_id or "").strip(),
    }

    normalized_message = str(message or "")
    lower_message = normalized_message.lower()
    if ("mcp" in lower_message and "tool" in lower_message) or "MCP工具" in normalized_message or "MCP自定义" in normalized_message:
        # Keep MCP custom call deterministic, avoid LLM drifting to unrelated actions.
        plan = _fallback_plan(normalized_message)
    else:
        plan = _llm_plan(
            message=normalized_message,
            domain_slug=domain_slug,
            snapshot=snapshot,
            conversation_history=prepared_history,
        )
    base_actions = [item for item in (plan.get("actions") if isinstance(plan.get("actions"), list) else []) if isinstance(item, dict)]
    actions, message_guard_warnings = _apply_message_action_guards(
        message=normalized_message,
        actions=base_actions,
    )
    task_plan_preview = _build_task_plan_preview(
        domain_slug=domain_slug,
        account_id=str(account_id or "").strip(),
        actions=actions,
    )
    required_confirmations = _collect_required_confirmations(actions)
    settings = get_settings()
    orchestrator_v2_enabled = bool(settings.orchestrator_v2_enabled)
    requires_confirmation = bool(plan.get("requires_confirmation")) or bool(required_confirmations)
    if orchestrator_v2_enabled and len(actions) > 0:
        requires_confirmation = True

    executed: List[Dict[str, Any]] = []
    warnings: List[str] = list(message_guard_warnings)

    if auto_execute and not requires_confirmation:
        executed, warnings = _execute_actions(
            client,
            actions=actions,
            domain_slug=domain_slug,
            domain_id=str(domain["id"]),
            account_id=account_id,
            triggered_by=triggered_by,
        )
    elif requires_confirmation:
        if orchestrator_v2_enabled:
            warnings.append("V2 编排模式已启用：请先确认计划，再执行。")
        else:
            warnings.append("计划需要确认，当前未执行。")

    _audit_decision(
        client,
        actor=f"assistant:{triggered_by}",
        domain_id=str(domain["id"]),
        message=message,
        plan=plan,
        executed=executed,
    )

    source_paths = _collect_intel_paths(recent_intel) + _collect_task_paths(executed)
    execution_envelopes = _build_execution_envelopes(executed)

    assistant_reply = _build_user_friendly_reply(
        plan_reply=str(plan.get("assistant_reply") or ""),
        actions=[item for item in actions if isinstance(item, dict)],
        executed=executed,
        warnings=warnings,
        requires_confirmation=requires_confirmation,
    )

    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "assistant_reply": assistant_reply,
        "requires_confirmation": requires_confirmation,
        "task_plan_preview": task_plan_preview,
        "required_confirmations": required_confirmations,
        "plan": plan,
        "executed": executed,
        "execution_envelopes": execution_envelopes,
        "warnings": warnings,
        "source_paths": source_paths[:20],
        "context_info": {
            "history_used_turns": len(prepared_history),
            "history_truncated": history_truncated,
            "max_history_chars": 3200,
            "agent_topology": ["assistant_orchestrator", "collector_agent", "analysis_agent", "copy_agent", "review_agent"],
            "run_ids": [str(item.get("id") or "") for item in executed if isinstance(item, dict) and str(item.get("id") or "").strip()][:20],
            "sop_current_version": 0,
        },
    }


def run_task_plan_confirm(
    _client: Client,
    *,
    task_plan: Dict[str, Any],
    confirm_text: str = "",
    second_confirm_text: str = "",
    actor: str = "api:assistant",
) -> Dict[str, Any]:
    normalized_plan = task_plan if isinstance(task_plan, dict) else {}
    steps = normalized_plan.get("steps") if isinstance(normalized_plan.get("steps"), list) else []
    required = [item for item in _collect_required_confirmations(steps) if isinstance(item, dict)]
    normalized_confirm = str(confirm_text or "").strip().lower()
    if normalized_confirm not in {"确认", "确认执行", "approve", "approved", "run", "execute", "yes", "ok"}:
        return {
            "status": "error",
            "error_code": "confirm_text_invalid",
            "message": "请先确认计划后再执行。",
            "required_confirmations": required,
        }

    has_high_risk = any(str(item.get("risk_level") or "") == "high" for item in required)
    second_ok = str(second_confirm_text or "").strip().lower() in {
        "二次确认",
        "确认发布",
        "高风险确认",
        "confirm_publish",
        "approve_high_risk",
    }
    if has_high_risk and not second_ok:
        return {
            "status": "error",
            "error_code": "second_confirmation_required",
            "message": "存在高风险动作，请补充二次确认。",
            "required_confirmations": required,
        }

    approved_plan = {**normalized_plan, "status": "approved", "approved_by": actor}
    return {
        "status": "ok",
        "task_plan": approved_plan,
        "required_confirmations": required,
        "message": "计划确认通过，可执行。",
    }


def run_task_plan(
    client: Client,
    *,
    task_plan: Dict[str, Any],
    domain_slug: str,
    account_id: str = "",
    triggered_by: str = "api:task_plan",
) -> Dict[str, Any]:
    normalized_plan = task_plan if isinstance(task_plan, dict) else {}
    settings = get_settings()
    if bool(settings.orchestrator_v2_enabled):
        status = str(normalized_plan.get("status") or "").strip().lower()
        if status not in {"approved", "running", "done"}:
            return {
                "status": "error",
                "error_code": "task_plan_not_approved",
                "message": "TaskPlan 尚未确认。请先调用 confirm，再执行 run。",
                "executed": [],
                "warnings": ["task_plan_not_approved"],
                "execution_envelopes": [],
            }

    steps = normalized_plan.get("steps") if isinstance(normalized_plan.get("steps"), list) else []
    actions: List[Dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool") or "").strip()
        if not tool:
            continue
        raw_input = step.get("input")
        action_input = raw_input if isinstance(raw_input, dict) else {}
        actions.append({"type": tool, **action_input})

    if not actions:
        return {
            "status": "error",
            "error_code": "empty_task_plan",
            "message": "TaskPlan 里没有可执行动作。",
            "executed": [],
            "warnings": ["empty_task_plan"],
            "execution_envelopes": [],
        }

    domain = _load_domain(client, domain_slug)
    if not domain:
        return {
            "status": "error",
            "error_code": "domain_not_found",
            "message": f"domain_not_found: {domain_slug}",
            "executed": [],
            "warnings": ["domain_not_found"],
            "execution_envelopes": [],
        }

    executed, warnings = _execute_actions(
        client,
        actions=actions,
        domain_slug=domain_slug,
        domain_id=str(domain["id"]),
        account_id=str(account_id or "").strip(),
        triggered_by=triggered_by,
    )
    envelopes = _build_execution_envelopes(executed)
    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "task_plan": {**normalized_plan, "status": "done"},
        "executed": executed,
        "warnings": warnings,
        "execution_envelopes": envelopes,
        "source_paths": _collect_task_paths(executed)[:20],
    }
