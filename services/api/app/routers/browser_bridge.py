from __future__ import annotations

from datetime import datetime
from copy import deepcopy
from hashlib import sha256
import json
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from supabase import Client

from app.config import get_settings
from app.db import get_supabase
from app.models import Actor, BrowserIntelItem, BrowserIntelImportRequest
from app.security import get_actor, require_roles
from app.services.browser_bridge import EXTENSION_ORIGIN, bridge_store
from app.services.browser_intel import import_browser_intel
from app.services.browser_capture import PublicCapture, persist_public_capture, update_capture_metadata
from app.services.browser_media import item_capture, read_account_media, save_public_image

async def bridge_gateway(request: Request):
    path = request.url.path
    if "/accounts/" in path:
        actor = get_actor(request.headers.get("x-user-id"), request.headers.get("x-user-role"))
        if actor.role not in {"admin", "operator"}:
            raise HTTPException(403, "此账号操作需要管理员或运营权限")
        local_control(request)
    elif path.endswith(("/extension/pair", "/extension/pair-info")):
        code = request.headers.get("x-pairing-code", "")
        if not 10 <= len(code) <= 80:
            raise HTTPException(401, "配对授权缺失")
        bridge_store.pairing_info(code)
        extension_origin(request)
    else:
        bearer(request)
        if "/worker/" in path:
            worker(request, request.path_params.get("job_id", ""))
        else:
            bridge_store.extension_session(bearer(request), extension_origin(request))
    if request.method == "POST" and len(await request.body()) > 800000:
        raise HTTPException(413, "浏览器观察结果过大")


router = APIRouter(prefix="/api/browser-bridge", tags=["browser-bridge"], dependencies=[Depends(bridge_gateway)])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountInput(StrictModel):
    domain_slug: str = Field(min_length=2, max_length=120)
    source_kind: Literal["hotspot", "viewpoint"] = "hotspot"
    mode: Literal["foreground_visual", "background_text"] = "foreground_visual"


class PairInput(StrictModel):
    code: str = Field(min_length=10, max_length=80)
    tab_id: int = Field(ge=0)
    consent: Literal[True]
    protocol_version: int = 1


class PairPreviewInput(StrictModel):
    code: str = Field(min_length=10, max_length=80)


class PauseInput(StrictModel):
    message: str = Field(min_length=1,max_length=300)


class Note(StrictModel):
    source_url: str = Field(max_length=2000)
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(max_length=5000)
    captured_at: datetime

    @field_validator("source_url")
    @classmethod
    def source(cls, value):
        return BrowserIntelItem.public_note_url(value)

    @field_validator("captured_at")
    @classmethod
    def capture_time(cls, value):
        return BrowserIntelItem.observed_time(value)


class ResultTarget(StrictModel):
    target_id: str = Field(min_length=1, max_length=80)
    source_url: str = Field(max_length=2000)
    title: str = Field(max_length=300)

    @field_validator("source_url")
    @classmethod
    def source(cls, value):
        return BrowserIntelItem.public_note_url(value)


class DetailTarget(StrictModel):
    target_id: str = Field(min_length=1,max_length=80)
    operation: Literal["expand_body","expand_reply","next_image"]


class Observation(StrictModel):
    kind: Literal["note", "results"]
    version: str = Field(min_length=1, max_length=80)
    note: Note | None = None
    results: list[ResultTarget] = Field(default_factory=list, max_length=12)
    screenshot: str | None = Field(default=None, max_length=700000, pattern=r"^data:image/jpeg;base64,[A-Za-z0-9+/=]+$")
    capture: PublicCapture | None = None
    detail_targets: list[DetailTarget] = Field(default_factory=list, max_length=30)


class BrowserResult(StrictModel):
    ok: bool
    paused: bool = False
    message: str = Field(default="", max_length=300)
    code: str = Field(default="", max_length=80)
    data: Observation | None = None


