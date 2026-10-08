import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
import asyncio
from supabase import Client

from app.config import Settings, get_settings
from app.db import get_supabase, user_facing_data_message
from app.models import (
    AgentConfigView,
    Actor,
    AccountAuthResetResponse,
    ContentMethodView,
    AccountLoopRunDailyRequest,
    AccountLoopRunDailyResponse,
    ExecutionRouteStatusView,
    AccountLoopStatusResponse,
    AccountLoginBootstrapRequest,
    AccountLoginBootstrapResponse,
    AccountOnboardingCommitRequest,
    AccountOnboardingCommitResponse,
    AccountRuntimeContextResponse,
    ChannelAccountCreateRequest,
    ChannelAccountCollectionPlanUpsertRequest,
    ChannelAccountListResponse,
    ChannelAccountRecord,
    LoopExecutionView,
    PromptVersionActivateRequest,
    PromptVersionCreateRequest,
    PromptVersionListResponse,
    PromptVersionRecord,
    PromptVersionRollbackRequest,
    SopCurrentView,
    SopSnapshotListResponse,
    ChannelAccountStrategyUpsertRequest,
    ChannelAccountUpdateRequest,
    ReviewActionRequest,
    ReviewActionResponse,
    SopRollbackRequest,
    SopRollbackResponse,
)
from app.security import require_roles
from app.services.account_service import (
    activate_prompt_version,
    append_sop_checkpoint,
    commit_account_onboarding,
    create_channel_account,
    create_prompt_version,
    delete_channel_account,
    get_account_agent_config_view,
    get_account_content_method_view,
    get_account_loop_execution_view,
    get_account_loop_status,
    get_execution_route_status_view,
    get_account_sop_current,
    get_account_sop_snapshots,
    get_channel_account_strategy,
    get_channel_account,
    list_channel_accounts,
    list_prompt_versions,
    launch_channel_account_login_bootstrap,
    reset_channel_account_auth,
    rollback_prompt_version,
    rollback_account_sop_snapshot,
    run_review_action,
    update_channel_account,
    update_channel_account_collection_plan,
    update_channel_account_strategy,
    verify_channel_account_login_state,
)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _fallback_account_strategy(account_id: str) -> dict:
    return {
        "status": "degraded",
        "account_id": account_id,
        "account_name": "",
        "strategy_profile": {},
        "collection_plan": {},
        "feedback_plan": {},
    }


def _fallback_loop_status(account_id: str, domain_slug: str) -> AccountLoopStatusResponse:
    return AccountLoopStatusResponse(
        status="degraded",
        account_id=account_id,
        domain_slug=domain_slug,
        loop={
            "stage": "degraded",
            "onboarding_ready": False,
            "blockers": ["当前无法读取账号闭环状态，请稍后刷新。"],
            "progress": {},
            "collection_health": {},
            "kpi_gate": {
                "status": "degraded",
                "passed": False,
                "reason": "当前无法读取反馈指标。",
                "metric_source": "",
                "impressions": 0,
                "interactions": 0,
                "engagement_rate": 0,
                "thresholds": {},
            },
            "latest": {},
            "completion_score": 0,
        },
        sop_latest=None,
    )


def _fallback_sop_current(account_id: str, domain_slug: str) -> SopCurrentView:
    return SopCurrentView(
        status="degraded",
        account_id=account_id,
        domain_slug=domain_slug,
        sop_latest=None,
        prompt_versions=[],
    )


def _fallback_sop_snapshots(account_id: str, domain_slug: str) -> SopSnapshotListResponse:
    return SopSnapshotListResponse(
        status="degraded",
        account_id=account_id,
        domain_slug=domain_slug,
        current_version=0,
        items=[],
    )


def _fallback_agent_config(account_id: str, domain_slug: str) -> AgentConfigView:
    return AgentConfigView(
        status="degraded",
        account_id=account_id,
        domain_slug=domain_slug,
        primary_entry="总教练入口",
        coordinator="总教练",
        nodes=[],
    )


