"""Record a human publication. This module never calls an external publisher."""

from copy import deepcopy
from datetime import datetime, timezone
import re
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException
import httpx
from postgrest.exceptions import APIError

from app.models import Actor
from app.security import ensure_role_in


_PUBLICATION_LOCK = RLock()
_READ_LIMIT = 1000
_PUBLISHED_STATES = {"published", "metrics_ready", "reflecting", "reflection_failed", "done"}
_WRITE_ERRORS = (APIError, httpx.HTTPError, TimeoutError, ConnectionError)
_NOTE_ID = re.compile(r"[0-9a-fA-F]{24}")
_NOTE_PATH = re.compile(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _canonical_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 4096 or any(char.isspace() for char in value) or "\\" in value:
        raise HTTPException(422, "A canonical Xiaohongshu note URL is required")
    try:
        parsed = urlsplit(value)
        match = _NOTE_PATH.fullmatch(parsed.path)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in {"xiaohongshu.com", "www.xiaohongshu.com"}
            and parsed.username is None and parsed.password is None
            and parsed.port in {None, 443} and match is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(422, "Use an HTTPS Xiaohongshu explore or discovery/item URL with a 24-hex note ID")
    note_id = match.group(1).lower()
    return f"https://www.xiaohongshu.com/explore/{note_id}", note_id


def _time(value) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _safe_archive(value):
    """Historical state is recoverable, but URL credentials must not reach receipts."""
    if isinstance(value, dict):
        return {key: _safe_archive(item) for key, item in value.items()
                if not any(secret in key.lower() for secret in ("token", "cookie", "password", "secret", "authorization"))}
    if isinstance(value, list):
        return [_safe_archive(item) for item in value]
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        try:
            parsed = urlsplit(value)
            return parsed._replace(netloc=parsed.hostname or "", query="", fragment="").geturl()
        except ValueError:
            return "[invalid URL]"
    return deepcopy(value)


def _task(client, task_id: str) -> dict:
    rows = client.table("pipeline_tasks").select("*").eq("id", task_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, "Pipeline task not found")
    return rows[0]


def _validate_scope(client, row: dict, account_id: str, domain_slug: str) -> None:
    domains = client.table("domains").select("id,slug").eq("slug", domain_slug).limit(1).execute().data or []
    if not domains:
        raise HTTPException(404, "Domain not found")
    direct_account = str(row.get("account_id") or "")
    payload_account = str(_dict(row.get("payload_jsonb")).get("channel_account_id") or "")
    if (row.get("deleted_at") or row.get("domain_id") != domains[0]["id"]
            or row.get("channel") != "xiaohongshu"
            or not (direct_account or payload_account)
            or any(value != account_id for value in (direct_account, payload_account) if value)):
        raise HTTPException(409, "Task does not belong to the selected account and domain")
    accounts = client.table("channel_accounts").select("*").eq("id", account_id).limit(1).execute().data or []
    if not accounts or accounts[0].get("is_active") is not True or accounts[0].get("channel") != "xiaohongshu":
        raise HTTPException(409, "An active Xiaohongshu account is required")
    account = accounts[0]
    configured = _dict(_dict(account.get("config_jsonb")).get("onboarding")).get("domain_slug")
    if ((configured and configured != domain_slug)
            or (account.get("domain_id") and account["domain_id"] != domains[0]["id"])):
        raise HTTPException(409, "Account domain does not match the selected task")


def _note_ids(row: dict) -> set[str]:
    publish = _dict(row.get("publish_jsonb"))
    sources = [_dict(publish.get("identity")), _dict(publish.get("last_result")), _dict(row.get("payload_jsonb"))]
    ids = set()
    for source in sources:
        for key in ("feed_id", "note_id", "remote_post_id"):
            value = str(source.get(key) or "")
            if _NOTE_ID.fullmatch(value):
                ids.add(value.lower())
        if source.get("published_url"):
            try:
                ids.add(_canonical_url(source["published_url"])[1])
            except HTTPException:
                pass
    return ids


def _check_duplicate(client, *, task_id: str, account_id: str, note_id: str) -> None:
    # Include legacy payload ownership and all domains; a completed post is still owned.
    seen = set()
    for column in ("account_id", "payload_jsonb->>channel_account_id"):
        rows = (client.table("pipeline_tasks").select("*").eq(column, account_id)
                .eq("channel", "xiaohongshu").limit(_READ_LIMIT + 1).execute().data or [])
        if len(rows) > _READ_LIMIT:
            raise HTTPException(409, "Duplicate-note check exceeded its bound; reconcile account publications first")
        for row in rows:
            if row["id"] in seen or row["id"] == task_id or row.get("deleted_at"):
                continue
            seen.add(row["id"])
            if note_id in _note_ids(row):
                raise HTTPException(409, "This note is already attached to another task for this account; an audited transfer is required")


def _matches(row: dict, note_id: str, published_at: datetime) -> bool:
    publish = _dict(row.get("publish_jsonb"))
    manual = _dict(publish.get("manual_publication"))
    identity = _dict(publish.get("identity"))
    return (row.get("status") in _PUBLISHED_STATES and manual.get("confirmed") is True
            and identity.get("feed_id") == note_id and _time(row.get("published_at")) == published_at)


def _snapshots(client, task_id: str) -> list[dict]:
    rows = (client.table("task_metrics").select("*").eq("pipeline_task_id", task_id)
            .limit(_READ_LIMIT + 1).execute().data or [])
    if len(rows) > _READ_LIMIT:
        raise HTTPException(409, "Correction snapshot count exceeded its bound; reconcile task evidence first")
    return rows


def _finish_registration(client, row: dict) -> dict:
    """Complete recoverable side writes without mutating task state on an identical retry."""
    receipt = _dict(_dict(row.get("publish_jsonb")).get("manual_publication"))
    revision = receipt.get("revision")
    for saved in receipt.get("snapshot_invalidations", []):
        invalidation = {
            "revision": revision, "reason": receipt["correction_reason"], "invalidated_at": receipt["confirmed_at"],
            "previous_metrics": saved["metrics_jsonb"], "previous_score": saved.get("score"),
        }
        try:
            current = (client.table("task_metrics").select("*").eq("id", saved["id"])
                       .eq("pipeline_task_id", row["id"]).limit(1).execute().data or [])
            if not current:
                raise HTTPException(503, "Correction snapshot invalidation is unconfirmed; retry this same submission")
            if _dict(_dict(current[0].get("metrics_jsonb")).get("publication_invalidated")).get("revision") == revision:
                continue
            rows = (client.table("task_metrics").update({"metrics_jsonb": {"publication_invalidated": invalidation}, "score": 0})
                    .eq("id", saved["id"]).eq("pipeline_task_id", row["id"]).execute().data or [])
            if not rows:
                raise HTTPException(503, "Correction snapshot invalidation is unconfirmed; retry this same submission")
        except _WRITE_ERRORS as exc:
            raise HTTPException(503, "Publication saved; snapshot invalidation is unconfirmed. Retry this same submission") from exc
    audit = receipt.get("audit")
    if audit:
        try:
            existing = client.table("audit_logs").select("id").eq("id", audit["id"]).limit(1).execute().data or []
            if not existing:
                client.table("audit_logs").insert(audit).execute()
                existing = client.table("audit_logs").select("id").eq("id", audit["id"]).limit(1).execute().data or []
                if not existing:
                    raise HTTPException(503, "Publication saved; audit is unconfirmed. Retry this same submission")
        except _WRITE_ERRORS as exc:
            try:
                existing = client.table("audit_logs").select("id").eq("id", audit["id"]).limit(1).execute().data or []
            except _WRITE_ERRORS:
                existing = []
            if not existing:
                raise HTTPException(503, "Publication saved; audit is unconfirmed. Retry this same submission") from exc
    return row


def register_manual_publication(
    client, *, pipeline_task_id: str, account_id: str, domain_slug: str,
    published_url: str, published_at: datetime, confirmed: bool, actor: Actor,
    correction_reason: str = "",
) -> dict:
    """Return the normal task row after recording explicit user-confirmed publication.

    A nonempty correction reason changes an existing publication. Identical retries
    preserve feedback and finish any interrupted audit/snapshot writes. Callers must
    serialize registration per account across workers: the local lock protects only
    this process; the existing schema has no account/note uniqueness constraint.
    """
    ensure_role_in(actor.role, ("operator", "admin"))
    if not actor.user_id.strip():
        raise HTTPException(403, "An identified operator or administrator is required")
    if confirmed is not True:
        raise HTTPException(422, "Explicit confirmation that this is the account's published note is required")
    if not pipeline_task_id or not account_id or not domain_slug:
        raise HTTPException(422, "Task, account and domain are required")
    url, note_id = _canonical_url(published_url)
    actual_time = _time(published_at) if isinstance(published_at, datetime) else None
    if actual_time is None or actual_time > _now():
        raise HTTPException(422, "Publication time must include a timezone and must not be in the future")
    if not isinstance(correction_reason, str) or len(correction_reason) > 4000:
        raise HTTPException(422, "Correction reason must be text of at most 4000 characters")
    reason = correction_reason.strip()
    actual_iso = actual_time.isoformat()

    with _PUBLICATION_LOCK:
        row = _task(client, pipeline_task_id)
        _validate_scope(client, row, account_id, domain_slug)
        _check_duplicate(client, task_id=pipeline_task_id, account_id=account_id, note_id=note_id)
        if _matches(row, note_id, actual_time):
            return _finish_registration(client, row)

        old_publish = _dict(row.get("publish_jsonb"))
        has_publication = bool(row.get("published_at") or _note_ids(row))
        correcting = has_publication and row.get("status") in _PUBLISHED_STATES
        if correcting and not reason:
            raise HTTPException(409, "Changing a publication requires an explicit correction reason")
        if not correcting and (row.get("status") != "approved" or has_publication or reason):
            raise HTTPException(409, "Only a reviewed, approved draft can register its first manual publication")
        if correcting and old_publish.get("manual_publication"):
            _finish_registration(client, row)

        timestamp = _now().isoformat()
        revision = str(uuid4())
        snapshots = _snapshots(client, pipeline_task_id) if correcting else []
        history = _safe_archive(old_publish.get("publication_history") or [])
        previous = None
        if correcting:
            previous_publish = {key: value for key, value in old_publish.items() if key != "publication_history"}
            if previous_publish.get("manual_publication"):
                previous_publish["manual_publication"] = {
                    key: value for key, value in _dict(previous_publish["manual_publication"]).items()
                    if key not in {"audit", "snapshot_invalidations"}
                }
            previous = _safe_archive({
                "identity": _dict(old_publish.get("identity")), "published_at": row.get("published_at"),
                "publish_jsonb": previous_publish,
                "metrics_jsonb": _dict(row.get("metrics_jsonb")), "status": row["status"], "stage": row.get("stage"),
            })
            history.append({"revision": revision, "actor": actor.user_id, "reason": reason,
                            "corrected_at": timestamp, "previous": previous})
        identity = {"feed_id": note_id, "remote_post_id": note_id, "published_url": url,
                    "recovered_by": "manual_publication", "recovered_at": timestamp}
        audit = {
            "id": revision, "actor": actor.user_id,
            "action": "pipeline.manual_publication_corrected" if correcting else "pipeline.manual_publication_registered",
            "target_type": "pipeline_task", "target_id": pipeline_task_id,
            "diff_jsonb": {"account_id": account_id, "domain_slug": domain_slug,
                           "from": row["status"], "to": "published", "identity": identity,
                           "published_at": actual_iso, "ownership": "user_confirmed", "reason": reason,
                           "previous": previous, "invalidated_snapshot_ids": [item["id"] for item in snapshots]},
        }
        receipt = {
            "revision": revision, "confirmed": True, "ownership": "user_confirmed", "provider_verified": False,
            "confirmed_by": actor.user_id, "confirmed_at": timestamp, "account_id": account_id,
            "domain_slug": domain_slug, "correction_reason": reason, "audit": audit,
            "snapshot_invalidations": _safe_archive([
                {"id": item["id"], "metrics_jsonb": _dict(item.get("metrics_jsonb")), "score": item.get("score")}
                for item in snapshots
            ]),
        }
        # Replace the evidence generation: old counters/checkpoints cannot describe
        # a changed note or publication clock. Its prior state remains in history.
        publish = {
            "identity": identity, "published_at": actual_iso, "manual_publication": receipt,
            "publication_history": history, "feedback_state": "pending_metrics",
            "feedback_schedule_hours": old_publish.get("feedback_schedule_hours") or [1, 3, 24],
            "feedback_completed_hours": [],
        }
        changes = {"status": "published", "stage": "feedback_pending", "published_at": actual_iso,
                   "publish_jsonb": publish, "metrics_jsonb": {}, "updated_at": timestamp}
        query = (client.table("pipeline_tasks").update(changes).eq("id", pipeline_task_id)
                 .eq("domain_id", row["domain_id"]).eq("status", row["status"]))
        if row.get("account_id"):
            query = query.eq("account_id", account_id)
        else:
            query = query.eq("payload_jsonb->>channel_account_id", account_id)
        query = query.eq("updated_at", row["updated_at"]) if row.get("updated_at") else query.is_("updated_at", "null")
        write_error = None
        try:
            rows = query.execute().data or []
        except _WRITE_ERRORS as exc:
            rows, write_error = [], exc
        if rows:
            return _finish_registration(client, rows[0])

        # Never replay an uncertain update. A concurrent identical registration is
        # success; a different current row must be presented to the operator.
        try:
            current = _task(client, pipeline_task_id)
            _validate_scope(client, current, account_id, domain_slug)
        except _WRITE_ERRORS as exc:
            raise HTTPException(503, "Publication write outcome is unknown; check task status before retrying") from exc
        if _matches(current, note_id, actual_time):
            return _finish_registration(client, current)
        if write_error is not None:
            raise HTTPException(503, "Publication write outcome is unconfirmed; check task status before retrying") from write_error
        raise HTTPException(409, "Task changed during publication registration; refresh before submitting again")
