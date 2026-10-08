from copy import deepcopy
from hashlib import sha256
from importlib import import_module, util
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from PIL import Image

from app.models import Actor
from fake_store import MemoryStore, Query


TASK_ID = "11111111-1111-4111-8111-111111111111"
ACCOUNT_ID = "33333333-3333-4333-8333-333333333333"
OTHER_ID = "44444444-4444-4444-8444-444444444444"
ACTOR = Actor(user_id="operator-test", role="operator")
LIMIT = 10 * 1024 * 1024


class DraftQuery(Query):
    def eq(self, key, value):
        # PostgREST JSONB equality takes JSON text, not Python's dict repr.
        if key in {"payload_jsonb", "review_jsonb", "publish_jsonb"} and isinstance(value, str):
            value = json.loads(value)
        return super().eq(key, value)


class DraftStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.hook = None

    def table(self, name):
        query = DraftQuery(self.tables.setdefault(name, []))
        execute = query.execute
        query.execute = lambda: self.hook(name, query, execute) if self.hook else execute()
        return query


@pytest.fixture
def media():
    assert util.find_spec("app.services.draft_media") is not None, "Task7 draft_media service missing"
    return import_module("app.services.draft_media")


@pytest.fixture
def store():
    db = DraftStore()
    db.tables["pipeline_tasks"] = [{
        "id": TASK_ID, "account_id": ACCOUNT_ID, "channel": "xiaohongshu",
        "status": "pending_review", "stage": "review", "published_at": None,
        "updated_at": "2026-10-07T00:00:00+00:00", "deleted_at": None,
        "payload_jsonb": {"channel_account_id": ACCOUNT_ID, "title": "Original", "body": "Draft body",
                          "image_prompt": "Unfulfilled prompt", "reference_images": ["competitor.png"]},
        "review_jsonb": {}, "publish_jsonb": {},
    }]
    return db


def image_bytes(fmt="PNG", color=(20, 90, 140)):
    stream = BytesIO()
    Image.new("RGB", (3, 2), color).save(stream, format=fmt)
    return stream.getvalue()


def task(store):
    return store.tables["pipeline_tasks"][0]


def save(media, store, root, content=None, **kwargs):
    return media.save_draft_image(store, **{
        "task_id": TASK_ID, "account_id": ACCOUNT_ID, "actor": ACTOR,
        "media_root": root, "content": image_bytes() if content is None else content, **kwargs,
    })


def read(media, store, root, image_id, **kwargs):
    return media.read_draft_image(store, **{
        "task_id": TASK_ID, "account_id": ACCOUNT_ID, "image_id": image_id,
        "media_root": root, **kwargs,
    })


def detach(media, store, root, image_id, **kwargs):
    return media.detach_draft_image(store, **{
        "task_id": TASK_ID, "account_id": ACCOUNT_ID, "image_id": image_id,
        "actor": ACTOR, "media_root": root, **kwargs,
    })


def first_image(row):
    return row["payload_jsonb"]["draft_media"]["images"][0]


def rejects(code, call):
    with pytest.raises(HTTPException) as error:
        call()
    assert error.value.status_code == code
    return error.value


@pytest.mark.parametrize("fmt,extension,mime", [
    ("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg"), ("WEBP", "webp", "image/webp"),
])
def test_actual_decoded_original_image_roundtrip(media, store, tmp_path, fmt, extension, mime):
    content = image_bytes(fmt)
    row = save(media, store, tmp_path, content)
    entry = first_image(row)
    assert row == task(store)
    assert row["payload_jsonb"]["draft_media"]["status"] == "ready"
    assert entry["status"] == "attached"
    assert str(UUID(entry["id"])) == entry["id"]
    assert entry["sha256"] == sha256(content).hexdigest()
    assert (entry["width"], entry["height"], entry["bytes"]) == (3, 2, len(content))
    assert entry["source"] == "manual_upload"
    assert entry["attached_by"] == ACTOR.user_id
    assert entry["extension"] == extension and entry["content_type"] == mime
    path, content_type = read(media, store, tmp_path, entry["id"])
    assert path == tmp_path / "drafts" / ACCOUNT_ID / TASK_ID / (entry["sha256"] + "." + extension)
    assert content_type == mime and path.read_bytes() == content
    assert list(path.parent.iterdir()) == [path]
    assert row["payload_jsonb"]["reference_images"] == ["competitor.png"]
    assert len(row["payload_jsonb"]["draft_media"]["images"]) == 1


