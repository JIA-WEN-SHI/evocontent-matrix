from __future__ import annotations

from typing import Any, Dict, List

from supabase import Client

_TOOL_POLICY_DEFAULT = {
    "default_action": "allow",
    "audiences": {
        "global": {
            "allow": [],
            "deny": [],
        },
        "assistant": {
            "allow": [],
            "deny": [],
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
}


def _to_tool_list(value: Any, *, limit: int = 64) -> List[str]:
    if not isinstance(value, list):
        return []
    items: List[str] = []
    seen: set[str] = set()
    for raw in value:
        text = str(raw or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def normalize_tool_policies(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    result: Dict[str, Any] = {
        "default_action": "deny",
        "audiences": {},
    }
    default_action = str(source.get("default_action") or "allow").strip().lower()
    if default_action not in {"allow", "deny"}:
        default_action = "allow"
    result["default_action"] = default_action

    raw_audiences = source.get("audiences") if isinstance(source.get("audiences"), dict) else {}
    merged_names = set(_TOOL_POLICY_DEFAULT["audiences"].keys()) | set(raw_audiences.keys())
    normalized_audiences: Dict[str, Dict[str, List[str]]] = {}
    for audience in merged_names:
        defaults = _TOOL_POLICY_DEFAULT["audiences"].get(audience) or {"allow": [], "deny": []}
        payload = raw_audiences.get(audience) if isinstance(raw_audiences.get(audience), dict) else {}
        allow = _to_tool_list(payload.get("allow"))
        deny = _to_tool_list(payload.get("deny"))
        if not allow:
            allow = list(defaults.get("allow") or [])
        if not deny:
            deny = list(defaults.get("deny") or [])
        normalized_audiences[str(audience)] = {"allow": allow, "deny": deny}
    result["audiences"] = normalized_audiences
    return result


def resolve_actor_audience(triggered_by: str) -> str:
    text = str(triggered_by or "").strip().lower()
    if "coach" in text:
        return "coach"
    if "assistant" in text:
        return "assistant"
    if text.startswith("system:") or "scheduler" in text:
        return "system"
    return "global"


def _load_domain_config(client: Client, domain_id: str) -> Dict[str, Any]:
    try:
        rows = (
            client.table("domains")
            .select("config_jsonb")
            .eq("id", domain_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    config = rows[0].get("config_jsonb")
    return config if isinstance(config, dict) else {}


def resolve_tool_policies(client: Client, *, domain_id: str) -> Dict[str, Any]:
    config = _load_domain_config(client, domain_id)
    control = config.get("orchestrator_control") if isinstance(config.get("orchestrator_control"), dict) else {}
    raw = control.get("tool_policies") if isinstance(control.get("tool_policies"), dict) else {}
    if not raw:
        raw = _TOOL_POLICY_DEFAULT
    return normalize_tool_policies(raw)


def is_action_allowed(*, policies: Dict[str, Any], audience: str, action_type: str) -> bool:
    normalized_action = str(action_type or "").strip()
    if normalized_action == "approve_and_publish_pending":
        return False
    if not normalized_action:
        return False
    raw_audiences = policies.get("audiences") if isinstance(policies.get("audiences"), dict) else {}
    raw_policy = raw_audiences.get(audience) if isinstance(raw_audiences.get(audience), dict) else {}
    allow_list = {str(item).strip() for item in (raw_policy.get("allow") if isinstance(raw_policy.get("allow"), list) else []) if str(item).strip()}
    deny_list = {str(item).strip() for item in (raw_policy.get("deny") if isinstance(raw_policy.get("deny"), list) else []) if str(item).strip()}
    if normalized_action in deny_list:
        return False
    if allow_list:
        return normalized_action in allow_list
    default_action = str(policies.get("default_action") or "allow").strip().lower()
    return default_action == "allow"


def build_policy_snapshot(
    *,
    policies: Dict[str, Any],
    audience: str,
    action_type: str,
    allowed: bool,
    source: str,
) -> Dict[str, Any]:
    raw_audiences = policies.get("audiences") if isinstance(policies.get("audiences"), dict) else {}
    raw_policy = raw_audiences.get(audience) if isinstance(raw_audiences.get(audience), dict) else {}
    return {
        "source": source,
        "audience": audience,
        "action_type": action_type,
        "allowed": bool(allowed),
        "default_action": str(policies.get("default_action") or "allow"),
        "allow": raw_policy.get("allow") if isinstance(raw_policy.get("allow"), list) else [],
        "deny": raw_policy.get("deny") if isinstance(raw_policy.get("deny"), list) else [],
    }
