"""Scoped human-publication and local content workflow controls."""
import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from supabase import Client

from app.config import get_settings, Settings
from app.db import get_supabase
from app.models import Actor, PipelineTaskRecord
from app.security import require_roles
from app.services.browser_media import MEDIA_ROOT

router = APIRouter(prefix='/api', tags=['content-workflow'])


class TaskScope(BaseModel):
    account_id: UUID
    domain_slug: str = Field(min_length=2, max_length=120)


class ManualPublicationRequest(TaskScope):
    published_url: str = Field(min_length=20, max_length=2000)
    published_at: datetime
    confirmed: bool = False
    correction_reason: str = Field(default='', max_length=1000)


class ObservationRequest(TaskScope):
    values: dict[str, Any]
    observed_at: datetime
    provenance: str = Field(min_length=2, max_length=200)


class KnowledgeRequest(BaseModel):
    domain_slug: str = Field(min_length=2, max_length=120)
    item_ids: list[UUID] = Field(default_factory=list, max_length=200)


async def _reconcile(client, settings, task_id, account_id, domain_slug):
    warning = ''
    try:
        async with httpx.AsyncClient(timeout=90) as remote:
            response = await remote.post(settings.agent_service_url+'/publish-feedback/reconcile',
                json={'domain_slug': domain_slug, 'account_id': account_id, 'pipeline_task_id': task_id, 'limit': 20})
            response.raise_for_status()
    except httpx.HTTPError:
        warning = '观测已保存，但复盘服务本轮未完成，请稍后刷新反馈'
    rows = client.table('pipeline_tasks').select('*').eq('id', task_id).eq('account_id', account_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(503, '观测操作已提交，但任务读取未确认，请刷新核对')
    return {**rows[0], 'operation_warning': warning}


@router.post('/pipeline/tasks/{task_id}/manual-publication', response_model=PipelineTaskRecord)
def manual_publication(task_id: UUID, request: ManualPublicationRequest,
    client: Client = Depends(get_supabase), actor: Actor = Depends(require_roles('admin', 'operator'))):
    from app.services.manual_publication import register_manual_publication
    return register_manual_publication(client, pipeline_task_id=str(task_id), account_id=str(request.account_id),
        domain_slug=request.domain_slug, published_url=request.published_url, published_at=request.published_at,
        confirmed=request.confirmed, actor=actor)


@router.patch('/pipeline/tasks/{task_id}/manual-publication', response_model=PipelineTaskRecord)
def correct_manual_publication(task_id: UUID, request: ManualPublicationRequest,
    client: Client = Depends(get_supabase), actor: Actor = Depends(require_roles('admin', 'operator'))):
    if len(request.correction_reason.strip()) < 3:
        raise HTTPException(422, '请填写至少三个字的纠错原因')
    from app.services.manual_publication import register_manual_publication
    return register_manual_publication(client, pipeline_task_id=str(task_id), account_id=str(request.account_id),
        domain_slug=request.domain_slug, published_url=request.published_url, published_at=request.published_at,
        confirmed=request.confirmed, actor=actor, correction_reason=request.correction_reason)


@router.post('/pipeline/tasks/{task_id}/feedback/observations')
async def manual_observation(task_id: UUID, request: ObservationRequest,
    client: Client = Depends(get_supabase), settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles('admin', 'operator'))):
    from app.services.task_observations import save_task_observation
    rows = client.table('pipeline_tasks').select('publish_jsonb').eq('id', str(task_id)).eq('account_id', str(request.account_id)).limit(1).execute().data or []
    identity = ((rows[0].get('publish_jsonb') or {}).get('identity') or {}) if rows else {}
    save_task_observation(client, pipeline_task_id=str(task_id), account_id=str(request.account_id), domain_slug=request.domain_slug,
        observation={'values': request.values, 'observed_at': request.observed_at, 'provider': 'manual',
            'source_ref': identity.get('published_url'), 'provenance': request.provenance}, actor=actor)
    return await _reconcile(client, settings, str(task_id), str(request.account_id), request.domain_slug)


