import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.models import Actor
from app.security import require_roles

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.post("/run-task/{task_id}")
async def run_task(
    task_id: str,
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    # Legacy API surface kept for compatibility; backend executes unified pipeline run.
    url = f"{settings.agent_service_url}/run-pipeline/{task_id}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return response.json()
