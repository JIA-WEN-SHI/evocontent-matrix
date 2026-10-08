import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.models import Actor, AssistantChatRequest
from app.security import require_roles

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


@router.post("/chat")
async def assistant_chat(
    request: AssistantChatRequest,
    settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    url = f"{settings.agent_service_url}/assistant/chat"
    payload = {
        "message": request.message,
        "domain_slug": request.domain_slug,
        "account_id": request.account_id,
        "auto_execute": request.auto_execute,
        "conversation_history": request.conversation_history,
        "triggered_by": f"api:{actor.user_id}",
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return response.json()