def _fallback_loop_execution_view(account_id: str, domain_slug: str) -> LoopExecutionView:
    return LoopExecutionView(
        status="degraded",
        account_id=account_id,
        domain_slug=domain_slug,
        current_stage="degraded",
        completion_score=0,
        steps=[],
        latest_summary={"message": "当前无法读取流程执行记录，请稍后刷新。"},
    )


def _fallback_content_method_view(account_id: str, domain_slug: str) -> ContentMethodView:
    return ContentMethodView(
        status="degraded",
        account_id=account_id,
        domain_slug=domain_slug,
        sections=[],
    )


def _fallback_execution_route(account_id: str, domain_slug: str) -> ExecutionRouteStatusView:
    return ExecutionRouteStatusView(
        status="degraded",
        account_id=account_id,
        domain_slug=domain_slug,
        primary_route="标准自动执行",
        backup_route="备用自动执行",
        readonly_bridge="只读补数",
        switch_rules=["当前无法读取执行策略，先按默认主方案理解。"],
        user_facing_status="当前无法读取执行策略状态。",
    )


@router.get("", response_model=ChannelAccountListResponse)
def get_accounts(
    channel: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> ChannelAccountListResponse:
    try:
        rows = list_channel_accounts(client, channel=channel, limit=limit)
    except Exception:  # noqa: BLE001
        rows = []
    return ChannelAccountListResponse(items=[ChannelAccountRecord(**row) for row in rows])


@router.get("/{account_id}", response_model=ChannelAccountRecord)
def get_account_by_id(
    account_id: str,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> ChannelAccountRecord:
    row = get_channel_account(client, account_id)
    return ChannelAccountRecord(**row)


@router.get("/{account_id}/runtime", response_model=AccountRuntimeContextResponse)
async def get_account_runtime(
    account_id: str,
    settings: Settings = Depends(get_settings),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> AccountRuntimeContextResponse:
    try:
        account = get_channel_account(client, account_id)
        strategy_payload = get_channel_account_strategy(client, account_id)
        strategy_profile = strategy_payload.get("strategy_profile") if isinstance(strategy_payload, dict) else {}
        collection_plan = strategy_payload.get("collection_plan") if isinstance(strategy_payload, dict) else {}

        execution: dict = {
            "status": "idle",
            "busy": False,
            "current_action": "",
            "current_task_id": "",
            "last_error": "",
            "last_used_at": None,
            "browser_context_key": f"browser:{account_id}",
            "lock_key": f"account:{account_id}",
        }
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            try:
                response = await http_client.get(f"{settings.agent_service_url}/accounts/{account_id}/runtime")
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict):
                    execution.update(payload)
            except Exception:  # noqa: BLE001
                execution["status"] = "unreachable"

        latest_task_rows = (
            client.table("pipeline_tasks")
            .select("id,status,updated_at,published_at")
            .eq("account_id", account_id)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        latest_run_rows = (
            client.table("runs_daily")
            .select("id,run_key,status,updated_at,finished_at")
            .eq("account_id", account_id)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        ).data or []

        return AccountRuntimeContextResponse(
            status="ok",
            account_id=account_id,
            account_name=str(account.get("account_name") or ""),
            channel=str(account.get("channel") or ""),
            execution=execution,
            collection_plan=collection_plan if isinstance(collection_plan, dict) else {},
            latest={
                "task": latest_task_rows[0] if latest_task_rows else None,
                "run": latest_run_rows[0] if latest_run_rows else None,
                "strategy_profile": strategy_profile,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return AccountRuntimeContextResponse(
            status="degraded",
            account_id=account_id,
            account_name="",
            channel="",
            execution={
                "status": "unreachable",
                "busy": False,
                "current_action": "",
                "current_task_id": "",
                "last_error": "account_runtime_degraded",
                "last_used_at": None,
                "browser_context_key": f"browser:{account_id}",
                "lock_key": f"account:{account_id}",
            },
            collection_plan={},
            latest={
                "task": None,
                "run": None,
                "strategy_profile": {
                    "degraded_notice": user_facing_data_message(exc),
                },
            },
        )


@router.post("", response_model=ChannelAccountRecord)
def post_account(
    request: ChannelAccountCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> ChannelAccountRecord:
    row = create_channel_account(client, request, actor)
    return ChannelAccountRecord(**row)


@router.patch("/{account_id}", response_model=ChannelAccountRecord)
def patch_account(
    account_id: str,
    request: ChannelAccountUpdateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> ChannelAccountRecord:
    row = update_channel_account(client, account_id, request, actor)
    return ChannelAccountRecord(**row)


@router.delete("/{account_id}")
def delete_account(
    account_id: str,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    return delete_channel_account(client, account_id, actor)


@router.post("/{account_id}/verify-login")
def verify_account_login(
    account_id: str,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    return verify_channel_account_login_state(client, account_id, actor)


@router.get("/{account_id}/strategy")
def get_account_strategy(
    account_id: str,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    try:
        return get_channel_account_strategy(client, account_id)
    except Exception:  # noqa: BLE001
        return _fallback_account_strategy(account_id)


@router.put("/{account_id}/strategy")
def put_account_strategy(
    account_id: str,
    request: ChannelAccountStrategyUpsertRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    return update_channel_account_strategy(client, account_id, request, actor)


@router.put("/{account_id}/collection-plan")
def put_account_collection_plan(
    account_id: str,
    request: ChannelAccountCollectionPlanUpsertRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    return update_channel_account_collection_plan(client, account_id, request, actor)


@router.post("/{account_id}/onboarding/commit", response_model=AccountOnboardingCommitResponse)
def post_account_onboarding_commit(
    account_id: str,
    request: AccountOnboardingCommitRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> AccountOnboardingCommitResponse:
    payload = commit_account_onboarding(client, account_id, request, actor)
    return AccountOnboardingCommitResponse(**payload)


@router.get("/{account_id}/loop/status", response_model=AccountLoopStatusResponse)
def get_account_loop_status_view(
    account_id: str,
    domain_slug: str = Query(default="japan_immigration"),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> AccountLoopStatusResponse:
    try:
        payload = get_account_loop_status(client, account_id=account_id, domain_slug=domain_slug)
        return AccountLoopStatusResponse(**payload)
    except Exception:  # noqa: BLE001
        return _fallback_loop_status(account_id, domain_slug)


@router.get("/{account_id}/sop/current", response_model=SopCurrentView)
def get_account_sop_current_view(
    account_id: str,
    domain_slug: str = Query(default="japan_immigration"),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> SopCurrentView:
    try:
        payload = get_account_sop_current(client, account_id=account_id, domain_slug=domain_slug)
        return SopCurrentView(**payload)
    except Exception:  # noqa: BLE001
        return _fallback_sop_current(account_id, domain_slug)


@router.get("/{account_id}/sop/snapshots", response_model=SopSnapshotListResponse)
def get_account_sop_snapshots_view(
    account_id: str,
    domain_slug: str = Query(default="japan_immigration"),
    limit: int = Query(default=10, ge=1, le=50),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> SopSnapshotListResponse:
    try:
        payload = get_account_sop_snapshots(client, account_id=account_id, domain_slug=domain_slug, limit=limit)
        return SopSnapshotListResponse(**payload)
    except Exception:  # noqa: BLE001
        return _fallback_sop_snapshots(account_id, domain_slug)


@router.get("/{account_id}/agent-config", response_model=AgentConfigView)
def get_account_agent_config(
    account_id: str,
    domain_slug: str = Query(default="japan_immigration"),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> AgentConfigView:
    try:
        payload = get_account_agent_config_view(client, account_id=account_id, domain_slug=domain_slug)
        return AgentConfigView(**payload)
    except Exception:  # noqa: BLE001
        return _fallback_agent_config(account_id, domain_slug)


@router.get("/{account_id}/loop-execution-view", response_model=LoopExecutionView)
def get_account_loop_execution_view_route(
    account_id: str,
    domain_slug: str = Query(default="japan_immigration"),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> LoopExecutionView:
    try:
        payload = get_account_loop_execution_view(client, account_id=account_id, domain_slug=domain_slug)
        return LoopExecutionView(**payload)
    except Exception:  # noqa: BLE001
        return _fallback_loop_execution_view(account_id, domain_slug)


@router.get("/{account_id}/content-method-view", response_model=ContentMethodView)
def get_account_content_method(
    account_id: str,
    domain_slug: str = Query(default="japan_immigration"),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> ContentMethodView:
    try:
        payload = get_account_content_method_view(client, account_id=account_id, domain_slug=domain_slug)
        return ContentMethodView(**payload)
    except Exception:  # noqa: BLE001
        return _fallback_content_method_view(account_id, domain_slug)


@router.get("/{account_id}/execution-route", response_model=ExecutionRouteStatusView)
def get_account_execution_route(
    account_id: str,
    domain_slug: str = Query(default="japan_immigration"),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> ExecutionRouteStatusView:
    try:
        payload = get_execution_route_status_view(client, account_id=account_id, domain_slug=domain_slug)
        return ExecutionRouteStatusView(**payload)
    except Exception:  # noqa: BLE001
        return _fallback_execution_route(account_id, domain_slug)


@router.post("/{account_id}/sop/rollback", response_model=SopRollbackResponse)
def post_account_sop_rollback(
    account_id: str,
    request: SopRollbackRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> SopRollbackResponse:
    payload = rollback_account_sop_snapshot(
        client,
        account_id=account_id,
        domain_slug=request.domain_slug,
        version=request.version,
        actor=actor,
        reason=request.reason,
    )
    return SopRollbackResponse(**payload)


@router.post("/{account_id}/loop/run-daily", response_model=AccountLoopRunDailyResponse)
async def post_account_loop_run_daily(
    account_id: str,
    request: AccountLoopRunDailyRequest,
    settings: Settings = Depends(get_settings),
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> AccountLoopRunDailyResponse:
    from app.services.xhs_cli_collection import uses_cli
    if uses_cli(client, account_id):
        from app.services.cli_content_loop import run_cli_content_loop
        async def draft(task_id):
            async with httpx.AsyncClient(timeout=180.0) as remote:
                response = await remote.post(f'{settings.agent_service_url}/run-pipeline/{task_id}')
                response.raise_for_status()
                return response.json()
        result = await asyncio.to_thread(lambda: asyncio.run(run_cli_content_loop(client, account_id=account_id, domain_slug=request.domain_slug,
            source_kind='viewpoint' if request.flow == 'viewpoint' else 'hotspot', run_key=request.run_key,
            actor=actor, run_draft=draft)))
        return AccountLoopRunDailyResponse(status=result['status'], account_id=account_id, domain_slug=request.domain_slug,
            gate_passed=bool((result.get('collection_health') or {}).get('passed')) or result['status'] in {'pending_review', 'approved'},
            blocked_reason=str(result.get('reason') or ''), loop_report=result, result=result)
    loop_status = get_account_loop_status(client, account_id=account_id, domain_slug=request.domain_slug)
    loop = loop_status.get("loop") if isinstance(loop_status.get("loop"), dict) else {}
    blockers = loop.get("blockers") if isinstance(loop.get("blockers"), list) else []
    collection_health = loop.get("collection_health") if isinstance(loop.get("collection_health"), dict) else {}
    onboarding_ready = bool(loop.get("onboarding_ready"))
    gate_passed = onboarding_ready and bool(collection_health.get("passed"))
    blocked_reason = "; ".join([str(item or "").strip() for item in blockers if str(item or "").strip()])[:500]
    if not request.force and not gate_passed:
        return AccountLoopRunDailyResponse(
            status="blocked",
            account_id=account_id,
            domain_slug=request.domain_slug,
            gate_passed=False,
            blocked_reason=blocked_reason or "loop_gate_failed",
            loop_report={
                "stage": loop.get("stage"),
                "onboarding_ready": onboarding_ready,
                "collection_health": collection_health,
                "next_action": "complete onboarding or improve case/asset ingestion",
            },
            result={},
        )

    flow = str(request.flow or "full").strip().lower()
    if flow == "viewpoint":
        url = f"{settings.agent_service_url}/daily-run/viewpoint/domain/{request.domain_slug}"
    else:
        flow = "full"
        url = f"{settings.agent_service_url}/daily-run/domain/{request.domain_slug}"
    params = {
        "triggered_by": f"api:{actor.user_id}",
        "preferred_account_id": account_id,
    }
    if request.run_key.strip():
        params["run_key"] = request.run_key.strip()

    async with httpx.AsyncClient(timeout=300.0) as http_client:
        try:
            response = await http_client.post(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    result = response.json() if response.content else {}

    append_sop_checkpoint(
        client,
        account_id=account_id,
        reason="daily_loop_checkpoint",
        actor=actor.user_id,
        changed_fields=["loop_report"],
        context={
            "domain_slug": request.domain_slug,
            "flow": flow,
            "result_status": str((result or {}).get("status") or ""),
            "run_key": str((result or {}).get("run_key") or ""),
        },
    )

    loop_report = {
        "stage_before_run": loop.get("stage"),
        "gate_passed": gate_passed,
        "flow": flow,
        "result_status": str((result or {}).get("status") or ""),
        "run_key": str((result or {}).get("run_key") or ""),
        "run_id": str((result or {}).get("run_id") or ""),
        "pipeline_task_id": str((result or {}).get("pipeline_task_id") or ""),
    }
    return AccountLoopRunDailyResponse(
        status="ok",
        account_id=account_id,
        domain_slug=request.domain_slug,
        gate_passed=True,
        blocked_reason="",
        loop_report=loop_report,
        result=result if isinstance(result, dict) else {"raw": result},
    )


@router.post("/{account_id}/loop/review-action", response_model=ReviewActionResponse)
def post_account_loop_review_action(
    account_id: str,
    request: ReviewActionRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "reviewer")),
) -> ReviewActionResponse:
    payload = run_review_action(client, account_id=account_id, request=request, actor=actor)
    return ReviewActionResponse(**payload)


@router.post("/{account_id}/auth/reset", response_model=AccountAuthResetResponse)
def post_account_auth_reset(
    account_id: str,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> AccountAuthResetResponse:
    return reset_channel_account_auth(client, account_id, actor)


@router.post("/{account_id}/login/bootstrap", response_model=AccountLoginBootstrapResponse)
def post_account_login_bootstrap(
    account_id: str,
    request: AccountLoginBootstrapRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> AccountLoginBootstrapResponse:
    return launch_channel_account_login_bootstrap(client, account_id, request, actor)


@router.get("/{account_id}/prompt-versions", response_model=PromptVersionListResponse)
def get_account_prompt_versions(
    account_id: str,
    domain_slug: str | None = Query(default=None),
    agent_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> PromptVersionListResponse:
    try:
        rows = list_prompt_versions(client, account_id, domain_slug=domain_slug, agent_name=agent_name, limit=limit)
    except Exception:  # noqa: BLE001
        rows = []
    return PromptVersionListResponse(items=[PromptVersionRecord(**row) for row in rows])


@router.post("/{account_id}/prompt-versions", response_model=PromptVersionRecord)
def post_account_prompt_version(
    account_id: str,
    request: PromptVersionCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> PromptVersionRecord:
    row = create_prompt_version(client, account_id, request, actor)
    return PromptVersionRecord(**row)


@router.post("/{account_id}/prompt-versions/{prompt_version_id}/rollback", response_model=PromptVersionRecord)
def post_account_prompt_version_rollback(
    account_id: str,
    prompt_version_id: str,
    request: PromptVersionRollbackRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> PromptVersionRecord:
    row = rollback_prompt_version(client, account_id, prompt_version_id, request.reason, actor)
    return PromptVersionRecord(**row)


@router.post("/{account_id}/prompt-versions/{prompt_version_id}/activate", response_model=PromptVersionRecord)
def post_account_prompt_version_activate(
    account_id: str,
    prompt_version_id: str,
    request: PromptVersionActivateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
) -> PromptVersionRecord:
    row = activate_prompt_version(client, account_id, prompt_version_id, request.reason, actor)
    return PromptVersionRecord(**row)

