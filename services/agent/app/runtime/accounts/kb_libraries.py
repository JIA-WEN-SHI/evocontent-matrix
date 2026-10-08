from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Dict, List, Tuple

from supabase import Client


def _safe_execute(query: Any) -> List[Dict[str, Any]]:
    try:
        res = query.execute()
        return res.data or []
    except Exception:  # noqa: BLE001
        return []


def _resolve_domain_id(client: Client, domain_slug: str) -> str:
    slug = str(domain_slug or "").strip() or "japan_immigration"
    rows = _safe_execute(client.table("domains").select("id").eq("slug", slug).limit(1))
    if not rows:
        return ""
    return str(rows[0].get("id") or "").strip()


def _parse_updated_at(value: Any) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return datetime.min
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.min


def _load_scoped_rows(
    client: Client,
    *,
    table: str,
    domain_id: str,
    account_id: str = "",
    limit: int = 300,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not domain_id:
        return [], []
    base = client.table(table).select("*").eq("domain_id", domain_id).is_("deleted_at", "null").limit(max(1, min(limit, 1000)))
    global_rows = _safe_execute(base.is_("account_id", "null").order("updated_at", desc=True))
    scoped_rows: List[Dict[str, Any]] = []
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        scoped_rows = _safe_execute(
            client.table(table)
            .select("*")
            .eq("domain_id", domain_id)
            .eq("account_id", normalized_account_id)
            .is_("deleted_at", "null")
            .order("updated_at", desc=True)
            .limit(max(1, min(limit, 1000)))
        )
    return global_rows, scoped_rows


def _merge_rows_with_scope_priority(
    *,
    global_rows: List[Dict[str, Any]],
    scoped_rows: List[Dict[str, Any]],
    key_builder,
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    combined = [*global_rows, *scoped_rows]
    combined.sort(
        key=lambda row: (
            0 if str(row.get("account_id") or "").strip() else 1,  # account scoped first
            -_parse_updated_at(row.get("updated_at")).timestamp() if _parse_updated_at(row.get("updated_at")) != datetime.min else 0.0,
        )
    )
    for row in combined:
        key = key_builder(row)
        if not key:
            continue
        if key not in merged:
            merged[key] = row
    return list(merged.values())


def load_kb_libraries(
    client: Client,
    *,
    domain_slug: str,
    account_id: str = "",
    applies_to: str = "",
    stage: str = "",
    direction: str = "",
    entity_type: str = "",
    target_agent: str = "",
    status: str = "active",
) -> Dict[str, Any]:
    domain_id = _resolve_domain_id(client, domain_slug)
    if not domain_id:
        return {"domain_id": "", "rulebooks": [], "playbooks": [], "io_rules": []}

    rule_global, rule_scoped = _load_scoped_rows(
        client,
        table="kb_rulebooks",
        domain_id=domain_id,
        account_id=account_id,
    )
    rulebooks = _merge_rows_with_scope_priority(
        global_rows=rule_global,
        scoped_rows=rule_scoped,
        key_builder=lambda row: str(row.get("rule_code") or "").strip().lower(),
    )

    playbook_global, playbook_scoped = _load_scoped_rows(
        client,
        table="kb_playbooks",
        domain_id=domain_id,
        account_id=account_id,
    )
    # keep highest version first, account scoped has priority
    playbooks_all = [*playbook_global, *playbook_scoped]
    playbooks_all.sort(
        key=lambda row: (
            0 if str(row.get("account_id") or "").strip() else 1,
            -int(row.get("version") or 1),
            -_parse_updated_at(row.get("updated_at")).timestamp() if _parse_updated_at(row.get("updated_at")) != datetime.min else 0.0,
        )
    )
    playbook_map: Dict[str, Dict[str, Any]] = {}
    for row in playbooks_all:
        key = str(row.get("playbook_code") or "").strip().lower()
        if not key:
            continue
        if key not in playbook_map:
            playbook_map[key] = row
    playbooks = list(playbook_map.values())

    io_global, io_scoped = _load_scoped_rows(
        client,
        table="kb_io_rules",
        domain_id=domain_id,
        account_id=account_id,
    )
    io_rules = _merge_rows_with_scope_priority(
        global_rows=io_global,
        scoped_rows=io_scoped,
        key_builder=lambda row: "|".join(
            [
                str(row.get("rule_code") or "").strip().lower(),
                str(row.get("direction") or "").strip().lower(),
                str(row.get("entity_type") or "").strip().lower(),
            ]
        ),
    )

    normalized_status = str(status or "").strip().lower()
    if normalized_status:
        rulebooks = [row for row in rulebooks if str(row.get("status") or "").strip().lower() == normalized_status]
        playbooks = [row for row in playbooks if str(row.get("status") or "").strip().lower() == normalized_status]
        io_rules = [row for row in io_rules if str(row.get("status") or "").strip().lower() == normalized_status]

    applies = str(applies_to or "").strip().lower()
    if applies:
        filtered_rulebooks: List[Dict[str, Any]] = []
        for row in rulebooks:
            targets = row.get("applies_to") if isinstance(row.get("applies_to"), list) else []
            normalized_targets = {str(item or "").strip().lower() for item in targets}
            if not normalized_targets or applies in normalized_targets:
                filtered_rulebooks.append(row)
        rulebooks = filtered_rulebooks

    stage_name = str(stage or "").strip().lower()
    if stage_name:
        playbooks = [row for row in playbooks if str(row.get("stage") or "").strip().lower() == stage_name]

    direction_name = str(direction or "").strip().lower()
    if direction_name:
        io_rules = [row for row in io_rules if str(row.get("direction") or "").strip().lower() == direction_name]

    entity_name = str(entity_type or "").strip().lower()
    if entity_name:
        io_rules = [row for row in io_rules if str(row.get("entity_type") or "").strip().lower() == entity_name]

    target_name = str(target_agent or "").strip().lower()
    if target_name:
        io_rules = [row for row in io_rules if str(row.get("target_agent") or "").strip().lower() in {"", target_name}]

    return {
        "domain_id": domain_id,
        "rulebooks": rulebooks,
        "playbooks": playbooks,
        "io_rules": io_rules,
    }


def derive_runtime_guidance(libs: Dict[str, Any]) -> Dict[str, Any]:
    rulebooks = libs.get("rulebooks") if isinstance(libs.get("rulebooks"), list) else []
    playbooks = libs.get("playbooks") if isinstance(libs.get("playbooks"), list) else []
    io_rules = libs.get("io_rules") if isinstance(libs.get("io_rules"), list) else []

    forbidden_claims: List[str] = []
    quality_gate: Dict[str, Any] = {}
    rule_summaries: List[str] = []
    prompt_rules: List[str] = []

    for row in rulebooks[:12]:
        code = str(row.get("rule_code") or "").strip()
        name = str(row.get("rule_name") or "").strip()
        severity = str(row.get("severity") or "").strip().lower()
        rule_type = str(row.get("rule_type") or "").strip().lower()
        rule_json = row.get("rule_jsonb") if isinstance(row.get("rule_jsonb"), dict) else {}
        text = str(row.get("rule_text") or "").strip()
        if code:
            rule_summaries.append(f"{code}({severity})")
        if text:
            prompt_rules.append(f"[{code or name}] {text[:220]}")
        hard_patterns = rule_json.get("hard_block_patterns")
        if isinstance(hard_patterns, list):
            for item in hard_patterns:
                token = str(item or "").strip()
                if token and token not in forbidden_claims:
                    forbidden_claims.append(token)
        if rule_type == "quality_gate" and isinstance(rule_json, dict):
            quality_gate = {**quality_gate, **rule_json}

    method_steps: List[str] = []
    playbook_refs: List[str] = []
    for row in playbooks[:8]:
        code = str(row.get("playbook_code") or "").strip()
        version = int(row.get("version") or 1)
        if code:
            playbook_refs.append(f"{code}@v{version}")
        steps = row.get("method_steps")
        if isinstance(steps, list):
            for step in steps[:6]:
                text = str(step or "").strip()
                if text:
                    method_steps.append(text[:220])

    io_contract_lines: List[str] = []
    io_refs: List[str] = []
    for row in io_rules[:8]:
        code = str(row.get("rule_code") or "").strip()
        direction = str(row.get("direction") or "").strip().lower()
        entity = str(row.get("entity_type") or "").strip().lower()
        required_fields = row.get("required_fields") if isinstance(row.get("required_fields"), list) else []
        dedupe_keys = row.get("dedupe_keys") if isinstance(row.get("dedupe_keys"), list) else []
        if code:
            io_refs.append(f"{code}:{direction}:{entity}")
        if required_fields:
            io_contract_lines.append(f"{code} required={required_fields}")
        if dedupe_keys:
            io_contract_lines.append(f"{code} dedupe={dedupe_keys}")
        for field in ("validation_jsonb", "quality_gate_jsonb", "output_template"):
            value = row.get(field)
            if isinstance(value, dict) and value:
                io_contract_lines.append(f"{code} {field}={json.dumps(value, ensure_ascii=False)}")
        gate = row.get("quality_gate_jsonb") if isinstance(row.get("quality_gate_jsonb"), dict) else {}
        for source, lower, upper in (("title_chars", "title_min", "title_max"), ("body_chars", "body_min", "body_max")):
            bounds = gate.get(source)
            if isinstance(bounds, list) and len(bounds) == 2:
                quality_gate[lower], quality_gate[upper] = bounds
        if "hashtags_min" in gate:
            quality_gate["hashtag_target"] = gate["hashtags_min"]

    prompt_sections: List[str] = []
    if prompt_rules:
        prompt_sections.append("规则约束：\n- " + "\n- ".join(prompt_rules[:8]))
    if method_steps:
        prompt_sections.append("方法步骤：\n- " + "\n- ".join(method_steps[:10]))
    if io_contract_lines:
        prompt_sections.append("输入输出契约：\n- " + "\n- ".join(io_contract_lines[:8]))

    prompt_appendix = "\n\n".join(prompt_sections).strip()
    return {
        "forbidden_claims": forbidden_claims[:20],
        "quality_gate": quality_gate if isinstance(quality_gate, dict) else {},
        "prompt_appendix": prompt_appendix,
        "rule_refs": rule_summaries,
        "playbook_refs": playbook_refs,
        "io_rule_refs": io_refs,
        "method_steps": method_steps[:16],
    }
