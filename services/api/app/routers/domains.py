from fastapi import APIRouter, Depends
from supabase import Client

from app.db import get_supabase
from app.models import Actor, DomainListResponse, DomainRecord, StrategyVersionCreateRequest
from app.security import require_roles
from app.services.strategy_service import (
    create_strategy_version,
    get_domain,
    list_domains,
    list_strategy_versions,
)

router = APIRouter(prefix="/api/domains", tags=["domains"])


@router.get("", response_model=DomainListResponse)
def get_domains(
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    rows = list_domains(client)
    return DomainListResponse(items=[DomainRecord(**item) for item in rows])


@router.get("/{slug}", response_model=DomainRecord)
def get_domain_detail(
    slug: str,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    return DomainRecord(**get_domain(client, slug))


@router.get("/{slug}/strategy/versions")
def get_strategy_versions(
    slug: str,
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
):
    return {"items": list_strategy_versions(client, slug)}


@router.post("/{slug}/strategy/versions")
def post_strategy_version(
    slug: str,
    request: StrategyVersionCreateRequest,
    client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles("admin", "operator")),
):
    return create_strategy_version(
        client=client,
        slug=slug,
        prompt_jsonb=request.prompt_jsonb,
        reason=request.reason,
        actor=actor,
    )
