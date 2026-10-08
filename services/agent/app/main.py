from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings
from app.db import get_supabase
from app.error_handling import install_error_handlers
from app.model_router import get_router
from app.agents.browser.router import router as browser_collection_router
from app.agents.assistant.orchestrator import run_assistant_chat, run_task_plan, run_task_plan_confirm
from app.agents.coach.orchestrator import run_coach_brief, run_coach_chat, run_coach_confirm_action
from app.orchestration.daily_ops import daily_ops_uses_cli, get_daily_ops_reports, get_daily_ops_status, run_account_collection, run_daily_ops_with_retry
from app.orchestration.daily_scheduler import get_scheduler_runtime_status, start_daily_scheduler, stop_daily_scheduler
from app.orchestration.pipeline_runner import run_pipeline_task as run_pipeline_task_service
from app.orchestration.publish_feedback import reconcile_publish_feedback
from app.orchestration.reflection import reflect_and_upgrade
from app.runtime.accounts.account_prompt_versions import get_active_prompt_text
from app.runtime.accounts.account_runtime import cleanup_idle_account_runtime, snapshot_account_runtime
from app.tools.collection.playwright_tools import run_playwright_tool
from app.tools.integrations.xhs_mcp_readonly import (
    collect_intel_bundle_via_mcp,
    custom_call_write_via_mcp,
    custom_call_readonly_via_mcp,
    get_mcp_readonly_status,
    get_mcp_write_status,
    publish_content_via_mcp,
    search_readonly_via_mcp,
    sync_recent_published_metrics_via_mcp,
)

app = FastAPI(title="EvoContent Agent", version="0.1.0")
app.include_router(browser_collection_router)
_PLAYWRIGHT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright-runtime")


def _run_playwright_serial(func, *args, **kwargs):
    future = _PLAYWRIGHT_EXECUTOR.submit(func, *args, **kwargs)
    return future.result()


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    triggered_by: str = Field(default="api:assistant")
    auto_execute: bool = True
    conversation_history: list[dict] = Field(default_factory=list)


class CoachChatRequest(BaseModel):
    message: str = Field(default="", max_length=4000)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    triggered_by: str = Field(default="api:coach")
    conversation_history: list[dict] = Field(default_factory=list)


class CoachConfirmActionRequest(BaseModel):
    action_id: str = Field(min_length=8, max_length=120)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    confirm: bool = True
    force_reexecute: bool = False
    triggered_by: str = Field(default="api:coach_confirm", max_length=160)


class McpReadonlySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    account_id: str = Field(default="", max_length=120)
    limit: int = Field(default=8, ge=1, le=50)
    source_kind: str = Field(default="hotspot", min_length=2, max_length=40)


class McpReadonlySyncMetricsRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    limit: int = Field(default=10, ge=1, le=50)


class McpReadonlyCollectIntelRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    query: str = Field(default="japan immigration", min_length=1, max_length=300)
    account_id: str = Field(default="", max_length=120)
    limit: int = Field(default=12, ge=1, le=100)
    include_home: bool = True
    include_search: bool = True
    include_detail_metrics: bool = False
    persist: bool = True
    tool_profile: dict = Field(default_factory=dict)


class McpReadonlyCustomCallRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=120)
    arguments: dict = Field(default_factory=dict)
    source_kind: str = Field(default="hotspot", min_length=2, max_length=40)
    limit: int = Field(default=20, ge=1, le=100)


class McpWriteCustomCallRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=120)
    arguments: dict = Field(default_factory=dict)


class McpWritePublishRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=20000)
    images: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    schedule_at: str = Field(default="", max_length=80)
    video: str = Field(default="", max_length=1000)


class HotPostAnalyzeItem(BaseModel):
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=20000)
    source_url: str = Field(default="", max_length=1000)


class HotPostAnalyzeRequest(BaseModel):
    items: list[HotPostAnalyzeItem] = Field(default_factory=list, max_length=20)
    analysis_prompt: str = Field(default="", max_length=12000)
    account_id: str = Field(default="", max_length=120)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)


class RebuildXhsContentRequest(BaseModel):
    competitor_analysis: list[dict] = Field(default_factory=list, max_length=30)
    ip_positioning: str = Field(default="", max_length=3000)
    weekly_keywords: list[str] = Field(default_factory=list, max_length=20)
    tone_style: str = Field(default="", max_length=800)
    strategy_prompt: str = Field(default="", max_length=20000)
    copy_prompt: str = Field(default="", max_length=20000)
    account_id: str = Field(default="", max_length=120)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)