def test_repeated_content_is_idempotent_even_after_approval(media, store, tmp_path):
    row = save(media, store, tmp_path)
    task(store)["status"] = "approved"
    task(store)["review_jsonb"] = {"approved_by": "reviewer", "approved_at": "today"}
    before = deepcopy(store.tables)
    repeated = save(media, store, tmp_path)
    assert first_image(repeated)["id"] == first_image(row)["id"]
    assert repeated["status"] == "approved"
    assert store.tables == before


def test_deterministic_ids_are_scoped_to_account_and_task(media, store, tmp_path):
    original = first_image(save(media, store, tmp_path))["id"]
    task(store)["payload_jsonb"].pop("draft_media")
    assert first_image(save(media, store, tmp_path))["id"] == original
    task(store)["id"] = OTHER_ID
    task(store)["payload_jsonb"].pop("draft_media")
    assert first_image(save(media, store, tmp_path, task_id=OTHER_ID))["id"] != original
    task(store)["account_id"] = OTHER_ID
    task(store)["payload_jsonb"] = {"channel_account_id": OTHER_ID}
    assert first_image(save(media, store, tmp_path, task_id=OTHER_ID, account_id=OTHER_ID))["id"] != original


@pytest.mark.parametrize("content", [b"", b"not an image", b"\x89PNG\r\n\x1a\nforged",
                                       b"\xff\xd8\xffforged", b"RIFFxxxxWEBPforged"])
