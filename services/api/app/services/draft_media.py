"""Account-scoped original draft uploads; detachment never purges stored bytes."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from urllib.parse import quote
import warnings

from fastapi import HTTPException

from app.models import Actor


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 9
EDITABLE_STATES = frozenset({
    "queued", "intel_ready", "pending_review", "review_rejected", "approved", "publish_failed",
})
FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}


def _uuid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", value,
    ):
        raise HTTPException(422, "Invalid media scope identifier")
    return str(UUID(value))


def _image_id(account_id: str, task_id: str, checksum: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"draft-image:{account_id}:{task_id}:{checksum}"))


def _editor(actor: Actor) -> None:
    if actor.role not in {"admin", "operator", "reviewer"}:
        raise HTTPException(403, "Draft image changes require an editor role")


def _task(client, task_id: str, account_id: str) -> dict:
    try:
        rows = client.table("pipeline_tasks").select("*").eq("id", task_id).limit(1).execute().data
    except Exception as exc:
        raise HTTPException(503, "Cannot confirm the draft task; refresh before retrying") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        if rows == []:
            raise HTTPException(404, "Draft task not found in this account")
        raise HTTPException(503, "Cannot confirm the draft task; refresh before retrying")
    row = rows[0]
    payload = row.get("payload_jsonb")
    if not isinstance(payload, dict):
        raise HTTPException(409, "Invalid draft payload")
    scopes = [row.get("account_id"), payload.get("channel_account_id")]
    scopes = [scope for scope in scopes if scope]
    if row.get("id") != task_id or row.get("deleted_at") or not scopes or any(scope != account_id for scope in scopes):
        raise HTTPException(404, "Draft task not found in this account")
    return deepcopy(row)


def _editable(row: dict) -> None:
    publication = row.get("publish_jsonb") or {}
    if not isinstance(publication, dict):
        raise HTTPException(409, "Invalid publication state")
    identity = publication.get("identity") or {}
    if not isinstance(identity, dict):
        raise HTTPException(409, "Invalid publication identity")
    published = row.get("published_at") or publication.get("manual_publication") or any(
        identity.get(key) or publication.get(key)
        for key in ("published_at", "published_url", "remote_post_id", "feed_id")
    )
    if row.get("status") not in EDITABLE_STATES or published:
        raise HTTPException(409, "Only unpublished editable drafts can change images")


def _decoded(content: bytes) -> dict:
    if not isinstance(content, bytes) or not content:
        raise HTTPException(422, "Image content must be nonempty bytes")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Draft image exceeds 10 MiB")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        expected = "PNG"
    elif content.startswith(b"\xff\xd8\xff"):
        expected = "JPEG"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        expected = "WEBP"
    else:
        raise HTTPException(422, "Only PNG, JPEG and WEBP images are supported")
    try:
        from PIL import Image
    except ImportError as exc:
        raise HTTPException(503, "Pillow is required to validate draft images") from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format != expected:
                    raise ValueError("Image signature does not match decoded format")
                image.verify()
            # verify() alone does not decode JPEG pixels or every animation frame.
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                for frame in range(getattr(image, "n_frames", 1)):
                    image.seek(frame)
                    image.load()
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(422, "Image is corrupt, truncated or unsafe to decode") from exc
    extension, mime = FORMATS[expected]
    return {"sha256": sha256(content).hexdigest(), "extension": extension,
            "content_type": mime, "bytes": len(content), "width": width, "height": height}


def _media(row: dict, account_id: str, task_id: str) -> dict:
    media = deepcopy(row["payload_jsonb"].get("draft_media", {
        "status": "awaiting_upload", "images": [], "audit": [],
    }))
    if not isinstance(media, dict) or not isinstance(media.get("images"), list) or not isinstance(media.get("audit", []), list):
        raise HTTPException(409, "Invalid draft image state")
    seen = set()
    for image in media["images"]:
        _entry(image, account_id, task_id)
        if image["sha256"] in seen:
            raise HTTPException(409, "Duplicate draft image records")
        seen.add(image["sha256"])
    media.setdefault("audit", [])
    return media


def _entry(image: dict, account_id: str, task_id: str) -> None:
    if not isinstance(image, dict):
        raise HTTPException(409, "Invalid draft image metadata")
    checksum = image.get("sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise HTTPException(409, "Invalid draft image checksum")
    extension = image.get("extension")
    mime = {extension: mime for extension, mime in FORMATS.values()}.get(extension) if isinstance(extension, str) else None
    if (not mime or image.get("content_type") != mime or image.get("source") != "manual_upload"
            or image.get("status") not in ("attached", "detached")
            or image.get("id") != _image_id(account_id, task_id, checksum)
            or any(type(image.get(key)) is not int or image[key] <= 0 for key in ("bytes", "width", "height"))
            or image["bytes"] > MAX_IMAGE_BYTES):
        raise HTTPException(409, "Invalid or non-original draft image metadata")


def _path(media_root: Path, account_id: str, task_id: str, image: dict) -> Path:
    root = Path(media_root).resolve()
    path = root
    for component in ("drafts", account_id, task_id, image["sha256"] + "." + image["extension"]):
        path = path / component
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or path.resolve() != path:
            raise HTTPException(422, "Draft media path redirects outside its namespace")
    if not path.is_relative_to(root / "drafts" / account_id / task_id):
        raise HTTPException(422, "Invalid draft media path")
    return path


def _file_content(path: Path, image: dict) -> bytes:
    if not path.is_file():
        raise HTTPException(404, "Draft image file is missing; reupload the original image")
    try:
        with path.open("rb") as source:
            content = source.read(MAX_IMAGE_BYTES + 1)
    except OSError as exc:
        raise HTTPException(503, "Draft image file cannot be read") from exc
    if len(content) != image["bytes"] or sha256(content).hexdigest() != image["sha256"]:
        raise HTTPException(409, "Draft image file does not match its saved checksum")
    return content


def _store_file(path: Path, image: dict, content: bytes) -> None:
    if os.path.lexists(path):
        _file_content(path, image)
        return
    temporary = path.with_name(f"{uuid4()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if os.path.lexists(path):
            _file_content(path, image)
        else:
            temporary.replace(path)
    except OSError as exc:
        raise HTTPException(503, "Draft image file could not be stored atomically") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _changes(row: dict, media: dict, *, action: str, image_id: str, actor: Actor) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    revision = str(uuid4())
    media["revision"] = revision
    media["status"] = "ready" if any(image["status"] == "attached" for image in media["images"]) else "awaiting_upload"
    media["audit"].append({
        "id": revision, "action": action, "image_id": image_id, "actor": actor.user_id, "at": now,
        "previous_review": deepcopy(row.get("review_jsonb") or {}),
    })
    payload = deepcopy(row["payload_jsonb"])
    payload["draft_media"] = media
    analysis = deepcopy(payload.get("analysis_jsonb") or {})
    if not isinstance(analysis, dict):
        raise HTTPException(409, "Invalid draft analysis")
    analysis.pop("quality_gate", None)
    analysis["review_required_after_edit"] = True
    payload["analysis_jsonb"] = analysis
    review = deepcopy(row.get("review_jsonb") or {})
    if not isinstance(review, dict):
        raise HTTPException(409, "Invalid draft review")
    review.pop("approved_by", None)
    review.pop("approved_at", None)
    review.update({"invalidated_at": now, "invalidated_by": actor.user_id, "invalidation_reason": "draft_images_changed"})
    changes = {"payload_jsonb": payload, "review_jsonb": review, "updated_at": now}
    if row["status"] in {"approved", "publish_failed", "review_rejected", "pending_review"}:
        changes.update({"status": "pending_review", "stage": "review"})
    return changes


def _write(client, row: dict, changes: dict, *, account_id: str, task_id: str) -> dict:
    query = client.table("pipeline_tasks").update(changes).eq("id", task_id)
    # Large drafts cannot fit in a Data API URL; every project writer advances updated_at.
    json_budget = 3500
    for key in ("status", "stage", "updated_at", "account_id", "published_at", "deleted_at",
                "payload_jsonb", "review_jsonb", "publish_jsonb"):
        if key not in row:
            continue
        value = row[key]
        if value is None:
            query = query.is_(key, "null")
        else:
            if key.endswith("_jsonb"):
                value = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
                size = len(quote(value, safe=''))
                if size > json_budget:
                    if not row.get('updated_at'):
                        raise HTTPException(409, "Draft has no revision timestamp; refresh before changing images")
                    continue
                json_budget -= size
            query = query.eq(key, value)
    uncertain = False
    try:
        query.execute()
    except Exception:
        uncertain = True
    # A timeout or empty/unknown response must never be presented as a saved attachment.
    try:
        current = _task(client, task_id, account_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(409, "Draft scope changed concurrently; refresh before retrying") from exc
        raise
    saved_media = current["payload_jsonb"].get("draft_media") or {}
    if isinstance(saved_media, dict) and saved_media.get("revision") == changes["payload_jsonb"]["draft_media"]["revision"]:
        return current
    if uncertain and current == row:
        raise HTTPException(503, "Cannot confirm the image change; refresh before retrying")
    raise HTTPException(409, "Draft changed concurrently; refresh before retrying")


def save_draft_image(client, *, task_id: str, account_id: str, content: bytes,
                     actor: Actor, media_root: Path) -> dict:
    """Attach original bytes, or restore a detached hash, returning the persisted task."""
    _editor(actor)
    task_id, account_id = _uuid(task_id), _uuid(account_id)
    row = _task(client, task_id, account_id)
    _editable(row)
    decoded = _decoded(content)
    media = _media(row, account_id, task_id)
    image = next((item for item in media["images"] if item["sha256"] == decoded["sha256"]), None)
    if image and any(decoded[key] != image[key] for key in decoded):
        raise HTTPException(409, "Saved draft image metadata does not match the original upload")
    if image and image["status"] == "attached":
        _store_file(_path(media_root, account_id, task_id, image), image, content)
        return row
    if sum(item["status"] == "attached" for item in media["images"]) >= MAX_IMAGES:
        raise HTTPException(409, "Draft already has 9 attached images")
    action = "restore" if image else "attach"
    if image is None:
        image = {**decoded, "id": _image_id(account_id, task_id, decoded["sha256"]), "source": "manual_upload"}
        media["images"].append(image)
    image.update({"status": "attached", "attached_by": actor.user_id,
                  "attached_at": datetime.now(timezone.utc).isoformat()})
    changes = _changes(row, media, action=action, image_id=image["id"], actor=actor)
    _store_file(_path(media_root, account_id, task_id, image), image, content)
    # Content-addressed bytes remain recoverable even if the task write is uncertain.
    return _write(client, row, changes, account_id=account_id, task_id=task_id)


def read_draft_image(client, *, task_id: str, account_id: str, image_id: str,
                     media_root: Path) -> tuple[Path, str]:
    """Read only an attached image in this scope; the parent route authenticates reads."""
    task_id, account_id, image_id = _uuid(task_id), _uuid(account_id), _uuid(image_id)
    row = _task(client, task_id, account_id)
    media = _media(row, account_id, task_id)
    image = next((item for item in media["images"] if item["id"] == image_id and item["status"] == "attached"), None)
    if not image:
        raise HTTPException(404, "Attached draft image not found in this task")
    path = _path(media_root, account_id, task_id, image)
    content = _file_content(path, image)
    decoded = _decoded(content)
    if any(decoded[key] != image[key] for key in decoded):
        raise HTTPException(409, "Draft image metadata does not match the decoded file")
    return path, image["content_type"]


def detach_draft_image(client, *, task_id: str, account_id: str, image_id: str,
                       actor: Actor, media_root: Path) -> dict:
    """Keep the image and its bytes for restoration by a later identical upload."""
    _editor(actor)
    task_id, account_id, image_id = _uuid(task_id), _uuid(account_id), _uuid(image_id)
    row = _task(client, task_id, account_id)
    _editable(row)
    media = _media(row, account_id, task_id)
    image = next((item for item in media["images"] if item["id"] == image_id), None)
    if not image:
        raise HTTPException(404, "Draft image not found in this task")
    _path(media_root, account_id, task_id, image)
    if image["status"] == "detached":
        return row
    image.update({"status": "detached", "detached_by": actor.user_id,
                  "detached_at": datetime.now(timezone.utc).isoformat()})
    changes = _changes(row, media, action="detach", image_id=image_id, actor=actor)
    return _write(client, row, changes, account_id=account_id, task_id=task_id)