class ChiefEvolutionRequest(BaseModel):
    competitor_dataset: list[dict] = Field(default_factory=list, max_length=400)
    our_history: list[dict] = Field(default_factory=list, max_length=400)
    current_system_version: dict = Field(default_factory=dict)
    milestone_goal: str = Field(default="", max_length=2000)


class AccountCollectionRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(min_length=1, max_length=120)
    source_kind: str = Field(default="hotspot", min_length=2, max_length=40)
    triggered_by: str = Field(default="api:manual", max_length=120)


class PlaywrightToolRunRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    tool_name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)


class PublishFeedbackReconcileRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    pipeline_task_id: str = Field(default="", max_length=120)
    limit: int = Field(default=20, ge=1, le=50)


class TaskPlanConfirmRequest(BaseModel):
    task_plan: dict = Field(default_factory=dict)
    confirm_text: str = Field(default="确认执行", max_length=120)
    second_confirm_text: str = Field(default="", max_length=120)
    actor: str = Field(default="api:assistant", max_length=160)


class TaskPlanRunRequest(BaseModel):
    task_plan: dict = Field(default_factory=dict)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    triggered_by: str = Field(default="api:task_plan", max_length=160)


def _local_client(request: Request) -> bool:
    host = str((request.client.host if request.client else "") or "").strip().lower()
    return host in {"127.0.0.1", "::1", "localhost"}


@app.middleware("http")
async def internal_gateway_guard(request: Request, call_next):
    # Keep local development and local API->Agent calls smooth.
    if request.url.path == "/health" or _local_client(request):
        return await call_next(request)
    token = str(get_settings().internal_service_token or "").strip()
    if not token:
        return await call_next(request)
    received = str(request.headers.get("x-internal-token") or "").strip()
    if received != token:
        return JSONResponse(
            status_code=401,
            content={
                "status": "error",
                "error": {
                    "code": "internal_token_invalid",
                    "message": "Agent execution layer is restricted to trusted gateway.",
                    "retryable": False,
                },
            },
        )
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    cleanup_idle_account_runtime()
    return {"status": "ok"}


@app.on_event("startup")
def on_startup() -> None:
    start_daily_scheduler()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_daily_scheduler()
    _PLAYWRIGHT_EXECUTOR.shutdown(wait=False, cancel_futures=True)


@app.post("/run-task/{task_id}")
def run_task(task_id: str):
    client = get_supabase()
    initial_state = {"task_id": task_id}
    try:
        # Legacy compatibility endpoint: delegate to the unified pipeline runner.
        result = run_pipeline_task_service(client, task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "status": "ok",
        "task_id": task_id,
        "result": result,
        "compat_mode": "run-task->run-pipeline",
        "initial_state": initial_state,
    }


@app.post("/run-pipeline/{pipeline_task_id}")
def run_pipeline_task(pipeline_task_id: str):
    client = get_supabase()
    try:
        result = run_pipeline_task_service(client, pipeline_task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "pipeline_task_id": pipeline_task_id, "result": result}


@app.post("/reflect/domain/{domain_slug}")
def run_reflection(domain_slug: str):
    client = get_supabase()
    result = reflect_and_upgrade(client, domain_slug)
    return result


