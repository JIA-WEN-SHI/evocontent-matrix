import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.config import Settings, get_settings
from app.models import Actor
from app.security import require_roles

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.post("/reflect")
async def trigger_reflection(
    domain_slug: str = "japan_immigration",
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    url = f"{settings.agent_service_url}/reflect/domain/{domain_slug}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/daily-run")
async def trigger_daily_run(
    domain_slug: str = "japan_immigration",
    settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    url = f"{settings.agent_service_url}/daily-run/domain/{domain_slug}"
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            response = await client.post(url, params={"triggered_by": f"api:{actor.user_id}"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.post("/daily-viewpoint-run")
async def trigger_daily_viewpoint_run(
    domain_slug: str = "japan_immigration",
    settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    url = f"{settings.agent_service_url}/daily-run/viewpoint/domain/{domain_slug}"
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            response = await client.post(url, params={"triggered_by": f"api:{actor.user_id}"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.get("/daily-status")
async def get_daily_status(
    domain_slug: str = "japan_immigration",
    flow: str = "full",
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    url = f"{settings.agent_service_url}/daily-run/status"
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.get(url, params={"domain_slug": domain_slug, "flow": flow})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()


@router.get("/daily-reports")
async def get_daily_reports(
    domain_slug: str = "japan_immigration",
    limit: int = 20,
    flow: str = "full",
    settings: Settings = Depends(get_settings),
    _: Actor = Depends(require_roles("admin", "operator")),
):
    url = f"{settings.agent_service_url}/daily-run/reports"
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.get(
                url,
                params={"domain_slug": domain_slug, "limit": limit, "flow": flow},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return response.json()