class ResultInput(StrictModel):
    command_id: str = Field(min_length=1, max_length=80)
    result: BrowserResult


class OperationInput(StrictModel):
    operation: Literal["observe", "search", "open_note", "scroll_note", "close_note", "read_note", "expand_body", "scroll_comments", "expand_reply", "next_image"]
    arguments: dict = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def bounded_args(cls, value):
        if len(json.dumps(value, ensure_ascii=False)) > 800 or any(not isinstance(v, str) for v in value.values()):
            raise ValueError("浏览器参数无效")
        return value


class SaveInput(StrictModel):
    summary: str | None = Field(default=None, min_length=10, max_length=3000)
    model_name: str = Field(default="", max_length=120)


class SummaryInput(StrictModel):
    summary: str = Field(min_length=10, max_length=3000)
    model_name: str = Field(default="", max_length=120)


class FinishInput(StrictModel):
    status: Literal["completed", "partial", "failed", "model_unavailable"]
    message: str = Field(min_length=1, max_length=300)


def local_client(request: Request):
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "浏览器连接仅允许本机访问")


def local_control(request: Request):
    local_client(request)
    if request.headers.get("origin") not in {None, "http://127.0.0.1:3001", "http://localhost:3001"}:
        raise HTTPException(403, "请通过本机项目页面控制浏览器")


def extension_origin(request: Request):
    local_client(request)
    origin = request.headers.get("x-extension-origin") or request.headers.get("origin") or ""
    if not EXTENSION_ORIGIN.fullmatch(origin) or request.headers.get("origin") not in {None, origin}:
        raise HTTPException(403, "请通过项目 Chrome 插件连接")
    return origin


def bearer(request: Request):
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer ") or len(value) > 200:
        raise HTTPException(401, "浏览器连接授权缺失")
    return value[7:]


def worker(request: Request, job_id: str):
    local_client(request)
    if request.headers.get("origin") is not None:
        raise HTTPException(403, "此接口仅供项目 Agent 使用")
    return bridge_store.worker_job(job_id, bearer(request))


def account_config(client, account_id, domain_slug):
    rows = client.table("channel_accounts").select("id,channel,is_active,config_jsonb").eq("id", account_id).limit(1).execute().data or []
    if not rows or not rows[0]["is_active"] or rows[0]["channel"] != "xiaohongshu":
        raise HTTPException(409, "请选择启用中的小红书账号")
    config = rows[0].get("config_jsonb") or {}
    if (config.get("onboarding") or {}).get("domain_slug") != domain_slug:
        raise HTTPException(409, "请选择账号当前规划的赛道")
    if (config.get("collection_plan") or {}).get("mode") != "browser_ui":
        raise HTTPException(409, "此账号尚未配置浏览器采集方式")
    return config


@router.post("/accounts/{account_id}/pairing", dependencies=[Depends(local_control)])
def create_pairing(account_id: str, body: AccountInput, client: Client = Depends(get_supabase), actor: Actor = Depends(require_roles("admin", "operator"))):
    account_config(client, account_id, body.domain_slug)
    return bridge_store.create_pairing(account_id, body.domain_slug, actor.user_id, body.mode)


@router.get("/accounts/{account_id}/status", dependencies=[Depends(local_control)])
def get_status(account_id: str, _: Actor = Depends(require_roles("admin", "operator"))):
    return bridge_store.status(account_id)


@router.post("/accounts/{account_id}/disconnect", dependencies=[Depends(local_control)])
def disconnect(account_id: str, _: Actor = Depends(require_roles("admin", "operator"))):
    bridge_store.disconnect(account_id)
    return {"connected": False}


@router.post("/extension/pair")
def pair(request: Request, body: PairInput):
    if body.code != request.headers.get("x-pairing-code"):
        raise HTTPException(401, "配对授权不匹配")
    return bridge_store.pair(body.code, extension_origin(request), body.tab_id, body.protocol_version)


