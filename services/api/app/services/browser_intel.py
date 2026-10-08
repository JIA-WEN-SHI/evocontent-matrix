from uuid import NAMESPACE_URL, uuid5

import httpx

from fastapi import HTTPException
from postgrest.exceptions import APIError
from supabase import Client

from app.models import Actor, AuditLogEntry, BrowserIntelImportRequest
from app.services.audit import write_audit_log


def intel_item_id(domain_id, account_id, source_type, source_url):
    return str(uuid5(NAMESPACE_URL, f"browser-ui:{domain_id}:{account_id}:{source_type}:{source_url}"))


def import_browser_intel(client: Client, *, account_id: str, request: BrowserIntelImportRequest, actor: Actor, capture_snapshots: dict | None = None) -> dict:
    accounts = client.table("channel_accounts").select("id,channel,is_active,config_jsonb").eq("id", account_id).limit(1).execute().data or []
    if not accounts:
        raise HTTPException(404, "账号不存在")
    account = accounts[0]
    if not account.get("is_active") or account.get("channel") != "xiaohongshu":
        raise HTTPException(409, "请选择启用中的小红书账号")
    onboarding = (account.get("config_jsonb") or {}).get("onboarding") or {}
    if onboarding.get("domain_slug") and onboarding["domain_slug"] != request.domain_slug:
        raise HTTPException(409, "采集赛道与账号当前规划不一致")
    domains = client.table("domains").select("id").eq("slug", request.domain_slug).limit(1).execute().data or []
    if not domains:
        raise HTTPException(404, "赛道不存在")
    domain_id = domains[0]["id"]
    provider = "xiaohongshu_cli" if request.collection_method == "xiaohongshu_cli" else "chrome_browser_ui"
    source_type = f"{request.source_kind}_{provider}"
    inserted, duplicates = 0, 0
    seen = set()
    ids = []
    for item in request.items:
        if item.source_url in seen:
            duplicates += 1
            continue
        seen.add(item.source_url)
        existing = (client.table("intelligence_items").select("id").eq("domain_id", domain_id)
                    .eq("account_id", account_id).eq("source_url", item.source_url).eq("source_type", source_type).limit(1).execute().data or [])
        if existing:
            duplicates += 1
            continue
        meta = {"provider": provider, "kind": request.source_kind, "query": item.query,
                "title": item.title, "captured_by": actor.user_id, "reason": request.reason,
                "evidence_scope": "cli_note_detail" if provider == "xiaohongshu_cli" else "visible_page", "metrics_verified": False}
        meta["collection_method"] = request.collection_method
        if request.model_name:
            meta["model_name"] = request.model_name
        if item.observed_metrics:
            meta["observed_metrics"] = item.observed_metrics
        if capture_snapshots and item.source_url in capture_snapshots:
            meta["browser_capture"] = capture_snapshots[item.source_url]
        item_id = intel_item_id(domain_id, account_id, source_type, item.source_url)
        payload = {"id": item_id, "domain_id": domain_id, "account_id": account_id,
                   "source_type": source_type, "source_url": item.source_url,
                   "captured_at": item.captured_at.isoformat(), "raw_text": f"{item.title}\n{item.raw_text}", "meta_jsonb": meta}
        insert_error = None
        try:
            created = client.table("intelligence_items").insert(payload).execute().data or []
        except (APIError, httpx.HTTPError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, APIError) and str(getattr(exc, "code", "")) == "23505":
                duplicates += 1
                continue
            insert_error = exc
            created = []
        if not created:
            for _ in range(2):
                try:
                    created = (client.table("intelligence_items").select("*").eq("id", item_id)
                               .eq("account_id", account_id).eq("domain_id", domain_id)
                               .eq("source_type", source_type).eq("source_url", item.source_url).limit(1).execute().data or [])
                    if created:
                        break
                except (APIError, httpx.HTTPError, TimeoutError, ConnectionError):
                    continue
        if not created:
            if insert_error:
                raise insert_error
            raise HTTPException(503, "未确认采集数据已保存，请刷新后核对")
        inserted += 1
        ids.append(item_id)
    write_audit_log(client, AuditLogEntry(actor=actor.user_id, action="account.browser_intel_imported",
                    target_type="channel_account", target_id=account_id,
                    diff_jsonb={"domain_slug": request.domain_slug, "inserted": inserted, "duplicates": duplicates,
                                "source_type": provider, "item_ids": ids, "reason": request.reason}))
    return {"status": "ok", "account_id": account_id, "domain_slug": request.domain_slug,
            "inserted": inserted, "duplicates": duplicates, "collected": len(seen), "item_ids": ids}