@router.post('/pipeline/tasks/{task_id}/feedback/sync')
async def sync_observation(task_id: UUID, request: TaskScope,
    client: Client = Depends(get_supabase), settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_roles('admin', 'operator'))):
    from app.services.task_observations import sync_task_observation
    await asyncio.to_thread(sync_task_observation, client, pipeline_task_id=str(task_id), account_id=str(request.account_id),
        domain_slug=request.domain_slug, actor=actor)
    return await _reconcile(client, settings, str(task_id), str(request.account_id), request.domain_slug)


@router.post('/ops/accounts/{account_id}/collection/reconcile-knowledge')
def reconcile_knowledge(account_id: UUID, request: KnowledgeRequest,
    client: Client = Depends(get_supabase), actor: Actor = Depends(require_roles('admin', 'operator'))):
    from app.services.collection_knowledge import link_capture_to_knowledge
    from app.services.kb_service import resolve_domain
    domain = resolve_domain(client, request.domain_slug)
    query = client.table('intelligence_items').select('id,meta_jsonb').eq('domain_id', domain['id']).eq('account_id', str(account_id)).order('captured_at', desc=True).limit(200)
    if request.item_ids:
        query = query.in_('id', [str(value) for value in request.item_ids])
    rows = query.execute().data or []
    results, failures = [], []
    for row in rows:
        if not (row.get('meta_jsonb') or {}).get('browser_capture'):
            continue
        try:
            results.append(link_capture_to_knowledge(client, account_id=str(account_id), domain_id=domain['id'], item_id=row['id'], actor=actor))
        except Exception:
            failures.append({'item_id': row['id'], 'message': '关联未完成，请核对记录归属或数据库状态'})
    return {'status': 'partial' if failures else 'ok', 'linked': len(results), 'items': results, 'failures': failures}


@router.post('/ops/accounts/{account_id}/browser/captures/{item_id}/retry-comments')
def retry_comments(account_id: UUID, item_id: UUID, client: Client = Depends(get_supabase),
    actor: Actor = Depends(require_roles('admin', 'operator'))):
    from app.services.xhs_cli_collection import retry_capture_comments
    return retry_capture_comments(client, account_id=str(account_id), item_id=str(item_id), actor=actor)


@router.post('/pipeline/tasks/{task_id}/images', response_model=PipelineTaskRecord)
async def upload_draft_image(task_id: UUID, request: Request, account_id: UUID = Query(),
    client: Client = Depends(get_supabase), actor: Actor = Depends(require_roles('admin', 'operator'))):
    from app.services.draft_media import save_draft_image
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 10*1024*1024:
            raise HTTPException(413, '每张图片不能超过10MiB')
    return save_draft_image(client, task_id=str(task_id), account_id=str(account_id), content=bytes(data), actor=actor, media_root=MEDIA_ROOT)


@router.get('/pipeline/tasks/{task_id}/images/{image_id}')
def read_draft_image(task_id: UUID, image_id: UUID, account_id: UUID = Query(),
    client: Client = Depends(get_supabase), _: Actor = Depends(require_roles('admin', 'operator', 'reviewer'))):
    from app.services.draft_media import read_draft_image as read
    path, mime = read(client, task_id=str(task_id), account_id=str(account_id), image_id=str(image_id), media_root=MEDIA_ROOT)
    return FileResponse(path, media_type=mime, headers={'Cache-Control': 'private, no-store'})


@router.delete('/pipeline/tasks/{task_id}/images/{image_id}', response_model=PipelineTaskRecord)
def detach_draft_image(task_id: UUID, image_id: UUID, account_id: UUID = Query(),
    client: Client = Depends(get_supabase), actor: Actor = Depends(require_roles('admin', 'operator'))):
    from app.services.draft_media import detach_draft_image as detach
    return detach(client, task_id=str(task_id), account_id=str(account_id), image_id=str(image_id), actor=actor, media_root=MEDIA_ROOT)