@router.post("/extension/pair-info")
def pair_info(request: Request, body: PairPreviewInput):
    if body.code != request.headers.get("x-pairing-code"):
        raise HTTPException(401, "配对授权不匹配")
    extension_origin(request)
    settings = get_settings()
    return {**bridge_store.pairing_info(body.code), "model_name": settings.browser_model_name or settings.model_draft_name,
            "provider": urlsplit(settings.browser_model_base_url or settings.model_api_base_url or "https://api.openai.com/v1").hostname,
            "max_notes": 3, "max_model_calls": 12, "max_comments": 100, "max_images": 24, "max_seconds": 600}


@router.post("/extension/poll")
def poll(request: Request):
    return {"command": bridge_store.lease_command(bearer(request), extension_origin(request))}


@router.post("/extension/pause")
def pause_extension(request: Request,body: PauseInput):
    bridge_store.pause_session(bearer(request),extension_origin(request),body.message)
    return {"paused":True}


@router.post("/extension/results")
def result(request: Request, body: ResultInput):
    bridge_store.complete_command(bearer(request), extension_origin(request), body.command_id, body.result.model_dump(mode="json", exclude_none=True))
    return {"accepted": True}


@router.post("/extension/disconnect")
def extension_disconnect(request: Request):
    session = bridge_store.extension_session(bearer(request), extension_origin(request))
    bridge_store.disconnect(session["account_id"])
    return {"connected": False}


async def start_collection(account_id, body, client):
    config = account_config(client, account_id, body.domain_slug)
    queries = [str(s.get("query") or "").strip()[:160] for s in (config.get("collection_plan") or {}).get("steps", []) if isinstance(s, dict) and s.get("tool") == "browser_ui_search"]
    queries = [q for q in queries if q][:3] or [str(q)[:160] for q in (config.get("strategy_profile") or {}).get("focus_keywords", [])[:3]]
    if not queries:
        raise HTTPException(409, "请先配置账号采集关键词")
    job = bridge_store.start_job(account_id, body.domain_slug, queries, body.source_kind)
    target = get_settings().agent_service_url.rstrip("/")
    if urlsplit(target).hostname not in {"127.0.0.1", "localhost", "::1"}:
        bridge_store.finish(bridge_store.jobs[job["id"]], "failed", "浏览器 Agent 必须运行在本机")
        raise HTTPException(409, "浏览器 Agent 必须运行在本机")
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=15) as http:
            response = await http.post(target + "/browser-collection/start", json={"job_id": job["id"], "worker_token": job["worker_token"]})
            response.raise_for_status()
    except httpx.HTTPError:
        bridge_store.finish(bridge_store.jobs[job["id"]], "failed", "无法启动项目 Agent，请检查服务")
    return {**bridge_store.job_view(job["id"], account_id), "job_id": job["id"]}


@router.post("/accounts/{account_id}/jobs", dependencies=[Depends(local_control)])
async def start(account_id: str, body: AccountInput, client: Client = Depends(get_supabase), _: Actor = Depends(require_roles("admin", "operator"))):
    return await start_collection(account_id, body, client)


@router.get("/accounts/{account_id}/jobs/{job_id}", dependencies=[Depends(local_control)])
def job_status(account_id: str, job_id: str, _: Actor = Depends(require_roles("admin", "operator"))):
    return bridge_store.job_view(job_id, account_id)


@router.post("/accounts/{account_id}/jobs/{job_id}/{action}", dependencies=[Depends(local_control)])
def control_job(account_id: str, job_id: str, action: Literal["stop", "resume"], _: Actor = Depends(require_roles("admin", "operator"))):
    return getattr(bridge_store, action)(job_id, account_id)


@router.get("/worker/{job_id}")
def worker_state(request: Request, job_id: str):
    job = worker(request, job_id)
    return {**bridge_store.job_view(job_id, job["account_id"]), "queries": job["queries"]}


