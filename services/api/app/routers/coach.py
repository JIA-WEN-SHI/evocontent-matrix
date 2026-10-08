import asyncio

import httpx
from fastapi import APIRouter, Depends, Query

from app.config import Settings, get_settings
from app.models import Actor, CoachChatRequest, CoachConfirmActionRequest
from app.security import require_roles

router = APIRouter(prefix="/api/coach", tags=["coach"])

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable_http_error(exc: httpx.HTTPError) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = int(exc.response.status_code)
        return code in _RETRYABLE_STATUS
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "server disconnected",
            "remoteprotocolerror",
            "connection reset",
            "timed out",
            "timeout",
            "temporarily unavailable",
        )
    )


async def _request_with_retry(
    *,
    method: str,
    url: str,
    params: dict | None = None,
    json_payload: dict | None = None,
    timeout: float,
    attempts: int = 3,
) -> httpx.Response:
    # A timed-out mutation may already have taken effect on the agent.
    attempts = max(1, attempts) if method.upper() in {"GET", "HEAD", "OPTIONS"} else 1
    last_exc: httpx.HTTPError | None = None
    for idx in range(max(1, attempts)):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(method, url, params=params, json=json_payload)
                response.raise_for_status()
                return response
        except httpx.HTTPError as exc:
            last_exc = exc
            if idx < attempts - 1 and _is_retryable_http_error(exc):
                await asyncio.sleep(0.2 * (idx + 1))
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("request retry exhausted")


@router.get("/brief")
async def coach_brief(
    domain_slug: str = Query(default="japan_immigration"),
    account_id: str = Query(default=""),
    settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    url = f"{settings.agent_service_url}/coach/brief"
    params = {
        "domain_slug": domain_slug,
        "account_id": account_id,
    }
    try:
        response = await _request_with_retry(method="GET", url=url, params=params, timeout=120.0, attempts=3)
        result = response.json()
    except httpx.HTTPError as exc:
        result = {
            "status": "degraded",
            "error": {
                "code": "coach_brief_unavailable",
                "message": str(exc),
                "retryable": True,
            },
            "domain_slug": domain_slug,
            "account_id": account_id or "",
            "account_name": "",
            "window_days": 14,
            "yesterday_summary": {},
            "today_actions": [],
            "decision_gates": [],
            "prompt_upgrade_suggestions": [],
            "context_snapshot": {"counts": {}, "intel_count": 0, "pipeline_count": 0, "kb_count": 0},
            "message": f"coach brief unavailable: {exc}",
        }
    if isinstance(result, dict):
        result.setdefault("triggered_by", f"api:{actor.user_id}")
    return result


@router.post("/chat")
async def coach_chat(
    request: CoachChatRequest,
    settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    url = f"{settings.agent_service_url}/coach/chat"
    payload = {
        "message": request.message,
        "domain_slug": request.domain_slug,
        "account_id": request.account_id,
        "conversation_history": request.conversation_history,
        "triggered_by": f"api:{actor.user_id}",
    }
    try:
        response = await _request_with_retry(
            method="POST",
            url=url,
            json_payload=payload,
            timeout=240.0,
            attempts=3,
        )
        return response.json()
    except httpx.HTTPError as exc:
        return {
            "status": "degraded",
            "error": {
                "code": "coach_chat_unavailable",
                "message": str(exc),
                "retryable": True,
            },
            "domain_slug": request.domain_slug,
            "account_id": request.account_id,
            "coach_reply": "教练服务短暂波动，我已保留你的问题。请 20 秒后再试一次。",
            "requires_confirmation": False,
            "brief": {
                "status": "degraded",
                "domain_slug": request.domain_slug,
                "account_id": request.account_id,
                "today_actions": [],
                "decision_gates": [],
                "prompt_upgrade_suggestions": [],
            },
            "pending_actions": [],
            "decision_gates": [],
            "prompt_upgrade_suggestions": [],
            "next_questions": [],
            "context_info": {"history_used_turns": len(request.conversation_history or []), "error": str(exc)},
        }


@router.post("/confirm-action")
async def coach_confirm_action(
    request: CoachConfirmActionRequest,
    settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    url = f"{settings.agent_service_url}/coach/confirm-action"
    payload = {
        "action_id": request.action_id,
        "domain_slug": request.domain_slug,
        "account_id": request.account_id,
        "confirm": request.confirm,
        "force_reexecute": bool(request.force_reexecute),
        "triggered_by": f"api:{actor.user_id}",
    }
    try:
        response = await _request_with_retry(
            method="POST",
            url=url,
            json_payload=payload,
            timeout=240.0,
            attempts=3,
        )
        return response.json()
    except httpx.HTTPError as exc:
        return {
            "status": "degraded",
            "error": {
                "code": "coach_confirm_unavailable",
                "message": str(exc),
                "retryable": True,
            },
            "action_id": request.action_id,
            "state": "retry_later",
            "message": "执行通道短暂波动，当前动作未确认落地，请稍后重试。",
            "result": {"status": "retry_later", "error": str(exc)},
        }
