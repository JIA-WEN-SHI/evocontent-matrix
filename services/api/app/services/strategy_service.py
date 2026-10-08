from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import HTTPException, status
from supabase import Client

from app.models import Actor, AuditLogEntry
from app.services.audit import write_audit_log


def list_domains(client: Client) -> List[Dict[str, Any]]:
    res = (
        client.table("domains")
        .select("id,slug,name,config_jsonb,active_strategy_version_id")
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def get_domain(client: Client, slug: str) -> Dict[str, Any]:
    domain = _fetch_domain(client, slug)
    return {
        "id": domain["id"],
        "slug": domain["slug"],
        "name": domain["name"],
        "config_jsonb": domain.get("config_jsonb") or {},
        "active_strategy_version_id": domain.get("active_strategy_version_id"),
    }


def _fetch_domain(client: Client, slug: str) -> Dict[str, Any]:
    res = client.table("domains").select("*").eq("slug", slug).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")
    return res.data[0]


def list_strategy_versions(client: Client, slug: str) -> List[Dict[str, Any]]:
    domain = _fetch_domain(client, slug)
    res = (
        client.table("strategy_versions")
        .select("*")
        .eq("domain_id", domain["id"])
        .order("version", desc=True)
        .execute()
    )
    return res.data or []


def create_strategy_version(
    client: Client,
    slug: str,
    prompt_jsonb: Dict[str, Any],
    reason: str,
    actor: Actor,
) -> Dict[str, Any]:
    domain = _fetch_domain(client, slug)
    max_res = (
        client.table("strategy_versions")
        .select("version")
        .eq("domain_id", domain["id"])
        .order("version", desc=True)
        .limit(1)
        .execute()
    )
    next_version = (max_res.data[0]["version"] if max_res.data else 0) + 1

    insert_res = (
        client.table("strategy_versions")
        .insert(
            {
                "domain_id": domain["id"],
                "version": next_version,
                "prompt_jsonb": prompt_jsonb,
                "reason": reason,
                "created_by": actor.user_id,
            }
        )
        .execute()
    )
    if not insert_res.data:
        raise HTTPException(status_code=500, detail="Failed to create strategy version")
    new_version = insert_res.data[0]

    domain_config = dict(domain.get("config_jsonb") or {})
    known_keys = {
        "audience",
        "pain_points",
        "hooks",
        "forbidden_claims",
        "brand_tone",
        "channel_rules",
        "reflection_policy",
        "sub_niches",
        "persona_profiles",
        "offer_matrix",
        "policy_sources",
        "compliance_rules",
        "channel_goals",
        "locale",
    }
    for key in known_keys:
        if key in prompt_jsonb:
            domain_config[key] = prompt_jsonb[key]

    if isinstance(prompt_jsonb.get("prompt_templates"), dict):
        domain_config["prompt_templates"] = prompt_jsonb["prompt_templates"]
    else:
        domain_config["prompt_templates"] = prompt_jsonb

    client.table("domains").update(
        {
            "active_strategy_version_id": new_version["id"],
            "config_jsonb": domain_config,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", domain["id"]).execute()

    write_audit_log(
        client,
        AuditLogEntry(
            actor=actor.user_id,
            action="strategy.version_created",
            target_type="domain",
            target_id=str(domain["id"]),
            diff_jsonb={
                "new_version_id": new_version["id"],
                "version": next_version,
                "reason": reason,
            },
        ),
    )
    return new_version