@app.post("/daily-run/domain/{domain_slug}")
def run_daily_domain_ops(
    domain_slug: str,
    triggered_by: str = "api:manual",
    run_key: str = "",
    preferred_account_id: str = "",
):
    client = get_supabase()
    try:
        # CLI delegates back through the API, which calls this agent to draft.
        dispatch = run_daily_ops_with_retry if daily_ops_uses_cli(client, domain_slug, preferred_account_id.strip()) else (
            lambda *args, **kwargs: _run_playwright_serial(run_daily_ops_with_retry, *args, **kwargs)
        )
        result = dispatch(
            client,
            domain_slug=domain_slug,
            run_key=run_key.strip() or None,
            triggered_by=triggered_by,
            flow="full",
            preferred_account_id=preferred_account_id.strip(),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/daily-run/viewpoint/domain/{domain_slug}")
def run_daily_domain_viewpoint_ops(
    domain_slug: str,
    triggered_by: str = "api:manual",
    run_key: str = "",
    preferred_account_id: str = "",
):
    client = get_supabase()
    try:
        dispatch = run_daily_ops_with_retry if daily_ops_uses_cli(client, domain_slug, preferred_account_id.strip()) else (
            lambda *args, **kwargs: _run_playwright_serial(run_daily_ops_with_retry, *args, **kwargs)
        )
        result = dispatch(
            client,
            domain_slug=domain_slug,
            run_key=run_key.strip() or None,
            triggered_by=triggered_by,
            flow="viewpoint",
            preferred_account_id=preferred_account_id.strip(),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.get("/daily-run/status")
def get_daily_domain_ops_status(domain_slug: str = "japan_immigration", flow: str = "full"):
    client = get_supabase()
    try:
        status = get_daily_ops_status(client, domain_slug=domain_slug, flow=(flow or "full"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    status["scheduler"] = get_scheduler_runtime_status()
    return status


@app.get("/daily-run/reports")
def list_daily_domain_ops_reports(domain_slug: str = "japan_immigration", limit: int = 20, flow: str = "full"):
    client = get_supabase()
    try:
        items = get_daily_ops_reports(client, domain_slug=domain_slug, limit=limit, flow=(flow or "full"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"items": items}


@app.post("/assistant/chat")
def assistant_chat(request: AssistantChatRequest):
    client = get_supabase()
    try:
        result = run_assistant_chat(
            client,
            message=request.message,
            domain_slug=request.domain_slug,
            account_id=request.account_id,
            triggered_by=request.triggered_by,
            auto_execute=request.auto_execute,
            conversation_history=request.conversation_history,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/coach/chat")
def coach_chat(request: CoachChatRequest):
    client = get_supabase()
    try:
        result = run_coach_chat(
            client,
            message=request.message,
            domain_slug=request.domain_slug,
            account_id=request.account_id,
            conversation_history=request.conversation_history,
            triggered_by=request.triggered_by,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.get("/coach/brief")
def coach_brief(domain_slug: str = "japan_immigration", account_id: str = ""):
    client = get_supabase()
    try:
        result = run_coach_brief(client, domain_slug=domain_slug, account_id=account_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/coach/confirm-action")
def coach_confirm_action(request: CoachConfirmActionRequest):
    client = get_supabase()
    try:
        result = run_coach_confirm_action(
            client,
            action_id=request.action_id,
            confirm=request.confirm,
            domain_slug=request.domain_slug,
            account_id=request.account_id,
            force_reexecute=request.force_reexecute,
            triggered_by=request.triggered_by,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/task-plan/confirm")
def task_plan_confirm(request: TaskPlanConfirmRequest):
    client = get_supabase()
    try:
        result = run_task_plan_confirm(
            client,
            task_plan=request.task_plan,
            confirm_text=request.confirm_text,
            second_confirm_text=request.second_confirm_text,
            actor=request.actor,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/task-plan/run")
def task_plan_run(request: TaskPlanRunRequest):
    client = get_supabase()
    try:
        result = run_task_plan(
            client,
            task_plan=request.task_plan,
            domain_slug=request.domain_slug,
            account_id=request.account_id,
            triggered_by=request.triggered_by,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.get("/accounts/{account_id}/runtime")
def account_runtime(account_id: str):
    cleanup_idle_account_runtime()
    return snapshot_account_runtime(account_id)


@app.post("/accounts/{account_id}/collect")
def account_collect(account_id: str, request: AccountCollectionRequest):
    client = get_supabase()
    try:
        return _run_playwright_serial(
            run_account_collection,
            client,
            domain_slug=request.domain_slug,
            account_id=account_id,
            source_kind=request.source_kind,
            triggered_by=request.triggered_by,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/playwright-tools/run")
def playwright_tool_run(request: PlaywrightToolRunRequest):
    client = get_supabase()
    try:
        return _run_playwright_serial(
            run_playwright_tool,
            client,
            account_id=request.account_id,
            tool_name=request.tool_name,
            params=request.params,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/publish-feedback/reconcile")
def publish_feedback_reconcile(request: PublishFeedbackReconcileRequest):
    client = get_supabase()
    try:
        return _run_playwright_serial(
            reconcile_publish_feedback,
            client,
            domain_slug=request.domain_slug,
            account_id=request.account_id,
            pipeline_task_id=request.pipeline_task_id,
            limit=request.limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/mcp/readonly/status")
def mcp_readonly_status():
    return get_mcp_readonly_status()


@app.post("/mcp/readonly/search")
def mcp_readonly_search(request: McpReadonlySearchRequest):
    client = get_supabase()
    try:
        return search_readonly_via_mcp(
            query=request.query,
            limit=request.limit,
            source_kind=request.source_kind,
            client=client,
            account_id=request.account_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/mcp/readonly/sync-metrics")
def mcp_readonly_sync_metrics(request: McpReadonlySyncMetricsRequest):
    client = get_supabase()
    try:
        return sync_recent_published_metrics_via_mcp(client, domain_slug=request.domain_slug, limit=request.limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/mcp/readonly/collect-intel")
def mcp_readonly_collect_intel(request: McpReadonlyCollectIntelRequest):
    client = get_supabase()
    try:
        return collect_intel_bundle_via_mcp(
            client,
            domain_slug=request.domain_slug,
            query=request.query,
            account_id=request.account_id,
            limit=request.limit,
            include_home=request.include_home,
            include_search=request.include_search,
            include_detail_metrics=request.include_detail_metrics,
            persist=request.persist,
            tool_profile=request.tool_profile,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "domain_slug": request.domain_slug,
            "query": request.query,
            "requested_limit": request.limit,
            "collected": 0,
            "inserted": 0,
            "include_home": request.include_home,
            "include_search": request.include_search,
            "include_detail_metrics": request.include_detail_metrics,
            "reason": "collect_intel_exception",
            "error": str(exc),
            "diagnostics": [{"step": "collect_intel_bundle_via_mcp", "error": str(exc)}],
            "items": [],
        }


@app.post("/mcp/readonly/custom-call")
def mcp_readonly_custom_call(request: McpReadonlyCustomCallRequest):
    try:
        return custom_call_readonly_via_mcp(
            tool_name=request.tool_name,
            arguments=request.arguments,
            source_kind=request.source_kind,
            limit=request.limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/mcp/write/status")
def mcp_write_status():
    return get_mcp_write_status()


@app.post("/mcp/write/custom-call")
def mcp_write_custom_call(request: McpWriteCustomCallRequest):
    try:
        return custom_call_write_via_mcp(
            tool_name=request.tool_name,
            arguments=request.arguments,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/mcp/write/publish")
def mcp_write_publish(request: McpWritePublishRequest):
    try:
        return publish_content_via_mcp(
            title=request.title,
            content=request.content,
            images=request.images,
            tags=request.tags,
            schedule_at=request.schedule_at,
            video=request.video,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/analysis/hot-posts")
def analysis_hot_posts(request: HotPostAnalyzeRequest):
    router = get_router()
    client = get_supabase()
    try:
        resolved_prompt = request.analysis_prompt
        if not resolved_prompt.strip() and request.account_id.strip():
            resolved_prompt = get_active_prompt_text(
                client,
                domain_slug=request.domain_slug,
                account_id=request.account_id,
                agent_name="hot_post_analysis",
            )
        return router.analyze_hot_posts(
            items=[item.model_dump() for item in request.items],
            analysis_prompt=resolved_prompt,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/content/rebuild-xhs")
def rebuild_xhs_content(request: RebuildXhsContentRequest):
    router = get_router()
    client = get_supabase()
    try:
        strategy_prompt = request.strategy_prompt
        copy_prompt = request.copy_prompt
        if request.account_id.strip():
            if not strategy_prompt.strip():
                strategy_prompt = get_active_prompt_text(
                    client,
                    domain_slug=request.domain_slug,
                    account_id=request.account_id,
                    agent_name="rebuild_strategy",
                )
            if not copy_prompt.strip():
                copy_prompt = get_active_prompt_text(
                    client,
                    domain_slug=request.domain_slug,
                    account_id=request.account_id,
                    agent_name="rebuild_copy",
                )
        return router.rebuild_xhs_content(
            competitor_analysis=request.competitor_analysis,
            ip_positioning=request.ip_positioning,
            weekly_keywords=request.weekly_keywords,
            tone_style=request.tone_style,
            strategy_prompt=strategy_prompt,
            copy_prompt=copy_prompt,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/analysis/chief-evolution")
def analysis_chief_evolution(request: ChiefEvolutionRequest):
    router = get_router()
    try:
        return router.chief_evolution_analysis(
            competitor_dataset=request.competitor_dataset,
            our_history=request.our_history,
            current_system_version=request.current_system_version,
            milestone_goal=request.milestone_goal,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


install_error_handlers(app)
