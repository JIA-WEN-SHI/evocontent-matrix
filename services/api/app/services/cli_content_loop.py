"""API-owned local collection followed by the existing draft-only pipeline."""
import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException

from app.models import AuditLogEntry
from app.services.audit import write_audit_log
from app.services.account_service import (
    get_account_loop_status, _compute_collection_health, _collection_day_bounds,
    _current_cli_items, _cli_evidence_refs,
)
from app.services.collection_knowledge import link_capture_to_knowledge
from app.services.xhs_cli_collection import collect_account
from app.services.content_branches import branch_selection, project_reference_ready


async def run_cli_content_loop(client, *, account_id, domain_slug, source_kind, run_key, actor, run_draft):
    if actor.role not in {'admin', 'operator'}:
        raise HTTPException(403, '生成需要运营权限')
    account_id = str(UUID(account_id))
    accounts = client.table('channel_accounts').select('*').eq('id', account_id).limit(1).execute().data or []
    domain = client.table('domains').select('id').eq('slug', domain_slug).limit(1).execute().data or []
    if not accounts or not domain or not accounts[0].get('is_active'):
        raise HTTPException(409, '账号或赛道不可用')
    config = accounts[0].get('config_jsonb') or {}
    strategy = config.get('strategy_profile') or {}
    overrides = strategy.get('prompt_overrides') or {}
    content_branch = branch_selection(overrides)
    project_reference = deepcopy(overrides.get('project_reference') or {})
    if content_branch == 'project_observer' and not project_reference_ready(project_reference):
        return {'status': 'blocked', 'reason': 'project_reference_required',
                'message': '请在账号定位与策略中补齐项目来源、技术用途、优势及输入输出，并核对来源后再生成。'}
    plan = config.get('collection_plan') or {}
    if accounts[0].get('channel') != 'xiaohongshu' or plan.get('mode') != 'xhs_cli' or (config.get('onboarding') or {}).get('domain_slug') != domain_slug:
        raise HTTPException(409, '账号采集方式或赛道不一致')
    initial = get_account_loop_status(client, account_id=account_id, domain_slug=domain_slug).get('loop') or {}
    if not initial.get('onboarding_ready'):
        return {'status': 'blocked', 'reason': 'onboarding_incomplete', 'message': '请先完成账号规划'}
    domain_id = domain[0]['id']
    now = datetime.now(timezone.utc).isoformat()
    local_day = _collection_day_bounds(plan)[0].date().isoformat()
    key = f'cli:{account_id}:{domain_id}:{run_key or local_day}:{source_kind}'[:400]
    if content_branch == 'project_observer':
        content_reference = {k: v for k, v in project_reference.items() if k not in {'checked_at', 'source_notes'}}
        revision = hashlib.sha256(json.dumps(content_reference, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        key += f':{content_branch}:{revision}'
    run_id = str(uuid5(NAMESPACE_URL, key))
    task_id = str(uuid5(NAMESPACE_URL, 'draft:'+key))
    result_base = {'run_key': key, 'run_id': run_id, 'pipeline_task_id': task_id}
    previous = client.table('runs_daily').select('*').eq('id', run_id).eq('account_id', account_id).eq('domain_id', domain_id).limit(1).execute().data or []
    resumed = False
    if previous:
        tasks = client.table('pipeline_tasks').select('status,stage').eq('id', task_id).eq('account_id', account_id).limit(1).execute().data or []
        if tasks and tasks[0]['status'] in {'pending_review', 'approved', 'published', 'metrics_ready', 'done'}:
            return {**result_base, 'status': tasks[0]['status'], 'reused': True, 'message': '已读取本轮现有任务，未重复生成'}
        prior_result = previous[0].get('result_jsonb') or {}
        safe_resume = (bool(run_key) and not tasks and previous[0].get('status') == 'FAILED'
                       and prior_result.get('status') in {'blocked', 'failed'}
                       and prior_result.get('safe_to_resume') is True)
        if not safe_resume:
            return {**result_base, 'status': 'awaiting_status', 'message': '本轮已有执行记录，请刷新任务状态；未重复提交'}
        # Claim only a known pre-draft failure. Uncertain writes and existing tasks
        # require status reconciliation, even if the run ledger says FAILED.
        query = (client.table('runs_daily').update({'status': 'COLLECTING', 'result_jsonb': {}, 'updated_at': now})
                 .eq('id', run_id).eq('account_id', account_id).eq('domain_id', domain_id).eq('status', 'FAILED'))
        query = query.eq('updated_at', previous[0]['updated_at']) if previous[0].get('updated_at') else query.is_('updated_at', 'null')
        created = query.execute().data or []
        if not created:
            return {**result_base, 'status': 'awaiting_status', 'message': '本轮状态已变化，请刷新核对'}
        resumed = True
    else:
        try:
            created = client.table('runs_daily').insert({'id': run_id, 'run_key': key, 'domain_id': domain_id,
                'account_id': account_id, 'flow': 'viewpoint' if source_kind == 'viewpoint' else 'full',
                'status': 'COLLECTING', 'target_posts_min': 1, 'run_date': local_day, 'started_at': now}).execute().data or []
        except Exception:
            previous = client.table('runs_daily').select('id').eq('id', run_id).eq('account_id', account_id).limit(1).execute().data or []
            if previous:
                return {**result_base, 'status': 'awaiting_status', 'message': '执行记录已存在，请刷新状态；未重复提交'}
            raise
    if not created:
        raise HTTPException(503, '执行记录写入未确认，请核对后再试')
    def ledger(state, result):
        saved = client.table('runs_daily').update({'status': state, 'result_jsonb': result,
            'updated_at': datetime.now(timezone.utc).isoformat()}).eq('id', run_id).eq('account_id', account_id).execute().data or []
        if not saved:
            raise HTTPException(503, '执行进度保存未确认，请刷新核对')
    task_write_started = False
    try:
        # Existing bundles can be linked without repeating search/download.
        notes = _current_cli_items(client, domain_id=domain_id, account_id=account_id, collection_plan=plan)
        warnings = []
        valid_items = {ref['item_id'] for ref in _cli_evidence_refs(client, domain_id=domain_id,
            account_id=account_id, collection_plan=plan) if ref['case_ids'] and ref['asset_ids']}
        for note in notes:
            if note['id'] in valid_items:
                continue
            if (note.get('meta_jsonb') or {}).get('browser_capture'):
                try:
                    linked = link_capture_to_knowledge(client, account_id=account_id, domain_id=domain_id, item_id=note['id'], actor=actor)
                    if isinstance(linked, dict):
                        warnings.extend(linked.get('warnings') or [])
                except Exception:
                    warnings.append({'item_id': note['id'], 'reason': 'knowledge_link_unavailable'})
        health = _compute_collection_health(client, domain_id=domain_id, account_id=account_id, collection_plan=plan)
        receipt = {'status': 'reused', 'message': '复用本日已保存素材'}
        if not health['passed']:
            receipt = await asyncio.to_thread(collect_account, client, account_id=account_id,
                domain_slug=domain_slug, source_kind=source_kind, actor=actor)
            health = _compute_collection_health(client, domain_id=domain_id, account_id=account_id, collection_plan=plan)
        receipt = {**receipt, 'warnings': [*(receipt.get('warnings') or []), *warnings]}
        if not health['passed']:
            result = {**result_base, 'status': 'blocked', 'reason': health.get('reason'), 'collection_health': health,
                      'collection': receipt, 'safe_to_resume': True}
            ledger('FAILED', result)
            return result
        refs = [ref for ref in _cli_evidence_refs(client, domain_id=domain_id, account_id=account_id, collection_plan=plan)
                if ref['case_ids'] and ref['asset_ids']]
        strategy = config.get('strategy_profile') or {}
        keywords = strategy.get('focus_keywords') or ['AI']
        topic = f'{str(keywords[0])[:80]}：可执行方法与检查清单'
        intent = {'topic': topic, 'content_branch': content_branch,
            'source_opinion': '面向本账号受众，基于参考素材重新组织可执行方法。不复制原文，不冒充亲测，不虚构数据。仅生成待审核草稿，发布由用户自行完成。',
            'evidence_refs': refs, 'run_key': key}
        if content_branch == 'project_observer':
            intent.update({'topic': f"{project_reference['name']}：项目观察与职业生活共情",
                'project_reference': project_reference,
                'source_opinion': '根据已核对的项目资料生成共情短文；事实来自项目来源，职业画面明确是设想。只生成待审核草稿。'})
        ledger('EVIDENCE_READY', {**result_base, 'evidence_refs': refs, 'collection': receipt})
        task = {'id': task_id, 'domain_id': domain_id, 'account_id': account_id, 'channel': 'xiaohongshu',
            'content_type': 'post', 'status': 'queued', 'stage': 'intake', 'created_by': actor.user_id,
            'intent_jsonb': intent,
            'payload_jsonb': {'channel_account_id': account_id, 'draft_media': {'status': 'awaiting_upload', 'images': []}},
            'review_jsonb': {}, 'publish_jsonb': {}, 'metrics_jsonb': {}}
        try:
            task_write_started = True
            saved = client.table('pipeline_tasks').insert(task).execute().data or []
            if not saved:
                raise RuntimeError('task_write_unconfirmed')
        except Exception:
            ledger('FAILED', {**result_base, 'status': 'awaiting_status', 'message': '草稿创建结果未确认，请刷新核对'})
            return {**result_base, 'status': 'awaiting_status', 'message': '草稿创建结果未确认，请刷新核对'}
        write_audit_log(client, AuditLogEntry(actor=actor.user_id, action='pipeline.task_created',
            target_type='pipeline_task', target_id=task_id, diff_jsonb={
                'domain_slug': domain_slug, 'account_id': account_id, 'channel': 'xiaohongshu',
                'content_type': 'post', 'run_id': run_id, 'run_key': key,
            }))
        ledger('PLANNING', result_base)
        try:
            response = await run_draft(task_id)
        except Exception:
            result = {**result_base, 'status': 'awaiting_status', 'message': '生成结果未确认，请打开任务刷新，勿重复提交'}
            ledger('PLANNING', result)
            return result
        inner = response.get('result') if isinstance(response.get('result'), dict) else response
        status = str(inner.get('status') or response.get('status') or 'awaiting_status')
        tasks = client.table('pipeline_tasks').select('status').eq('id', task_id).eq('account_id', account_id).limit(1).execute().data or []
        if tasks and tasks[0]['status'] == 'pending_review':
            status = 'pending_review'
        elif status == 'ok':
            status = 'awaiting_status'
        result = {**result_base, 'status': status, 'collection': receipt, 'collection_health': health, 'draft': inner, 'resumed': resumed}
        ledger('AWAITING_APPROVAL' if status == 'pending_review' else 'FAILED' if status in {'failed', 'blocked'} else 'PLANNING', result)
        return result
    except HTTPException:
        ledger('FAILED', {**result_base, 'status': 'failed', 'safe_to_resume': not task_write_started})
        raise
    except Exception:
        ledger('FAILED', {**result_base, 'status': 'failed', 'safe_to_resume': not task_write_started,
                          'message': '流程未完成，已保存执行记录'})
        raise HTTPException(503, '流程未完成，请刷新执行记录核对；已保存的素材不会丢失') from None
