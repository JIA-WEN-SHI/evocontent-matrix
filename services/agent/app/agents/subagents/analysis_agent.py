from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from supabase import Client

from app.agents.subagents.base import build_envelope, load_source_rows, source_evidence
from app.model_router import get_router
from app.runtime.accounts.kb_libraries import derive_runtime_guidance, load_kb_libraries


def run_analysis_agent(
    client: Client,
    *,
    domain_slug: str,
    domain_id: str,
    account_id: str,
    payload: Dict[str, Any] | None = None,
    triggered_by: str,
    trace_id: str,
) -> Dict[str, Any]:
    _ = triggered_by
    payload_map = payload if isinstance(payload, dict) else {}
    try:
        intel_rows = load_source_rows(client, table="intelligence_items", domain_id=domain_id, account_id=account_id, limit=120)
        case_rows = load_source_rows(client, table="cases", domain_id=domain_id, account_id=account_id, limit=120)
    except Exception as exc:
        return build_envelope(agent_name="analysis_agent", status="retry_later", code="source_read_failed",
                              retryable=True, trace_id=trace_id, message=str(exc))
    libs = load_kb_libraries(
        client,
        domain_slug=domain_slug,
        account_id=account_id,
        applies_to="analysis",
        stage="analysis",
        direction="egress",
        entity_type="case",
        target_agent="analysis_agent",
        status="active",
    )
    guidance = derive_runtime_guidance(libs)
    analysis_prompt = "\n\n".join(part for part in [
        str(payload_map.get("analysis_prompt") or "").strip(),
        str(guidance.get("prompt_appendix") or "").strip(),
    ] if part)
    hot_inputs: List[Dict[str, Any]] = []
    evidence: List[Dict[str, Any]] = []
    for row in case_rows[:20]:
        if not isinstance(row, dict) or not str(row.get("content") or "").strip() or not source_evidence([row], "cases"):
            continue
        hot_inputs.append(
            {
                "title": str(row.get("title") or "").strip(),
                "body": str(row.get("content") or "").strip(),
                "source_url": str(row.get("url") or "").strip(),
            }
        )
        evidence.extend(source_evidence([row], "cases"))
    if not hot_inputs:
        for row in intel_rows[:20]:
            if not isinstance(row, dict) or not str(row.get("raw_text") or "").strip() or not source_evidence([row], "intelligence_items"):
                continue
            hot_inputs.append(
                {
                    "title": str((row.get("meta_jsonb") or {}).get("title") or "" if isinstance(row.get("meta_jsonb"), dict) else "").strip(),
                    "body": str(row.get("raw_text") or "").strip(),
                    "source_url": str(row.get("source_url") or "").strip(),
                }
            )
            evidence.extend(source_evidence([row], "intelligence_items"))
    if not hot_inputs or not evidence:
        return build_envelope(agent_name="analysis_agent", status="blocked", code="missing_source_evidence",
                              trace_id=trace_id, message="请先采集可核验的正文样本。")
    try:
        analysis = get_router().analyze_hot_posts(hot_inputs, analysis_prompt=analysis_prompt)
    except Exception as exc:
        return build_envelope(agent_name="analysis_agent", status="retry_later", code="analysis_failed",
                              retryable=True, trace_id=trace_id, message=str(exc), evidence_refs=evidence)
    results = analysis.get("results") if isinstance(analysis, dict) else None
    if not isinstance(results, list) or not results or not all(isinstance(row, dict) and row for row in results) or analysis.get("status") not in {"ok", "success"}:
        return build_envelope(agent_name="analysis_agent", status="failed", code="invalid_analysis_result",
                              trace_id=trace_id, evidence_refs=evidence)
    insight = {
        "domain_slug": domain_slug,
        "sample_count": len(hot_inputs),
        "intel_count": len(intel_rows),
        "case_count": len(case_rows),
        "analysis_results": results,
        "samples": hot_inputs,
        "governance": {
            "rule_refs": guidance.get("rule_refs") if isinstance(guidance.get("rule_refs"), list) else [],
            "playbook_refs": guidance.get("playbook_refs") if isinstance(guidance.get("playbook_refs"), list) else [],
            "io_rule_refs": guidance.get("io_rule_refs") if isinstance(guidance.get("io_rule_refs"), list) else [],
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return build_envelope(
        agent_name="analysis_agent",
        status="success",
        trace_id=trace_id,
        message="analysis finished",
        result=insight,
        evidence_refs=evidence,
    )
