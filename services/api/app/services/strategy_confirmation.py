"""Explicit, account-scoped SOP confirmation with resumable persisted stages."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from supabase import Client

from app.models import Actor, AuditLogEntry
from app.services.account_service import (
    _append_sop_snapshot,
    _normalize_collection_plan,
    _normalize_feedback_plan,
)
from app.services.audit import write_audit_log
from app.services.task_observations import _feed_id, _time, normalize_values


_TARGETS = {"collection_plan", "feedback_plan", "publish_preferences"}
_RECEIPTS = "strategy_confirmation_receipts"
_BLOCKED = re.compile(r"\bhold\b|insufficient[ _-]evidence|competitor[ _-]only|\u8bc1\u636e\u4e0d\u8db3|\u4ec5\u7ade\u54c1", re.IGNORECASE)
_PUBLISH_FIELDS = {"next_publish_slot_local", "min_action_gap_seconds", "max_action_gap_seconds", "updated_by_feedback"}
_PUBLISHED_STATUSES = {"published", "metrics_ready", "reflecting", "reflection_failed", "done"}
_ACTUAL_PROVIDERS = {"manual", "xiaohongshu_cli", "xhs_mcp_readonly"}
_COUNTER_ALIASES = {"impressions": "views", "comments": "comments_count", "follows": "followers_delta"}
_COUNTERS = {"views", "impressions", "likes", "collects", "comments", "comments_count", "shares", "follows", "followers_delta"}
_MAX_CONFIG_FILTER_BYTES = 2400


class _ConfirmationAudit(AuditLogEntry):
    # A stable primary key also deduplicates audit writes whose response was lost.
    id: str


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(client: Client, table: str, **filters: Any) -> dict:
    query = client.table(table).select("*")
    for key, value in filters.items():
        query = query.eq(key, value)
    rows = query.limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, f"{table} record not found")
    return rows[0]


def _config(account: dict) -> dict:
    return deepcopy(account.get("config_jsonb") or {})


def _version(config: dict) -> int:
    snapshots = config.get("sop_snapshots") or []
    return int(config.get("sop_latest_version") or (snapshots[-1].get("version", 0) if snapshots else 0))


def _source_fingerprint(item: dict) -> str:
    source = {key: item.get(key) for key in ("id", "domain_id", "account_id", "type", "title", "content", "tags")}
    return hashlib.sha256(_json(source).encode("utf-8")).hexdigest()


def _normalize(target: str, value: dict) -> dict:
    if target == "collection_plan":
        return _normalize_collection_plan(deepcopy(value))
    if target == "feedback_plan":
        return _normalize_feedback_plan(deepcopy(value))
    return deepcopy(value)


def _save_config(client: Client, account: dict, config: dict) -> dict:
    original = _config(account)
    serialized = _json(original)
    small_config = len(quote(serialized, safe="")) <= _MAX_CONFIG_FILTER_BYTES
    timestamp = account.get("updated_at")
    try:
        _time(timestamp)
        usable_timestamp = True
    except HTTPException:
        usable_timestamp = False
    if not small_config and not usable_timestamp:
        raise HTTPException(409, "large account configuration requires a valid updated_at concurrency guard")
    query = (client.table("channel_accounts")
             .update({"config_jsonb": config, "updated_at": _now()})
             .eq("id", account["id"]))
    if usable_timestamp:
        query = query.eq("updated_at", timestamp)
    if small_config:
        query = query.eq("config_jsonb", serialized)
    else:
        # Account writers advance updated_at; a short version guard keeps SOP CAS off oversized URLs.
        version = original.get("sop_latest_version")
        if version is None:
            query = query.is_("config_jsonb->>sop_latest_version", "null")
        else:
            text = str(version)
            if not re.fullmatch(r"\d{1,20}", text):
                raise HTTPException(409, "invalid SOP concurrency version")
            query = query.eq("config_jsonb->>sop_latest_version", text)
    rows = query.execute().data or []
    if not rows:
        raise HTTPException(409, "account configuration changed during confirmation; retry after checking state")
    saved = _get(client, "channel_accounts", id=account["id"])
    if _config(saved) != config:
        raise HTTPException(409, "account configuration persistence could not be confirmed")
    return saved


def _body(item: dict) -> dict:
    content = item.get("content")
    if isinstance(content, dict):
        return deepcopy(content)
    try:
        parsed = json.loads(str(content or ""))
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validate_executable(item: dict, body: dict, proposal: dict | None) -> None:
    # The persisted item always participates, even when a caller supplies a generic proposal.
    def blocked(value: Any) -> bool:
        if isinstance(value, str):
            return bool(_BLOCKED.search(value))
        if isinstance(value, list):
            return any(blocked(entry) for entry in value)
        if isinstance(value, dict):
            if any(value.get(key) is True for key in ("hold", "insufficient_evidence", "competitor_only")):
                return True
            if any(value.get(key) is False for key in ("executable", "has_own_account_evidence")):
                return True
            return any(blocked(entry) for entry in value.values())
        return False
    content = body if body else item.get("content")
    if blocked([item.get("title"), item.get("tags"), content, proposal]):
        raise HTTPException(422, "HOLD or insufficient own-account evidence cannot be applied")


def _proposal(body: dict, target: str, injected: dict | None) -> dict:
    chosen = body.get("proposal")
    if chosen is None:
        changes = body.get("proposed_changes")
        if isinstance(changes, list):
            chosen = next((entry for entry in changes[:3] if isinstance(entry, dict) and entry.get("target") == target), None)
        elif body.get("target") == target:
            chosen = body
    if chosen is not None and injected is not None:
        if not isinstance(injected, dict) or any(chosen.get(key) != injected.get(key) for key in ("target", "before", "after")):
            raise HTTPException(409, "injected proposal differs from the persisted suggestion")
    if chosen is None:
        chosen = injected
    if not isinstance(chosen, dict) or chosen.get("target") != target:
        raise HTTPException(422, "no executable proposal for the requested target")
    if not isinstance(chosen.get("before"), dict) or not isinstance(chosen.get("after"), dict):
        raise HTTPException(422, "proposal requires explicit before and after objects")
    if len(_json(chosen).encode("utf-8")) > 65536:
        raise HTTPException(422, "proposal exceeds confirmation size limit")
    return deepcopy(chosen)


def _reject_invalidated(value: dict) -> None:
    for key in ("invalidated", "invalidated_at", "publication_invalidated"):
        marker = value.get(key)
        if marker is not None and marker is not False:
            raise HTTPException(422, "publication or observation evidence has been invalidated")


def _actual_values(value: Any) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(422, "saved observation requires actual counter values")
    normalized = {}
    for key in _COUNTERS.intersection(value):
        if value[key] is None:
            continue
        name = _COUNTER_ALIASES.get(key, key)
        values = normalize_values({name: value[key]})
        if name in normalized and normalized[name] != values[name]:
            raise HTTPException(422, "observation counter aliases disagree")
        normalized.update(values)
    if not normalized:
        raise HTTPException(422, "evidence task has no saved own-account observations")
    return normalized


def _actual_provenance(provenance: Any) -> None:
    def actual(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip()) and not re.search(r"synthetic|preview|simulat|fabricated|mock", value, re.IGNORECASE)
        if isinstance(value, dict):
            if value.get("synthetic") is True or value.get("actual") is False or value.get("is_real") is False:
                return False
            strings = [entry for entry in value.values() if isinstance(entry, (str, dict))]
            return bool(strings) and all(actual(entry) for entry in strings)
        return False
    if not actual(provenance):
        raise HTTPException(422, "observation requires explicit actual provenance")


def _actual_observation(task: dict, *, account_id: str, domain_slug: str, metrics: dict | None = None) -> dict:
    if task.get("status") not in _PUBLISHED_STATUSES or task.get("channel") not in {None, "", "xiaohongshu"}:
        raise HTTPException(422, "evidence task is not an actual published note")
    publish = task.get("publish_jsonb")
    if not isinstance(publish, dict):
        raise HTTPException(422, "registered publication metadata is required")
    _reject_invalidated(task)
    _reject_invalidated(publish)
    registration = publish.get("manual_publication")
    if (not isinstance(registration, dict) or registration.get("confirmed") is not True
            or registration.get("ownership") != "user_confirmed"
            or registration.get("account_id") != account_id or registration.get("domain_slug") != domain_slug):
        raise HTTPException(422, "own-account manual publication registration is required")
    _reject_invalidated(registration)
    published = _time(task.get("published_at"))
    now = datetime.now(timezone.utc)
    if published > now or ("published_at" in publish and _time(publish["published_at"]) != published):
        raise HTTPException(422, "publication time is future or inconsistent")
    identity = publish.get("identity")
    if not isinstance(identity, dict):
        raise HTTPException(422, "registered publication identity is required")
    note_ids = []
    for key in ("feed_id", "remote_post_id"):
        if identity.get(key):
            note_ids.append(_feed_id("https://www.xiaohongshu.com/explore/" + str(identity[key])))
    if identity.get("published_url"):
        note_ids.append(_feed_id(identity["published_url"]))
    if not note_ids or len(set(note_ids)) != 1:
        raise HTTPException(422, "registered publication identities disagree or are missing")

    current_metrics = task.get("metrics_jsonb")
    if isinstance(current_metrics, dict):
        _reject_invalidated(current_metrics)
    if metrics is None:
        metrics = current_metrics
    if not isinstance(metrics, dict) or metrics.get("metrics_mode") != "real":
        raise HTTPException(422, "only explicitly real observations can confirm SOP")
    _reject_invalidated(metrics)
    if "observation" in metrics:
        observation = metrics["observation"]
        if not isinstance(observation, dict):
            raise HTTPException(422, "saved observation metadata is invalid")
    else:
        # Older actual metadata is usable only when source, provider, time and provenance survive.
        observation = {
            "values": metrics.get("post_metrics"),
            "observed_at": metrics.get("observed_at") or metrics.get("post_metrics_synced_at"),
            "provider": metrics.get("provider") or metrics.get("mcp_source"),
            "source_ref": metrics.get("source_ref") or metrics.get("post_metrics_source_ref"),
            "provenance": metrics.get("provenance"),
        }
        if observation["provenance"] is None and observation["provider"] == "xhs_mcp_readonly" and metrics.get("mcp_tool"):
            observation["provenance"] = {"tool_name": metrics["mcp_tool"]}
    _reject_invalidated(observation)
    for metadata in (metrics, observation):
        for key, expected in (("account_id", account_id), ("domain_id", task["domain_id"]), ("domain_slug", domain_slug)):
            if key in metadata and metadata[key] != expected:
                raise HTTPException(422, "saved observation belongs to another account or domain")
    if observation.get("provider") not in _ACTUAL_PROVIDERS:
        raise HTTPException(422, "saved observation requires an actual provider")
    _actual_provenance(observation.get("provenance"))
    observed = _time(observation.get("observed_at"))
    if not published <= observed <= now:
        raise HTTPException(422, "observation time is outside the actual publication interval")
    if _feed_id(observation.get("source_ref")) != note_ids[0]:
        raise HTTPException(422, "observation does not describe the registered publication")
    values = _actual_values(observation.get("values"))
    if "post_metrics" in metrics and _actual_values(metrics["post_metrics"]) != values:
        raise HTTPException(422, "saved observation and post metrics disagree")
    if "post_metrics_synced_at" in metrics and _time(metrics["post_metrics_synced_at"]) != observed:
        raise HTTPException(422, "saved observation times disagree")
    return {**observation, "values": values, "observed_at": observed.isoformat(),
            "note_id": note_ids[0], "observation_id": metrics.get("observation_id")}


def _match_evidence_ref(ref: dict, observation: dict) -> None:
    _reject_invalidated(ref)
    for key in ("timestamp", "observed_at"):
        if key in ref and _time(ref[key]) != _time(observation["observed_at"]):
            raise HTTPException(422, "evidence reference time does not match the saved observation")
    for key in ("provider", "observation_id"):
        if key in ref and (not ref[key] or ref[key] != observation.get(key)):
            raise HTTPException(422, "evidence reference does not match the saved observation")
    source_ref = str(ref.get("source_ref") or "")
    if "://" in source_ref and _feed_id(source_ref) != observation["note_id"]:
        raise HTTPException(422, "evidence reference describes another publication")
    if "values" in ref and _actual_values(ref["values"]) != observation["values"]:
        raise HTTPException(422, "evidence reference counters do not match the saved observation")
    if "provenance" in ref:
        _actual_provenance(ref["provenance"])


def _referenced_observation(client: Client, task: dict, ref: dict, *, account_id: str, domain_slug: str) -> dict:
    nested = ref.get("observation", {})
    if not isinstance(nested, dict):
        raise HTTPException(422, "invalid referenced observation metadata")
    if nested.get("observation_id") is None:
        # Older feedback workers emitted a null optional ID alongside actual source/time metadata.
        nested = {key: value for key, value in nested.items() if key != "observation_id"}
    identifiers = [entry["observation_id"] for entry in (ref, nested) if "observation_id" in entry]
    if str(ref.get("source_type") or ref.get("kind") or "").lower() == "task_metrics" and "id" in ref:
        identifiers.append(ref["id"])
    if identifiers and (any(not isinstance(value, str) or not value.strip() or len(value) > 200
                            for value in identifiers) or len(set(identifiers)) != 1):
        raise HTTPException(422, "invalid or inconsistent observation identifiers")
    times = [_time(entry[key]) for entry in (ref, nested) for key in ("timestamp", "observed_at") if key in entry]
    if times and len(set(times)) != 1:
        raise HTTPException(422, "referenced observation times disagree")
    current = task.get("metrics_jsonb") or {}
    if not isinstance(current, dict):
        raise HTTPException(422, "invalid current observation metadata")
    snapshot_id = identifiers[0] if identifiers else None
    if not snapshot_id and not times:
        snapshot_id = current.get("observation_id")
    snapshot = None
    if snapshot_id or times:
        query = client.table("task_metrics").select("*").eq("pipeline_task_id", task["id"])
        query = query.eq("id", snapshot_id) if snapshot_id else query.eq("captured_at", times[0].isoformat())
        rows = query.limit(2).execute().data or []
        if len(rows) > 1 or (not rows and (snapshot_id or current.get("observation_id"))):
            raise HTTPException(422, "referenced observation snapshot is missing or ambiguous")
        if rows:
            snapshot = rows[0]
    if snapshot is None:
        # Legacy actual metadata has no immutable row; it must still match the reference exactly.
        observation = _actual_observation(task, account_id=account_id, domain_slug=domain_slug)
    else:
        _reject_invalidated(snapshot)
        metrics = deepcopy(snapshot.get("metrics_jsonb"))
        if not isinstance(metrics, dict) or not snapshot.get("id"):
            raise HTTPException(422, "invalid observation snapshot metadata")
        if metrics.get("observation_id") not in (None, snapshot["id"]):
            raise HTTPException(422, "snapshot observation identity disagrees")
        metrics["observation_id"] = snapshot["id"]
        observation = _actual_observation(task, account_id=account_id, domain_slug=domain_slug, metrics=metrics)
        registration = task["publish_jsonb"]["manual_publication"]
        invalidations = registration.get("snapshot_invalidations", [])
        if not isinstance(invalidations, list) or any(not isinstance(entry, dict) for entry in invalidations):
            raise HTTPException(422, "invalid publication correction metadata")
        if any(entry.get("id") == snapshot["id"] for entry in invalidations):
            raise HTTPException(422, "referenced snapshot was invalidated by a publication correction")
        if _time(snapshot.get("captured_at")) != _time(observation["observed_at"]):
            raise HTTPException(422, "snapshot capture time disagrees with its observation")
    _match_evidence_ref(ref, observation)
    _match_evidence_ref(nested, observation)
    return observation


def _evidence(client: Client, body: dict, proposal: dict, *, account_id: str, domain_id: str, domain_slug: str) -> list:
    refs = body.get("evidence_refs", proposal.get("evidence_refs"))
    if not isinstance(refs, list) or not refs or len(refs) > 100 or len(_json(refs)) > 32768:
        raise HTTPException(422, "bounded own-account evidence references are required")
    own_evidence = False
    checked_tasks = {}
    resolved_refs = []
    for ref in refs:
        task_id = ""
        if isinstance(ref, str) and ref.strip():
            if "://" not in ref:
                task_id = ref.strip()
        elif isinstance(ref, dict) and str(ref.get("source_ref") or ref.get("task_id") or ref.get("pipeline_task_id") or ref.get("id") or "").strip():
            for key, expected in (("account_id", account_id), ("domain_id", domain_id), ("domain_slug", domain_slug)):
                if ref.get(key) is not None and ref[key] != expected:
                    raise HTTPException(422, "evidence belongs to another account or domain")
            kind = str(ref.get("source_type") or ref.get("kind") or "").lower()
            if "competitor" not in kind:
                task_id = str(ref.get("task_id") or ref.get("pipeline_task_id") or "").strip()
                source_ref = str(ref.get("source_ref") or "").strip()
                if not task_id and source_ref and "://" not in source_ref:
                    task_id = source_ref
        else:
            raise HTTPException(422, "invalid evidence reference")
        if task_id:
            if task_id not in checked_tasks:
                checked_tasks[task_id] = _get(client, "pipeline_tasks", id=task_id, account_id=account_id, domain_id=domain_id)
            observation = _referenced_observation(client, checked_tasks[task_id], ref if isinstance(ref, dict) else {},
                                                  account_id=account_id, domain_slug=domain_slug)
            if observation.get("observation_id"):
                ref = deepcopy(ref) if isinstance(ref, dict) else {"source_ref": ref}
                ref.update(task_id=task_id, observation_id=observation["observation_id"])
                ref.setdefault("observed_at", observation["observed_at"])
                ref.setdefault("provider", observation["provider"])
            own_evidence = True
        resolved_refs.append(deepcopy(ref))
    if not own_evidence:
        raise HTTPException(422, "competitor-only evidence cannot confirm account SOP")
    if len(_json(resolved_refs)) > 32768:
        raise HTTPException(422, "resolved evidence exceeds confirmation size limit")
    return resolved_refs


def _validate_fields(target: str, before: dict, after: dict) -> None:
    allowed = _PUBLISH_FIELDS if target == "publish_preferences" else set(_normalize(target, {}))
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if changed - allowed:
        raise HTTPException(422, "proposal contains unsupported change fields")
    if target == "publish_preferences":
        for key in ("min_action_gap_seconds", "max_action_gap_seconds"):
            if key in after and (type(after[key]) is not int or not 1 <= after[key] <= 3600):
                raise HTTPException(422, "invalid publish action interval")
        if "next_publish_slot_local" in after and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(after["next_publish_slot_local"])):
            raise HTTPException(422, "invalid local publish slot")
        if "updated_by_feedback" in after and type(after["updated_by_feedback"]) is not bool:
            raise HTTPException(422, "invalid feedback preference marker")
        if after.get("max_action_gap_seconds", 3600) < after.get("min_action_gap_seconds", 1):
            raise HTTPException(422, "publish action interval is inverted")


def _result(account: dict, receipt: dict) -> dict:
    return {
        "status": "ok", "item_id": receipt["item_id"], "account_id": receipt["account_id"],
        "account_name": str(account.get("account_name") or ""), "domain_slug": receipt["domain_slug"],
        "target": receipt["target"], "reason": receipt["reason"],
        "before_summary": receipt["before_summary"], "after_summary": receipt["after_summary"],
        "memory_status": "active", "version": receipt["version"], "active_version": _version(_config(account)),
        "sop_latest": deepcopy(receipt["snapshot"]), "receipt": deepcopy(receipt),
    }


def apply_confirmed_strategy(
    client: Client, *, item_id: str, domain_slug: str, account_id: str,
    target: str, actor: Actor, proposal: dict | None = None,
) -> dict:
    """Apply a persisted suggestion or an injected before/after proposal, then return its receipt.

    Content may carry ``proposal`` or ``proposed_changes`` and ``evidence_refs``.
    Evidence must match an actual observation of a scoped, registered manual publication.
    Once prepared, retries use the stored proposal, even if the caller regenerates one.
    Database uncertainty returns 503; repeat the same item/account/target to recover.
    """
    if actor.role not in {"admin", "operator"}:
        raise HTTPException(403, "strategy confirmation requires an operator")
    if not all(isinstance(value, str) and value.strip() for value in (item_id, domain_slug, account_id)):
        raise HTTPException(422, "item, domain and account are required")
    if target not in _TARGETS:
        raise HTTPException(422, "unsupported apply target")
    try:
        return _apply(client, item_id=item_id, domain_slug=domain_slug, account_id=account_id,
                      target=target, actor=actor, proposal=proposal)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "strategy confirmation persistence is unconfirmed; retry the same request to recover") from exc


def _apply(client: Client, *, item_id: str, domain_slug: str, account_id: str,
           target: str, actor: Actor, proposal: dict | None) -> dict:
    domain = _get(client, "domains", slug=domain_slug)
    item = _get(client, "memory_items", id=item_id, domain_id=domain["id"], account_id=account_id)
    account = _get(client, "channel_accounts", id=account_id)
    config = _config(account)
    if config.get("domain_slug") and config["domain_slug"] != domain_slug:
        raise HTTPException(422, "account belongs to another domain")
    body = _body(item)
    receipts = config.setdefault(_RECEIPTS, {})
    receipt = receipts.get(item_id)
    fingerprint = _source_fingerprint(item)
    if receipt:
        if not isinstance(receipt, dict) or receipt.get("stage") not in {"prepared", "configured", "checkpointed", "audited", "completed"}:
            raise HTTPException(409, "invalid confirmation receipt stage")
        if any(receipt.get(key) != expected for key, expected in
               (("account_id", account_id), ("domain_slug", domain_slug), ("target", target), ("source_fingerprint", fingerprint))):
            raise HTTPException(409, "confirmation receipt scope or source changed")
        _evidence(client, {"evidence_refs": receipt["evidence_refs"]}, {},
                  account_id=account_id, domain_id=domain["id"], domain_slug=domain_slug)
        if receipt["stage"] == "completed":
            if item["status"] != "active":
                raise HTTPException(409, "confirmed suggestion is no longer active")
            _get(client, "audit_logs", id=receipt["id"])
            return _result(account, receipt)
        if item["status"] != "pending" and not (item["status"] == "active" and receipt["stage"] == "audited"):
            raise HTTPException(409, "suggestion state changed during confirmation")
        _validate_executable(item, body, None)
    else:
        if item.get("status") != "pending":
            raise HTTPException(422, "item is not pending")
        _validate_executable(item, body, proposal)
        selected = _proposal(body, target, proposal)
        _validate_fields(target, selected["before"], selected["after"])
        before = _normalize(target, selected["before"])
        after_input = {**selected["before"], **selected["after"]} if target != "publish_preferences" else selected["after"]
        after = _normalize(target, after_input)
        version = _version(config)
        expected_version = selected.get("before_version", selected.get("version", body.get("before_version", version)))
        if type(expected_version) is not int or expected_version != version or _normalize(target, config.get(target) or {}) != before:
            raise HTTPException(409, "proposal is stale; review current configuration and version")
        if before == after:
            raise HTTPException(422, "proposal has no executable change")
        refs = _evidence(client, body, selected, account_id=account_id, domain_id=domain["id"], domain_slug=domain_slug)
        if len(receipts) >= 100:
            completed = next((key for key, value in receipts.items() if value.get("stage") == "completed"), None)
            if completed is None:
                raise HTTPException(409, "too many unfinished confirmations")
            del receipts[completed]
        receipt = {
            "id": str(uuid5(NAMESPACE_URL, f"sop-confirmation:{account_id}:{item_id}:{target}")),
            "item_id": item_id, "account_id": account_id, "domain_slug": domain_slug, "target": target,
            "actor": actor.user_id, "reason": str(selected.get("reason") or "confirmed strategy")[:400],
            "before_summary": str(selected.get("before_summary") or "")[:2000],
            "after_summary": str(selected.get("after_summary") or "")[:2000],
            "before": before, "after": after, "base_version": version,
            "evidence_refs": refs, "source_fingerprint": fingerprint, "stage": "prepared", "created_at": _now(),
        }
        receipts[item_id] = receipt
        account = _save_config(client, account, config)

    if receipt["stage"] == "prepared":
        if _version(config) != receipt["base_version"] or _normalize(target, config.get(target) or {}) != receipt["before"]:
            raise HTTPException(409, "account changed since confirmation was prepared")
        config[target] = deepcopy(receipt["after"])
        receipt["stage"] = "configured"
        account = _save_config(client, account, config)

    if receipt["stage"] == "configured":
        if _version(config) != receipt["base_version"] or _normalize(target, config.get(target) or {}) != receipt["after"]:
            raise HTTPException(409, "account changed before the SOP checkpoint")
        # Reuse the checkpoint builder inside the same conditional write as its receipt.
        snapshot = _append_sop_snapshot(
            config, reason=receipt["reason"], changed_fields=[target], actor=receipt["actor"],
            context={"source_item_id": item_id, "confirmation_id": receipt["id"],
                     "domain_slug": domain_slug, "account_id": account_id, "evidence_refs": receipt["evidence_refs"],
                     "before": receipt["before"], "after": receipt["after"]},
        )
        snapshot["publish_preferences"] = deepcopy(config.get("publish_preferences") or {})
        receipt.update(stage="checkpointed", version=snapshot["version"], snapshot=deepcopy(snapshot))
        account = _save_config(client, account, config)

    if receipt["stage"] in {"checkpointed", "audited"}:
        if _version(config) != receipt["version"] or _normalize(target, config.get(target) or {}) != receipt["after"]:
            raise HTTPException(409, "account changed after the SOP checkpoint")
        if not any(snapshot == receipt["snapshot"] for snapshot in config.get("sop_snapshots", [])):
            raise HTTPException(409, "SOP checkpoint persistence is unconfirmed")

    if receipt["stage"] == "checkpointed":
        audits = client.table("audit_logs").select("id").eq("id", receipt["id"]).limit(1).execute().data or []
        if not audits:
            write_audit_log(client, _ConfirmationAudit(
                id=receipt["id"], actor=receipt["actor"], action="ops.pending_strategy_item_applied",
                target_type="memory_item", target_id=item_id,
                diff_jsonb={"confirmation_id": receipt["id"], "account_id": account_id, "domain_slug": domain_slug,
                            "target": target, "from": receipt["before"], "to": receipt["after"],
                            "reason": receipt["reason"], "version": receipt["version"], "evidence_refs": receipt["evidence_refs"]},
            ))
        _get(client, "audit_logs", id=receipt["id"])
        receipt["stage"] = "audited"
        account = _save_config(client, account, config)

    _evidence(client, {"evidence_refs": receipt["evidence_refs"]}, {},
              account_id=account_id, domain_id=domain["id"], domain_slug=domain_slug)
    current = _get(client, "memory_items", id=item_id, domain_id=domain["id"], account_id=account_id)
    if _source_fingerprint(current) != fingerprint:
        raise HTTPException(409, "suggestion changed before activation")
    if current.get("status") == "pending":
        query = (client.table("memory_items").update({"status": "active", "updated_at": _now()})
                 .eq("id", item_id).eq("domain_id", domain["id"]).eq("account_id", account_id).eq("status", "pending"))
        if current.get("updated_at") is not None:
            query = query.eq("updated_at", current["updated_at"])
        if not query.execute().data:
            raise HTTPException(409, "suggestion changed during activation")
    elif current.get("status") != "active":
        raise HTTPException(409, "suggestion is no longer pending")
    active = _get(client, "memory_items", id=item_id, domain_id=domain["id"], account_id=account_id)
    if active.get("status") != "active" or _source_fingerprint(active) != fingerprint:
        raise HTTPException(409, "suggestion activation persistence is unconfirmed")
    receipt.update(stage="completed", completed_at=_now())
    account = _save_config(client, account, config)
    return _result(account, receipt)