def test_invalid_signatures_or_undecodable_files_never_write(media, store, tmp_path, content):
    before = deepcopy(store.tables)
    rejects(422, lambda: save(media, store, tmp_path, content))
    assert store.tables == before
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
def test_decodable_unsupported_format_rejected(media, store, tmp_path, fmt):
    rejects(422, lambda: save(media, store, tmp_path, image_bytes(fmt)))
    assert "draft_media" not in task(store)["payload_jsonb"]


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_truncated_real_image_rejected(media, store, tmp_path, fmt):
    content = image_bytes(fmt)
    rejects(422, lambda: save(media, store, tmp_path, content[:len(content) // 2]))
    assert list(tmp_path.iterdir()) == []


def test_pixel_bomb_rejected(media, store, tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 2)
    rejects(422, lambda: save(media, store, tmp_path))
    assert "draft_media" not in task(store)["payload_jsonb"]


def test_ten_mib_boundary(media, store, tmp_path):
    content = image_bytes()
    exact = content + b"\0" * (LIMIT - len(content))
    row = save(media, store, tmp_path, exact)
    assert first_image(row)["bytes"] == LIMIT
    before = deepcopy(store.tables)
    rejects(413, lambda: save(media, store, tmp_path, exact + b"x"))
    assert store.tables == before


def test_nine_active_images_limit_with_dedup_and_detach(media, store, tmp_path):
    for i in range(9):
        row = save(media, store, tmp_path, image_bytes(color=(i, 90, 140)))
    assert len(row["payload_jsonb"]["draft_media"]["images"]) == 9
    before = deepcopy(store.tables)
    rejects(409, lambda: save(media, store, tmp_path, image_bytes(color=(9, 90, 140))))
    assert store.tables == before
    assert save(media, store, tmp_path, image_bytes(color=(0, 90, 140))) == task(store)
    detach(media, store, tmp_path, first_image(row)["id"])
    row = save(media, store, tmp_path, image_bytes(color=(9, 90, 140)))
    assert sum(i["status"] == "attached" for i in row["payload_jsonb"]["draft_media"]["images"]) == 9
    rejects(409, lambda: save(media, store, tmp_path, image_bytes(color=(0, 90, 140))))


@pytest.mark.parametrize("operation,field", [
    (operation, field) for operation in ("save", "read", "detach")
    for field in ("task_id", "account_id", "image_id") if operation != "save" or field != "image_id"
])
@pytest.mark.parametrize("value", ["../outside", "..\\outside", "%2e%2e", "not-a-uuid"])
def test_unsafe_identifiers_rejected(media, store, tmp_path, operation, field, value):
    before = deepcopy(store.tables)
    kwargs = {field: value}
    if operation == "save":
        call = lambda: save(media, store, tmp_path, **kwargs)
    else:
        image_id = kwargs.pop("image_id", OTHER_ID)
        func = read if operation == "read" else detach
        call = lambda: func(media, store, tmp_path, image_id, **kwargs)
    rejects(422, call)
    assert store.tables == before and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("operation", ["save", "read", "detach"])
def test_foreign_or_missing_task_rejected(media, store, tmp_path, operation):
    for kwargs in ({"account_id": OTHER_ID}, {"task_id": OTHER_ID}):
        before = deepcopy(store.tables)
        func = save if operation == "save" else read if operation == "read" else detach
        args = () if operation == "save" else (OTHER_ID,)
        rejects(404, lambda: func(media, store, tmp_path, *args, **kwargs))
        assert store.tables == before


@pytest.mark.parametrize("changes", [
    {"deleted_at": "today"}, {"account_id": OTHER_ID},
    {"payload_jsonb": {"channel_account_id": OTHER_ID}},
])
def test_deleted_or_conflicting_account_scopes_rejected(media, store, tmp_path, changes):
    task(store).update(changes)
    before = deepcopy(store.tables)
    rejects(404, lambda: save(media, store, tmp_path))
    assert store.tables == before and list(tmp_path.iterdir()) == []


def test_legacy_payload_account_scope_supported(media, store, tmp_path):
    task(store).pop("account_id")
    assert first_image(save(media, store, tmp_path))["status"] == "attached"


@pytest.mark.parametrize("role", ["viewer", "service", "unknown"])
@pytest.mark.parametrize("operation", ["save", "detach"])
def test_role_restrictions(media, store, tmp_path, role, operation):
    actor = Actor(user_id="test", role=role)
    func = save if operation == "save" else detach
    args = () if operation == "save" else (OTHER_ID,)
    rejects(403, lambda: func(media, store, tmp_path, *args, actor=actor))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("role", ["admin", "operator", "reviewer"])
def test_existing_editor_roles_allowed(media, store, tmp_path, role):
    assert first_image(save(media, store, tmp_path, actor=Actor(user_id="test", role=role)))["status"] == "attached"


@pytest.mark.parametrize("state", ["publishing", "published", "feedback_pending", "running", "completed"])
@pytest.mark.parametrize("operation", ["save", "detach"])
def test_non_draft_states_reject_mutation(media, store, tmp_path, state, operation):
    entry = first_image(save(media, store, tmp_path))
    task(store)["status"] = state
    before = deepcopy(store.tables)
    func = save if operation == "save" else detach
    args = () if operation == "save" else (entry["id"],)
    rejects(409, lambda: func(media, store, tmp_path, *args))
    assert store.tables == before


@pytest.mark.parametrize("marker", [
    {"published_at": "2026-10-06T00:00:00Z"},
    {"publish_jsonb": {"identity": {"remote_post_id": "published-note"}}},
    {"publish_jsonb": {"manual_publication": {"ownership": "user_confirmed"}}},
])
def test_published_provenance_blocks_draft_mutation(media, store, tmp_path, marker):
    task(store).update(marker)
    rejects(409, lambda: save(media, store, tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("state", ["queued", "intel_ready", "pending_review", "review_rejected", "approved", "publish_failed"])
def test_allowed_draft_states_and_review_invalidation(media, store, tmp_path, state):
    task(store).update({"status": state, "stage": state, "review_jsonb": {
        "approved_by": "reviewer", "approved_at": "today", "comment": "Keep review history",
    }})
    task(store)["payload_jsonb"]["analysis_jsonb"] = {"quality_gate": {"ok": True}, "other": "keep"}
    row = save(media, store, tmp_path)
    assert row["status"] == ("pending_review" if state in {"approved", "review_rejected", "publish_failed"} else state)
    if row["status"] == "pending_review":
        assert row["stage"] == "review"
    assert "approved_at" not in row["review_jsonb"] and "approved_by" not in row["review_jsonb"]
    assert row["review_jsonb"]["comment"] == "Keep review history"
    analysis = row["payload_jsonb"]["analysis_jsonb"]
    assert analysis["review_required_after_edit"] is True and analysis["other"] == "keep"
    assert "quality_gate" not in analysis
    audit = row["payload_jsonb"]["draft_media"]["audit"][-1]
    assert audit["action"] == "attach" and audit["actor"] == ACTOR.user_id


def test_detach_reversible_idempotent_and_invalidates_approval(media, store, tmp_path):
    row = save(media, store, tmp_path)
    entry = first_image(row)
    path, _ = read(media, store, tmp_path, entry["id"])
    task(store).update({"status": "approved", "review_jsonb": {"approved_at": "today", "approved_by": "reviewer"}})
    detached = detach(media, store, tmp_path, entry["id"])
    assert detached["status"] == "pending_review" and detached["stage"] == "review"
    assert "approved_by" not in detached["review_jsonb"]
    assert first_image(detached)["status"] == "detached"
    assert first_image(detached)["detached_by"] == ACTOR.user_id
    assert first_image(detached)["detached_at"]
    assert detached["payload_jsonb"]["draft_media"]["status"] == "awaiting_upload"
    assert path.is_file()
    rejects(404, lambda: read(media, store, tmp_path, entry["id"]))
    before = deepcopy(store.tables)
    assert detach(media, store, tmp_path, entry["id"]) == task(store)
    assert store.tables == before
    restored = save(media, store, tmp_path)
    assert first_image(restored)["id"] == entry["id"]
    assert first_image(restored)["status"] == "attached"
    assert len(restored["payload_jsonb"]["draft_media"]["images"]) == 1
    assert [i["action"] for i in restored["payload_jsonb"]["draft_media"]["audit"]] == ["attach", "detach", "restore"]
    assert read(media, store, tmp_path, entry["id"])[0] == path


def test_unknown_file_and_attachment_never_served_or_deleted(media, store, tmp_path):
    row = save(media, store, tmp_path)
    directory = read(media, store, tmp_path, first_image(row)["id"])[0].parent
    unknown = directory / "unknown.png"
    unknown.write_bytes(image_bytes())
    rejects(404, lambda: read(media, store, tmp_path, OTHER_ID))
    rejects(404, lambda: detach(media, store, tmp_path, OTHER_ID))
    assert unknown.is_file()


def test_missing_file_is_not_ready_read_and_can_reupload_or_detach(media, store, tmp_path):
    entry = first_image(save(media, store, tmp_path))
    path, _ = read(media, store, tmp_path, entry["id"])
    path.unlink()
    rejects(404, lambda: read(media, store, tmp_path, entry["id"]))
    assert first_image(save(media, store, tmp_path))["id"] == entry["id"]
    assert path.read_bytes() == image_bytes()
    path.unlink()
    assert first_image(detach(media, store, tmp_path, entry["id"]))["status"] == "detached"


@pytest.mark.parametrize("field,value", [
    ("sha256", "../outside"), ("extension", "../../png"), ("content_type", "text/html"),
    ("source", "captured_reference"), ("id", OTHER_ID),
])
def test_untrusted_attachment_metadata_rejected(media, store, tmp_path, field, value):
    entry = first_image(save(media, store, tmp_path))
    first_image(task(store))[field] = value
    rejects(409, lambda: read(media, store, tmp_path, value if field == "id" else entry["id"]))


def test_corrupt_existing_file_is_never_overwritten_or_served(media, store, tmp_path):
    entry = first_image(save(media, store, tmp_path))
    path, _ = read(media, store, tmp_path, entry["id"])
    path.write_bytes(b"corrupted")
    before = deepcopy(store.tables)
    rejects(409, lambda: read(media, store, tmp_path, entry["id"]))
    rejects(409, lambda: save(media, store, tmp_path))
    assert path.read_bytes() == b"corrupted" and store.tables == before


def test_symlink_namespace_denied(media, store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / "drafts").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    rejects(422, lambda: save(media, store, tmp_path))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("mutation", ["text", "status", "stage", "account", "review"])
def test_conditional_write_cannot_clobber_concurrent_changes(media, store, tmp_path, mutation):
    def hook(name, query, execute):
        if name == "pipeline_tasks" and query.action == "update":
            if mutation == "text":
                task(store)["payload_jsonb"]["body"] = "Concurrent edit"
            elif mutation == "status":
                task(store)["status"] = "publishing"
            elif mutation == "stage":
                task(store)["stage"] = "executing"
            elif mutation == "account":
                task(store)["account_id"] = OTHER_ID
            else:
                task(store)["review_jsonb"] = {"approved_by": "other"}
        return execute()
    store.hook = hook
    rejects(409, lambda: save(media, store, tmp_path))
    assert "draft_media" not in task(store)["payload_jsonb"]
    assert len(list(tmp_path.rglob("*.png"))) == 1
    assert list(tmp_path.rglob("*.tmp")) == []


@pytest.mark.parametrize("outcome", ["empty", "timeout_after_commit", "timeout_before_commit", "unknown"])
def test_write_output_requires_confirmed_persistence(media, store, tmp_path, outcome):
    writes = 0
    def hook(name, query, execute):
        nonlocal writes
        if name == "pipeline_tasks" and query.action == "update":
            writes += 1
            if outcome in {"empty", "timeout_after_commit"}:
                execute()
            if outcome == "empty":
                return SimpleNamespace(data=[])
            raise TimeoutError("uncertain write")
        if name == "pipeline_tasks" and writes and outcome == "unknown":
            raise TimeoutError("cannot confirm")
        return execute()
    store.hook = hook
    if outcome in {"empty", "timeout_after_commit"}:
        assert first_image(save(media, store, tmp_path))["status"] == "attached"
    else:
        error = rejects(503, lambda: save(media, store, tmp_path))
        assert "confirm" in str(error.detail).lower()
        assert "draft_media" not in task(store)["payload_jsonb"]
    assert writes == 1
    assert len(list(tmp_path.rglob("*.png"))) == 1
    assert list(tmp_path.rglob("*.tmp")) == []
    store.hook = None
    retry = save(media, store, tmp_path)
    assert len(retry["payload_jsonb"]["draft_media"]["images"]) == 1


def test_detach_timeout_after_commit_is_confirmed_without_purge(media, store, tmp_path):
    entry = first_image(save(media, store, tmp_path))
    path, _ = read(media, store, tmp_path, entry["id"])
    def hook(name, query, execute):
        if name == "pipeline_tasks" and query.action == "update":
            execute()
            raise TimeoutError("lost response")
        return execute()
    store.hook = hook
    assert first_image(detach(media, store, tmp_path, entry["id"]))["status"] == "detached"
    assert path.is_file()


def test_atomic_file_failure_never_attaches_or_leaves_temp(media, store, tmp_path, monkeypatch):
    def failed_replace(self, target):
        raise OSError("disk failure")
    monkeypatch.setattr(Path, "replace", failed_replace)
    before = deepcopy(store.tables)
    rejects(503, lambda: save(media, store, tmp_path))
    assert store.tables == before
    assert list(tmp_path.rglob("*.tmp")) == [] and list(tmp_path.rglob("*.png")) == []


@pytest.mark.parametrize("field,value", [("extension", []), ("status", []), ("width", 99), ("bytes", 999), ("sha256", [])])
def test_reupload_rejects_corrupt_saved_metadata(media, store, tmp_path, field, value):
    save(media, store, tmp_path)
    first_image(task(store))[field] = value
    rejects(409, lambda: save(media, store, tmp_path))


def test_fabricated_write_response_is_not_success(media, store, tmp_path):
    def hook(name, query, execute):
        if name == "pipeline_tasks" and query.action == "update":
            return SimpleNamespace(data=[{**deepcopy(task(store)), **query.payload}])
        return execute()
    store.hook = hook
    rejects(409, lambda: save(media, store, tmp_path))
    assert "draft_media" not in task(store)["payload_jsonb"]


def test_detach_conflict_preserves_attachment_and_bytes(media, store, tmp_path):
    entry = first_image(save(media, store, tmp_path))
    path, _ = read(media, store, tmp_path, entry["id"])
    def hook(name, query, execute):
        if name == "pipeline_tasks" and query.action == "update":
            task(store)["payload_jsonb"]["body"] = "Concurrent body"
        return execute()
    store.hook = hook
    rejects(409, lambda: detach(media, store, tmp_path, entry["id"]))
    assert first_image(task(store))["status"] == "attached" and path.is_file()


def test_published_task_can_preview_existing_attachment(media, store, tmp_path):
    entry = first_image(save(media, store, tmp_path))
    task(store).update({"status": "published", "published_at": "today"})
    assert read(media, store, tmp_path, entry["id"])[0].read_bytes() == image_bytes()


def test_file_symlink_inside_media_root_denied(media, store, tmp_path):
    entry = first_image(save(media, store, tmp_path))
    path, _ = read(media, store, tmp_path, entry["id"])
    target = tmp_path / "other-task.png"
    target.write_bytes(image_bytes())
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    rejects(422, lambda: read(media, store, tmp_path, entry["id"]))
    rejects(422, lambda: save(media, store, tmp_path))
    assert target.read_bytes() == image_bytes()


def test_real_postgrest_client_emits_safe_conditional_json_write(media, store, tmp_path):
    import httpx
    from postgrest import SyncPostgrestClient

    persisted = deepcopy(task(store))
    persisted["payload_jsonb"]["body"] = 'A quoted "draft" with Unicode: \u914d\u56fe'
    old = deepcopy(persisted)
    methods = []

    def respond(request):
        methods.append(request.method)
        assert request.url.path == "/pipeline_tasks"
        assert request.url.params["id"] == "eq." + TASK_ID
        if request.method == "PATCH":
            assert request.url.params["account_id"] == "eq." + ACCOUNT_ID
            assert request.url.params["status"] == "eq.pending_review"
            assert request.url.params["published_at"] == "is.null"
            assert json.loads(request.url.params["payload_jsonb"][3:]) == old["payload_jsonb"]
            assert json.loads(request.url.params["review_jsonb"][3:]) == old["review_jsonb"]
            assert json.loads(request.url.params["publish_jsonb"][3:]) == old["publish_jsonb"]
            persisted.update(json.loads(request.content))
        else:
            assert request.method == "GET"
        return httpx.Response(200, json=[deepcopy(persisted)])

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = SyncPostgrestClient("https://database.invalid", http_client=transport)
        assert save(media, client, tmp_path) == persisted
    assert methods == ["GET", "PATCH", "GET"]


def test_large_real_draft_uses_bounded_conditional_filter(media, store, tmp_path):
    import httpx
    from postgrest import SyncPostgrestClient
    persisted = deepcopy(task(store))
    persisted['payload_jsonb']['body'] = 'Long draft ' * 5000
    old_stamp = persisted['updated_at']
    def respond(request):
        if request.method == 'PATCH':
            assert len(str(request.url)) < 8000
            assert request.url.params['updated_at'] == 'eq.' + old_stamp
            assert 'payload_jsonb' not in request.url.params
            persisted.update(json.loads(request.content))
        return httpx.Response(200, json=[deepcopy(persisted)])
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = SyncPostgrestClient('https://database.invalid', http_client=transport)
        assert save(media, client, tmp_path) == persisted
