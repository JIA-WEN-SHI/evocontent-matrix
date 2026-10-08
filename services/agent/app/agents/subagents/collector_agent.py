from __future__ import annotations

from typing import Any, Dict

from supabase import Client

from app.agents.subagents.base import build_envelope
from app.orchestration.daily_ops import run_account_collection
from app.execution.rpa import dispatch_collection


def run_collector_agent(
    client: Client,
    *,
    domain_slug: str,
    account_id: str,
    payload: Dict[str, Any] | None = None,
    triggered_by: str,
    trace_id: str,
) -> Dict[str, Any]:
    request_payload = payload if isinstance(payload, dict) else {}
    source_kind = str(request_payload.get("source_kind") or "hotspot").strip() or "hotspot"

    def rpa_handoff(primary_result=None):
        instruction = request_payload.get("rpa_instruction")
        result = dispatch_collection(client, domain_slug=domain_slug, account_id=account_id,
                                     instruction=instruction if isinstance(instruction, dict) else {})
        if primary_result is not None:
            result["primary_result"] = primary_result
        return build_envelope(agent_name="collector_agent", status="blocked" if result.get("manual_handoff", {}).get("required") else "retry_later",
                              code=str(result.get("code") or "rpa_awaiting_collection"), trace_id=trace_id,
                              retryable=False, message="备用采集交接结果；派发成功不代表采集完成。", result=result)

    if str(request_payload.get("execution_method") or "").lower() == "rpa":
        return rpa_handoff()
    try:
        result = run_account_collection(
            client,
            domain_slug=domain_slug,
            account_id=account_id,
            source_kind=source_kind,
            triggered_by=triggered_by,
        )
        result = result if isinstance(result, dict) else {"status": "failed", "reason": "invalid_collection_result"}
        status = str(result.get("status") or "failed").lower()
        evidence = [
            {"source_type": str(item.get("source_type") or "collected_source"),
             "source_ref": str(item.get("source_url")), "timestamp": str(item.get("captured_at") or "")}
            for item in result.get("preview", []) if isinstance(item, dict) and item.get("source_url")
        ]
        succeeded = status in {"ok", "success"} and int(result.get("collected") or 0) > 0 and bool(evidence)
        if not succeeded and isinstance(request_payload.get("rpa_instruction"), dict):
            return rpa_handoff(result)
        return build_envelope(
            agent_name="collector_agent",
            status="success" if succeeded else ("retry_later" if status == "retry_later" else "blocked"),
            code="" if succeeded else str(result.get("reason") or "no_collected_samples"),
            retryable=status == "retry_later",
            trace_id=trace_id,
            message="collector finished",
            result=result,
            evidence_refs=evidence,
        )
    except Exception as exc:  # noqa: BLE001
        if isinstance(request_payload.get("rpa_instruction"), dict):
            return rpa_handoff({"status": "failed", "error": str(exc)})
        return build_envelope(
            agent_name="collector_agent",
            status="retry_later",
            code="collector_failed",
            retryable=True,
            trace_id=trace_id,
            message=str(exc),
            result={"status": "retry_later", "error": str(exc)},
        )