@router.post("/worker/{job_id}/commands")
def worker_command(request: Request, job_id: str, body: OperationInput):
    job = worker(request, job_id)
    fields = {"observe": set(), "search": {"version", "query"}, "open_note": {"version", "target_id"}, "scroll_note": {"version", "direction"}, "close_note": {"version"}, "read_note": {"version"}}
    fields.update({"expand_body": {"version", "target_id"}, "scroll_comments": {"version"}, "expand_reply": {"version", "target_id"}, "next_image": {"version", "target_id"}})
    if set(body.arguments) != fields[body.operation]:
        raise HTTPException(422, "浏览器操作参数不匹配")
    if body.operation == "search" and (not body.arguments["query"].strip() or len(body.arguments["query"]) > 160):
        raise HTTPException(422, "搜索关键词无效")
    if body.operation == "scroll_note" and body.arguments["direction"] not in {"up", "down"}:
        raise HTTPException(422, "滚动方向无效")
    return bridge_store.enqueue(job_id, body.operation, body.arguments)


@router.get("/worker/{job_id}/commands/{command_id}")
def worker_result(request: Request, job_id: str, command_id: str):
    return bridge_store.command_result(worker(request, job_id), command_id)


@router.post("/worker/{job_id}/save")
def save_note(request: Request, job_id: str, body: SaveInput, client: Client = Depends(get_supabase)):
    job = worker(request, job_id)
    # Admission and the write are serialized with stop/disconnect.
    with bridge_store.lock:
        bridge_store.active(job)
        evidence = next((c for c in reversed(list(bridge_store.commands.values())) if c["job_id"] == job_id and c["operation"] == "read_note" and (c.get("result") or {}).get("ok") and ((c.get("result") or {}).get("data") or {}).get("kind") == "note"), None)
        note = ((evidence or {}).get("result") or {}).get("data", {}).get("note")
        if not note or len(note.get("text", "").strip()) < 10:
            raise HTTPException(409, "没有本轮已读取的可见正文，不能保存")
        latest = next((c for c in reversed(list(bridge_store.commands.values())) if c["job_id"] == job_id and ((c.get("result") or {}).get("data") or {}).get("kind")),None)
        latest_data = ((latest or {}).get("result") or {}).get("data") or {}
        if latest_data.get("kind") != "note" or (latest_data.get("note") or {}).get("source_url") != note["source_url"]:
            raise HTTPException(409,"正文来源已变化，请重新读取后保存")
        if note["source_url"] in job["saved_urls"]:
            return {"saved": False, "message": "本轮已保存此笔记"}
        if job["collected"] >= 3:
            raise HTTPException(409, "已达到 3 篇采集上限")
        if job["mode"] == "background_text":
            capture = ((evidence or {}).get("result") or {}).get("data",{}).get("capture")
            if not capture or capture["source_url"] != note["source_url"]:
                raise HTTPException(409,"没有本轮已确认的完整资料快照")
            imported = persist_public_capture(client,job=job,capture=capture,summary=body.summary,model_name=body.model_name,actor=Actor(user_id=job["actor"],role="operator"))
        else:
            if not body.summary:
                raise HTTPException(422,"前台视觉模式需要摘要")
            item = BrowserIntelItem(source_url=note["source_url"], title=note["title"][:200], raw_text=body.summary, query=job["queries"][0], captured_at=note["captured_at"])
            imported = import_browser_intel(client, account_id=job["account_id"],
                request=BrowserIntelImportRequest(domain_slug=job["domain_slug"], source_kind=job["source_kind"], items=[item],
                    reason="项目 Agent 使用当前 Chrome 可见正文采集", collection_method="project_browser_agent", model_name=body.model_name),
                actor=Actor(user_id=job["actor"], role="operator"))
        job["saved_urls"].append(note["source_url"])
        job["collected"] += 1
        job["inserted"] += imported["inserted"]
        job["duplicates"] += imported["duplicates"]
        job["updated"] += imported.get("updated",0)
        if imported.get("item_id"):
            job.setdefault("saved_item_ids",[]).append(imported["item_id"])
        job["events"] = (job["events"] + [f"已核对保存：{note['title'][:80]}"])[-30:]
        return {"saved": True, "collected": job["collected"], "inserted": imported["inserted"], "duplicates": imported["duplicates"], "updated":imported.get("updated",0),"item_id":imported.get("item_id"),"image_indices":[i["index"] for i in capture.get("image_candidates",[])] if job["mode"]=="background_text" else []}


