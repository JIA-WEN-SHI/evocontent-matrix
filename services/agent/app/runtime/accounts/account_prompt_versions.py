from __future__ import annotations

from typing import Any, Dict

from supabase import Client


def _resolve_domain_id(client: Client, domain_slug: str) -> str:
    normalized_slug = str(domain_slug or "").strip() or "japan_immigration"
    try:
        rows = (
            client.table("domains")
            .select("id")
            .eq("slug", normalized_slug)
            .limit(1)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return ""
    return str(rows[0].get("id") or "").strip() if rows else ""


def load_active_prompt_versions(
    client: Client,
    *,
    domain_slug: str = "japan_immigration",
    account_id: str = "",
) -> Dict[str, Dict[str, Any]]:
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        return {}
    domain_id = _resolve_domain_id(client, domain_slug)
    if not domain_id:
        return {}

    try:
        rows = (
            client.table("prompt_versions")
            .select("*")
            .eq("domain_id", domain_id)
            .eq("account_id", normalized_account_id)
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(50)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        agent_name = str(row.get("agent_name") or "").strip()
        system_prompt = str(row.get("system_prompt") or "").strip()
        if not agent_name or not system_prompt:
            continue
        if agent_name not in result:
            result[agent_name] = row
    return result


def get_active_prompt_text(
    client: Client,
    *,
    domain_slug: str = "japan_immigration",
    account_id: str = "",
    agent_name: str,
) -> str:
    versions = load_active_prompt_versions(client, domain_slug=domain_slug, account_id=account_id)
    row = versions.get(str(agent_name or "").strip())
    if not isinstance(row, dict):
        return ""
    return str(row.get("system_prompt") or "").strip()
