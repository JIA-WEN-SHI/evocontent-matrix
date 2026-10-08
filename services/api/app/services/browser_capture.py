from datetime import datetime
from copy import deepcopy
from hashlib import sha256
import json
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import HTTPException
from postgrest.exceptions import APIError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import Actor, AuditLogEntry, BrowserIntelItem, BrowserIntelImportRequest
from app.services.audit import write_audit_log
from app.services.browser_intel import import_browser_intel


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicComment(EvidenceModel):
    key: str = Field(min_length=1, max_length=120)
    parent_key: str | None = Field(default=None, max_length=120)
    text: str = Field(min_length=1, max_length=2000)
    author_name: str = Field(default="", max_length=200)
    observed_time: str = Field(default="", max_length=100)
    truncated: bool = False


class ImageCandidate(EvidenceModel):
    key: str = Field(min_length=1, max_length=120)
    index: int = Field(ge=0, le=100)
    url: str = Field(min_length=1, max_length=2000)


class PublicCapture(EvidenceModel):
    schema_version: Literal[1] = 1
    source_url: str = Field(max_length=2000)
    title: str = Field(min_length=1, max_length=300)
    body_text: str = Field(max_length=50000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    author_name: str = Field(default="", max_length=200)
    comments: list[PublicComment] = Field(default_factory=list, max_length=100)
    image_candidates: list[ImageCandidate] = Field(default_factory=list, max_length=24)
    body_status: Literal["complete", "partial", "unsupported"] = "partial"
    comments_status: Literal["complete", "partial", "unsupported", "empty", "failed"] = "partial"
    images_status: Literal["complete", "partial", "unsupported", "empty"] = "partial"
    truncation_reasons: list[str] = Field(default_factory=list, max_length=20)
    captured_at: datetime

    @field_validator("source_url")
    @classmethod
    def source(cls, value):
        return BrowserIntelItem.public_note_url(value)

    @field_validator("captured_at")
    @classmethod
    def time(cls, value):
        return BrowserIntelItem.observed_time(value)

    @field_validator("tags", "truncation_reasons")
    @classmethod
    def bounded_strings(cls, values):
        if any(len(v) > 200 for v in values):
            raise ValueError("公开资料标签或原因过长")
        return values


def merge_public_capture(previous: dict | None, incoming: dict) -> dict:
    result = PublicCapture(**incoming).model_dump(mode="json")
    if previous and previous.get("source_url") != result["source_url"]:
        raise ValueError("不能合并不同笔记的公开资料")
    comments = {(i["key"],i.get("parent_key")):i for i in result["comments"]}
    for item in (previous or {}).get("comments",[]):
        comments.setdefault((item["key"], item.get("parent_key")), item)
    result["comments"] = list(comments.values())[:100]
    images = {i["index"]:i for i in (previous or {}).get("image_candidates",[])}
    images.update({i["index"]:i for i in result["image_candidates"]})
    result["image_candidates"] = list(images.values())[:24]
    result["image_candidates"].sort(key=lambda i: i["index"])
    if previous:
        result["previous_captured_at"] = previous.get("captured_at")
        retained = False
        if result["body_status"] != "complete" and len(previous.get("body_text","")) > len(result["body_text"]):
            result["body_text"] = previous["body_text"]
            retained = True
        for field,status in [("comments","comments_status"),("image_candidates","images_status")]:
            if any(i["key"] not in {j["key"] for j in incoming.get(field,[])} for i in result[field]):
                if result[status] != "failed":
                    result[status] = "partial"
                retained = True
        if retained:
            result["truncation_reasons"] = (result["truncation_reasons"]+["保留先前确认的资料，本轮未重新读取完整"])[-20:]
    return result


def capture_snapshot(previous, checked, summary, model_name, job, actor):
    snapshot = merge_public_capture(previous,checked)
    summary = summary or (previous or {}).get("summary")
    snapshot.update(summary=summary,summary_status="available" if summary else "unavailable",model_name=model_name,mode=job["mode"],captured_by=actor.user_id,collection_method=job.get("collection_method", "project_browser_agent"),images=deepcopy((previous or {}).get("images",[])))
    snapshot["revision"] = sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return snapshot


def scoped_item(client, job, item_id=None):
    domains = client.table("domains").select("id").eq("slug",job["domain_slug"]).limit(1).execute().data or []
    if not domains:
        raise HTTPException(404,"赛道不存在")
    provider = "xiaohongshu_cli" if job.get("collection_method") == "xiaohongshu_cli" else "chrome_browser_ui"
    query = client.table("intelligence_items").select("*").eq("account_id",job["account_id"]).eq("domain_id",domains[0]["id"]).eq("source_type",f"{job['source_kind']}_{provider}")
    query = query.eq("id",item_id) if item_id else query.eq("source_url",job["source_url"])
    return query.limit(1).execute().data or []


def guard_capture_metadata(query, meta):
    guards = {
        "meta_jsonb->>content_revision": meta.get("content_revision"),
        "meta_jsonb->browser_capture->>revision": (meta.get("browser_capture") or {}).get("revision"),
        "meta_jsonb->knowledge_links->>linked_at": (meta.get("knowledge_links") or {}).get("linked_at"),
    }
    for key, value in guards.items():
        if value is not None and (not isinstance(value, str) or len(value) > 128):
            raise HTTPException(409, "资料版本信息无效，请刷新核对")
        query = query.is_(key, "null") if value is None else query.eq(key, value)
    return query


def update_capture_metadata(client, *, account_id, item_id, meta, revision, expected_revision, raw_text=None):
    rows = client.table("intelligence_items").select("*").eq("id",item_id).eq("account_id",account_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, "采集资料不存在")
    previous = rows[0].get("meta_jsonb") or {}
    if (previous.get("browser_capture") or {}).get("revision") != expected_revision:
        raise HTTPException(409, "原采集版本已变化，请刷新核对；未覆盖新评论或图片")
    merged = {**deepcopy(previous), **deepcopy(meta)}
    # Knowledge links are owned by the linker; capture saves cannot replace them.
    if "knowledge_links" in previous:
        merged["knowledge_links"] = deepcopy(previous["knowledge_links"])
    else:
        merged.pop("knowledge_links", None)
    merged["content_revision"] = uuid4().hex
    error = None
    try:
        values = {"meta_jsonb":merged}
        if raw_text is not None:
            values["raw_text"] = raw_text
        query = client.table("intelligence_items").update(values).eq("id",item_id).eq("account_id",account_id)
        saved = guard_capture_metadata(query, previous).execute().data or []
        if not saved:
            raise HTTPException(409, "采集资料已被更新，请刷新核对；未覆盖新资料")
    except (APIError,httpx.HTTPError,TimeoutError,ConnectionError) as exc:
        error = exc
    for _ in range(2):
        try:
            rows = client.table("intelligence_items").select("*").eq("id",item_id).eq("account_id",account_id).limit(1).execute().data or []
            if (rows and (rows[0].get("meta_jsonb") or {}).get("content_revision") == merged["content_revision"]
                    and (rows[0].get("meta_jsonb") or {}).get("browser_capture",{}).get("revision") == revision
                    and (raw_text is None or rows[0].get("raw_text") == raw_text)):
                return rows[0]
        except (APIError,httpx.HTTPError,TimeoutError,ConnectionError):
            continue
    if error:
        raise error
    raise HTTPException(503,"资料保存结果未确认，请刷新核对，勿重复提交")


def persist_public_capture(client, *, job: dict, capture: dict, summary: str | None, model_name: str, actor: Actor) -> dict:
    checked = PublicCapture(**capture).model_dump(mode="json")
    accounts = client.table("channel_accounts").select("*").eq("id",job["account_id"]).limit(1).execute().data or []
    if not accounts or accounts[0].get("channel") != "xiaohongshu" or not accounts[0].get("is_active"):
        raise HTTPException(409,"请选择启用中的小红书账号")
    configured = ((accounts[0].get("config_jsonb") or {}).get("onboarding") or {}).get("domain_slug")
    if configured and configured != job["domain_slug"]:
        raise HTTPException(409,"账号赛道已变化")
    scope = {**job,"source_url":checked["source_url"]}
    is_cli = job.get("collection_method") == "xiaohongshu_cli"
    rows = scoped_item(client,scope)
    inserted = 0
    if not rows:
        snapshot = capture_snapshot(None,checked,summary,model_name,job,actor)
        raw_text = (checked["title"] + "\n" + checked["body_text"])[:5000] if is_cli else summary or "公开原文已保存，模型摘要暂未生成，请查看原文。"
        if len(raw_text) < 10:
            raw_text += "\n公开笔记原文"
        request = BrowserIntelImportRequest(domain_slug=job["domain_slug"],source_kind=job["source_kind"],collection_method=job.get("collection_method", "project_browser_agent"),model_name=model_name,
            reason="小红书 CLI 公开资料采集" if is_cli else "后台公开页面资料采集",items=[BrowserIntelItem(source_url=checked["source_url"],title=checked["title"][:200],raw_text=raw_text,captured_at=checked["captured_at"],query=job["queries"][0])])
        receipt = import_browser_intel(client,account_id=job["account_id"],request=request,actor=actor,capture_snapshots={checked["source_url"]:snapshot})
        inserted = receipt["inserted"]
        rows = scoped_item(client,scope)
    if not rows:
        raise HTTPException(503,"未确认原文记录已建立")
    row = rows[0]
    meta = deepcopy(row.get("meta_jsonb") or {})
    if not inserted:
        snapshot = capture_snapshot(meta.get("browser_capture"),checked,summary,model_name,job,actor)
        meta["browser_capture"] = snapshot
        row = update_capture_metadata(client,account_id=job["account_id"],item_id=row["id"],meta=meta,revision=snapshot["revision"],expected_revision=(row.get("meta_jsonb",{}).get("browser_capture") or {}).get("revision"))
        meta = deepcopy(row.get("meta_jsonb") or {})
    elif meta.get("browser_capture",{}).get("revision") != snapshot["revision"]:
        raise HTTPException(503,"初次原文保存结果未确认，请刷新核对")
    if is_cli:
        update_capture_metadata(client,account_id=job["account_id"],item_id=row["id"],meta=meta,revision=snapshot["revision"],expected_revision=(row.get("meta_jsonb",{}).get("browser_capture") or {}).get("revision"),raw_text=checked["title"]+"\n"+snapshot["body_text"])
    write_audit_log(client,AuditLogEntry(actor=actor.user_id,action="account.browser_capture_saved",target_type="intelligence_item",target_id=row["id"],diff_jsonb={"account_id":job["account_id"],"source_url":checked["source_url"],"mode":job["mode"],"comments":len(snapshot["comments"]),"image_candidates":len(snapshot["image_candidates"])}))
    return {"saved":True,"item_id":row["id"],"inserted":inserted,"duplicates":0 if inserted else 1,"updated":0 if inserted else 1,"capture_status":snapshot["comments_status"]}
