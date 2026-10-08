from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from supabase import Client

from app.agents.subagents.base import build_envelope
from app.agents.subagents.analysis_agent import run_analysis_agent
from app.model_router import get_router
from app.content_branches import PROJECT_OBSERVER, resolve_content_branch, observer_strategy, project_source_ready
from app.runtime.accounts.account_strategy import strategy_from_account
from app.runtime.accounts.kb_libraries import derive_runtime_guidance, load_kb_libraries


def _resolve_domain_id(client: Client, domain_slug: str) -> str:
    try:
        rows = client.table("domains").select("id").eq("slug", domain_slug).limit(1).execute().data or []
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        return ""
    return str(rows[0].get("id") or "").strip()


def _extract_hashtags(text: str) -> List[str]:
    tags = re.findall(r"#([^\s#]{1,24})", str(text or ""))
    deduped: List[str] = []
    for tag in tags:
        token = str(tag or "").strip()
        if token and token not in deduped:
            deduped.append(token)
    return deduped[:8]


def run_copy_agent(
    client: Client,
    *,
    domain_slug: str,
    account_id: str,
    payload: Dict[str, Any] | None = None,
    triggered_by: str,
    trace_id: str,
) -> Dict[str, Any]:
    context = payload if isinstance(payload, dict) else {}
    topic = str(context.get("topic") or "日本移民")[:80]

    domain_id = _resolve_domain_id(client, domain_slug)
    if not domain_id:
        return build_envelope(agent_name="copy_agent", status="blocked", code="domain_unavailable", trace_id=trace_id)
    try:
        accounts = client.table("channel_accounts").select("id,config_jsonb").eq("id", account_id).limit(1).execute().data or []
        account_strategy = strategy_from_account(accounts[0] if accounts else {})
        branch = resolve_content_branch(context, account_strategy)
    except Exception:
        return build_envelope(agent_name="copy_agent", status="blocked", code="account_strategy_unavailable", trace_id=trace_id)
    overrides = account_strategy.get("prompt_overrides") or {}
    project = context.get("project_reference", overrides.get("project_reference")) or {}
    observer = branch == PROJECT_OBSERVER
    if observer and (domain_slug != "ai_content" or not project_source_ready(project)):
        return build_envelope(agent_name="copy_agent", status="blocked", code="project_reference_required",
                              trace_id=trace_id, message="请补齐可核对的 AI 项目资料。")
    analysis = ({"status": "success", "result": {"samples": [], "analysis_results": []},
                 "evidence_refs": [{"source_type": "project_reference", "source_ref": project["source_url"],
                                    "source_url": project["source_url"], "timestamp": project["checked_at"]}]}
                if observer else run_analysis_agent(
        client, domain_slug=domain_slug, domain_id=domain_id, account_id=account_id,
        payload=context, triggered_by=triggered_by, trace_id=trace_id,
    ))
    if analysis.get("status") != "success":
        return build_envelope(agent_name="copy_agent", status=analysis.get("status", "failed"),
                              code=analysis.get("code", "analysis_required"),
                              retryable=bool(analysis.get("retryable")), trace_id=trace_id,
                              message=analysis.get("message", ""), result={"analysis": analysis})
    insight = analysis["result"]

    libs = load_kb_libraries(
        client,
        domain_slug=domain_slug,
        account_id=account_id,
        applies_to="copy",
        stage="copy",
        direction="egress",
        entity_type="asset",
        target_agent="copy_agent",
        status="active",
    )
    guidance = derive_runtime_guidance(libs)

    intel_items: List[Dict[str, Any]] = []
    for row in insight.get("samples", [])[:8]:
        if not isinstance(row, dict):
            continue
        intel_items.append(
            {
                "raw_text": str(row.get("body") or "")[:3000],
                "source_type": "analysis_sample",
                "source_url": str(row.get("source_url") or ""),
            }
        )

    strategy = {
        "brand_tone": "专业、清晰、结论先行、避免空话",
        "audience": ["关注日本移民/签证规划的人群"],
        "pain_points": ["条件边界不清晰", "路径和时间成本不确定"],
        "hooks": [topic, "先说结论", "别先做签证再想目标"],
        "focus_keywords": [topic, "日本移民", "经营管理签证", "日本永住"],
        "forbidden_claims": guidance.get("forbidden_claims") if isinstance(guidance.get("forbidden_claims"), list) else [],
        "governance_context": {
            "rule_refs": guidance.get("rule_refs") if isinstance(guidance.get("rule_refs"), list) else [],
            "playbook_refs": guidance.get("playbook_refs") if isinstance(guidance.get("playbook_refs"), list) else [],
            "io_rule_refs": guidance.get("io_rule_refs") if isinstance(guidance.get("io_rule_refs"), list) else [],
            "method_steps": guidance.get("method_steps") if isinstance(guidance.get("method_steps"), list) else [],
        },
        "draft": "\n\n".join([
            str(guidance.get("prompt_appendix") or "").strip(),
            "真实样本的分析结果（仅供写作参考，不得编造事实）：\n"
            + json.dumps(insight.get("analysis_results", []), ensure_ascii=False),
        ]),
    }
    if observer:
        strategy = observer_strategy(strategy, project, str(context.get("content_variant") or "discovery_first"))
        intel_items = [{"raw_text": json.dumps(project, ensure_ascii=False), "source_type": "project_reference",
                        "source_url": project["source_url"], "captured_at": project["checked_at"]}]

    try:
        drafted = get_router().generate_draft(strategy=strategy, intel=intel_items, channel="xiaohongshu")
    except Exception as exc:
        return build_envelope(agent_name="copy_agent", status="retry_later", code="draft_failed",
                              retryable=True, trace_id=trace_id, message=str(exc))
    drafted = drafted if isinstance(drafted, dict) else {}
    title = str(drafted.get("title") or "").strip()
    body = str(drafted.get("body") or "").strip()
    if not title or not body:
        return build_envelope(agent_name="copy_agent", status="failed", code="incomplete_draft",
                              trace_id=trace_id, evidence_refs=analysis["evidence_refs"])

    hashtags = _extract_hashtags(body)
    if not observer and len(hashtags) < 3:
        for token in ["日本移民", "经营管理签证", "日本永住", "赴日规划"]:
            if token not in hashtags:
                hashtags.append(token)
            if len(hashtags) >= 3:
                break
    missing_tags = [tag for tag in hashtags[:6] if f"#{tag}" not in body]
    if missing_tags:
        body = f"{body}\n\n" + " ".join(f"#{tag}" for tag in missing_tags)
    cta = next((line.strip() for line in reversed(body.splitlines())
                if not line.strip().startswith("#") and any(word in line for word in ["评论", "留言", "收藏", "私信"])), "")
    if not observer and not cta:
        cta = "你目前最需要厘清哪一步？欢迎在评论区留言。"
        body += "\n\n" + cta
    from app.orchestration.pipeline_runner import _evaluate_xhs_quality
    configured_gate = dict(guidance.get("quality_gate") or {})
    if observer:
        configured_gate.update({"content_branch": branch, "project_reference": project})
    quality = _evaluate_xhs_quality(
        title=title, body=body, focus_keywords=strategy["focus_keywords"],
        forbidden_claims=strategy["forbidden_claims"], quality_gate_config=configured_gate,
    )
    configured_gate = {} if observer else (guidance.get("quality_gate") or {})
    required_checks = set()
    for fields, check in (({"title_min", "title_max"}, "title_length"),
                          ({"body_min", "body_max"}, "body_length"),
                          ({"hashtag_target"}, "hashtag_count")):
        if fields.intersection(configured_gate):
            required_checks.add(check)
    if required_checks.intersection(quality["failed_checks"]):
        quality["passed"] = False

    generated = {
        "title": title,
        "body": body,
        "hashtags": [f"#{tag}" for tag in hashtags[:6]],
        "full_post": f"{title}\n\n{body}",
        "cta": cta,
        "why_this_writing": {
            "basis": "依据真实样本拆解与当前方法库生成；内容仍需人工审核。",
            "analysis_results": insight.get("analysis_results", []),
            "method_steps": guidance.get("method_steps", []),
            "evidence_refs": analysis["evidence_refs"],
        },
        "quality_gate": quality,
        "review_required": True,
        "content_branch": branch,
        "governance": {
            "rule_refs": guidance.get("rule_refs") if isinstance(guidance.get("rule_refs"), list) else [],
            "playbook_refs": guidance.get("playbook_refs") if isinstance(guidance.get("playbook_refs"), list) else [],
            "io_rule_refs": guidance.get("io_rule_refs") if isinstance(guidance.get("io_rule_refs"), list) else [],
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    return build_envelope(
        agent_name="copy_agent",
        status="success" if quality["passed"] else "blocked",
        code="" if quality["passed"] else "content_quality_failed",
        trace_id=trace_id,
        message="copy finished" if quality["passed"] else "文案未通过质量检查，请修订后审核。",
        result=generated,
        evidence_refs=analysis["evidence_refs"],
    )
