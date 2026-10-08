from __future__ import annotations

from typing import Any, Dict

from supabase import Client

from app.agents.subagents.base import build_envelope
from app.orchestration.publish_feedback import reconcile_publish_feedback
from app.runtime.accounts.kb_libraries import derive_runtime_guidance, load_kb_libraries


def run_review_agent(
    client: Client,
    *,
    domain_slug: str,
    account_id: str,
    payload: Dict[str, Any] | None = None,
    triggered_by: str,
    trace_id: str,
) -> Dict[str, Any]:
    request_payload = payload if isinstance(payload, dict) else {}
    limit = max(1, min(50, int(request_payload.get("limit") or 20)))
    libs = load_kb_libraries(
        client,
        domain_slug=domain_slug,
        account_id=account_id,
        applies_to="review",
        stage="review",
        direction="egress",
        entity_type="review",
        target_agent="review_agent",
        status="active",
    )
    guidance = derive_runtime_guidance(libs)
    try:
        result = reconcile_publish_feedback(
            client,
            domain_slug=domain_slug,
            account_id=account_id,
            limit=limit,
        )
        if not isinstance(result, dict) or result.get("status") not in {"ok", "success"}:
            return build_envelope(agent_name="review_agent", status="retry_later", code="feedback_failed",
                                  retryable=True, trace_id=trace_id, result=result)
        enriched = dict(result)
        real_items = [item for item in result.get("items", [])
                      if isinstance(item, dict) and item.get("real_metrics") is True
                      and item.get("metrics_mode") == "real" and item.get("task_id")]
        actions = {"keep_actions": [], "adjust_actions": [], "discard_actions": []}
        history_refs = []
        for item in real_items:
            history = item.get("history_comparison") if isinstance(item.get("history_comparison"), dict) else {}
            if history.get("status") != "compared":
                continue
            history_refs.extend(history.get("evidence_refs", []))
            for name in actions:
                for action in history.get(name, []):
                    if isinstance(action, dict) and action.get("action"):
                        actions[name].append({**action, "confirmation_required": True, "auto_apply": False})
        enriched.update(actions)
        enriched["limitations"] = ["复盘建议是指标启发式判断，需人工确认因果；缺少对照证据时不建议直接废弃策略。"]
        enriched["governance"] = {
            "rule_refs": guidance.get("rule_refs") if isinstance(guidance.get("rule_refs"), list) else [],
            "playbook_refs": guidance.get("playbook_refs") if isinstance(guidance.get("playbook_refs"), list) else [],
            "io_rule_refs": guidance.get("io_rule_refs") if isinstance(guidance.get("io_rule_refs"), list) else [],
        }
        enriched["review_methods"] = guidance.get("method_steps") if isinstance(guidance.get("method_steps"), list) else []
        return build_envelope(
            agent_name="review_agent",
            status="success" if real_items else "retry_later",
            code="" if real_items else "awaiting_real_metrics",
            retryable=not bool(real_items),
            trace_id=trace_id,
            message="review finished",
            result=enriched,
            evidence_refs=[
                {
                    "source_type": "pipeline_tasks.metrics_jsonb",
                    "source_ref": str(item["task_id"]),
                    "timestamp": str(item.get("metrics_captured_at") or ""),
                }
                for item in real_items
            ] + history_refs,
        )
    except Exception as exc:  # noqa: BLE001
        return build_envelope(
            agent_name="review_agent",
            status="retry_later",
            code="review_failed",
            retryable=True,
            trace_id=trace_id,
            message=str(exc),
            result={"status": "retry_later", "error": str(exc)},
        )
