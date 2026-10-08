"""Bounded handoff to the repository's collection-only RPA Edge contract."""

from typing import Any, Dict
from urllib.parse import urlparse

import httpx

from app.config import get_settings


COLLECTION_TASKS = {
    "collect_case": "case", "collect_asset": "asset",
    "collect_user_need": "user_need", "collect_review": "review",
}


def publish_handoff(task: Dict[str, Any]) -> Dict[str, Any]:
    payload = task.get("payload_jsonb") if isinstance(task.get("payload_jsonb"), dict) else {}
    return {
        "status": "failed", "error": "rpa_publish_contract_unavailable", "attempt": 0,
        "readiness": {"ready": False, "executor_verified": False, "supported_actions": list(COLLECTION_TASKS)},
        "manual_handoff": {
            "required": True, "task_id": str(task.get("id") or ""),
            "channel": str(task.get("channel") or ""),
            "title": str(task.get("title") or payload.get("title") or ""),
            "body": str(task.get("body") or payload.get("body") or ""),
            "reason": "现有备用执行契约只支持采集；请人工核对发布状态后接手，避免重复发布。",
        },
    }


def dispatch_collection(client: Any, *, domain_slug: str, account_id: str, instruction: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    base = str(getattr(settings, "supabase_url", "") or "").rstrip("/")
    key = str(getattr(settings, "supabase_service_role_key", "") or "")
    instruction_id = str(instruction.get("instruction_id") or "").strip()
    task_type = str(instruction.get("task_type") or "").strip()
    entity_type = str(instruction.get("entity_type") or "").strip()
    params = instruction.get("params", {})
    readiness = {"ready": False, "executor_verified": False, "contract": "rpa-dispatch", "task_type": task_type}

    def manual(code: str, *, uncertain: bool = False) -> Dict[str, Any]:
        return {"status": "manual_required", "code": code, "instruction_id": instruction_id,
                "readiness": readiness, "dispatch_uncertain": uncertain,
                "manual_handoff": {"required": True, "reason": code,
                                   "instruction_id": instruction_id,
                                   "next_action": "核对该指令的执行记录后再处理；不要换新指令编号重复派发。"}}

    if not instruction_id or not account_id or not domain_slug or not isinstance(params, dict):
        return manual("rpa_instruction_incomplete")
    if COLLECTION_TASKS.get(task_type) != entity_type:
        return manual("rpa_task_contract_unsupported")
    if urlparse(base).scheme not in {"http", "https"} or not urlparse(base).netloc or not key:
        return manual("rpa_dispatch_configuration_missing")
    try:
        domains = client.table("domains").select("id").eq("slug", domain_slug).limit(1).execute().data or []
        if not domains:
            return manual("rpa_domain_unavailable")
        templates = (client.table("rpa_task_templates").select("task_type,entity_type,octopus_flow_id")
                     .eq("domain_id", domains[0]["id"]).eq("task_type", task_type)
                     .eq("is_active", True).is_("deleted_at", "null").limit(1).execute().data or [])
        if not templates or templates[0].get("entity_type") != entity_type or not str(templates[0].get("octopus_flow_id") or "").strip():
            return manual("rpa_template_unavailable")
    except Exception:
        return manual("rpa_template_read_failed")
    readiness.update({"can_attempt_dispatch": True, "state": "configured_unverified"})
    body = {"instruction_id": instruction_id, "domain_slug": domain_slug,
            "account_id": account_id, "task_type": task_type, "entity_type": entity_type,
            "source_run_id": str(instruction.get("source_run_id") or instruction_id), "params": params}
    try:
        # One bounded request. The Edge run ledger deduplicates instruction_id.
        with httpx.Client(timeout=10.0, follow_redirects=False) as transport:
            response = transport.post(base + "/functions/v1/rpa-dispatch", json=body,
                                      headers={"authorization": f"Bearer {key}", "apikey": key})
        if response.status_code >= 300:
            return manual("rpa_dispatch_http_error", uncertain=True)
        data = response.json()
        if not isinstance(data, dict):
            return manual("rpa_dispatch_invalid_response", uncertain=True)
        if data.get("status") == "ok" and data.get("run_id") and data.get("octopus_run_id"):
            readiness.update({"ready": True, "executor_verified": True, "state": "dispatch_acknowledged"})
            return {"status": "dispatched", "run_id": str(data["run_id"]),
                    "instruction_id": instruction_id, "readiness": readiness,
                    "awaiting_callback": True, "collection_complete": False}
        if data.get("status") == "exists" and isinstance(data.get("run"), dict):
            run = data["run"]
            if run.get("id") and run.get("instruction_id") == instruction_id:
                return {"status": "existing_run", "run_id": str(run["id"]),
                        "run_status": str(run.get("status") or "unknown"),
                        "instruction_id": instruction_id, "readiness": readiness,
                        "collection_complete": False, "manual_handoff": {
                            "required": run.get("status") in {"failed", "rejected", "canceled", "partial_failed"},
                            "next_action": "检查已有运行及入库结果，不重复派发。"}}
        return manual("rpa_dispatch_invalid_response", uncertain=True)
    except (httpx.HTTPError, ValueError):
        return manual("rpa_dispatch_uncertain", uncertain=True)