@router.post("/worker/{job_id}/media/{item_id}/{index}")
def worker_media(request: Request,job_id: str,item_id: str,index: int,client: Client = Depends(get_supabase)):
    job = worker(request,job_id)
    def active():
        with bridge_store.lock:
            try:
                bridge_store.active(job)
                return True
            except HTTPException:
                return False
    with bridge_store.lock:
        bridge_store.active(job)
    return save_public_image(client,job=job,item_id=item_id,index=index,should_continue=active,lock=bridge_store.lock)


@router.post("/worker/{job_id}/summary/{item_id}")
def worker_summary(request: Request,job_id: str,item_id: str,body: SummaryInput,client: Client = Depends(get_supabase)):
    job = worker(request,job_id)
    with bridge_store.lock:
        bridge_store.active(job)
        if job["mode"] != "background_text" or item_id not in job.get("saved_item_ids",[]):
            raise HTTPException(403,"只能补充本轮已确认原文的摘要")
        row = item_capture(client,job["account_id"],item_id)
        meta = deepcopy(row["meta_jsonb"])
        capture = meta["browser_capture"]
        previous_revision = capture.get("revision")
        capture.update(summary=body.summary,summary_status="available",model_name=body.model_name)
        capture["revision"] = sha256(json.dumps(capture,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        placeholder = capture["title"][:200]+"\n公开原文已保存，模型摘要暂未生成，请查看原文。"
        raw = capture["title"][:200]+"\n"+body.summary if row.get("raw_text") == placeholder and meta.get("collection_method") == "project_browser_agent" else None
        update_capture_metadata(client,account_id=job["account_id"],item_id=item_id,meta=meta,revision=capture["revision"],expected_revision=previous_revision,raw_text=raw)
        return {"updated":True,"item_id":item_id}


@router.get("/accounts/{account_id}/items/{item_id}/capture",dependencies=[Depends(local_control)])
def account_capture(account_id: str,item_id: str,client: Client = Depends(get_supabase),_: Actor = Depends(require_roles("admin","operator"))):
    row = item_capture(client,account_id,item_id)
    capture = deepcopy(row["meta_jsonb"]["browser_capture"])
    candidates = capture.pop("image_candidates",[])
    images = {i["index"]:i for i in capture.get("images",[])}
    capture["images"] = [images.get(i["index"],{"index":i["index"],"status":"pending","message":"尚未确认本机文件已保存"}) for i in candidates]
    for image in capture["images"]:
        image.pop("candidate_url",None)
    return capture


@router.get("/accounts/{account_id}/items/{item_id}/media/{media_id}",dependencies=[Depends(local_control)])
def account_media(account_id: str,item_id: str,media_id: str,client: Client = Depends(get_supabase),actor: Actor = Depends(require_roles("admin","operator"))):
    image = read_account_media(client,account_id=account_id,item_id=item_id,media_id=media_id,actor=actor)
    return FileResponse(image["path"],media_type=image["content_type"],headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})


@router.post("/worker/{job_id}/finish")
def finish(request: Request, job_id: str, body: FinishInput):
    job = worker(request, job_id)
    status = body.status
    if status == "completed" and not job["collected"]:
        status = "failed"
    return bridge_store.finish(job, status, body.message)
