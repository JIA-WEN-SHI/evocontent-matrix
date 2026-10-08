import importlib
import importlib.util
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from app.models import Actor
from fake_store import MemoryStore

ACCOUNT = '11111111-1111-4111-8111-111111111111'
DOMAIN = '22222222-2222-4222-8222-222222222222'

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def module(monkeypatch, ready=True):
    assert importlib.util.find_spec('app.services.cli_content_loop'), 'CLI draft loop is missing'
    m = importlib.import_module('app.services.cli_content_loop')
    monkeypatch.setattr(m, 'get_account_loop_status', lambda *_a, **_k: {'loop': {'onboarding_ready': ready}})
    return m


def database():
    db = MemoryStore()
    db.tables['domains'][0]['slug'] = 'ai_content'
    db.tables['channel_accounts'] = [{'id': ACCOUNT, 'channel': 'xiaohongshu', 'is_active': True,
        'config_jsonb': {'onboarding': {'domain_slug': 'ai_content'}, 'collection_plan': {'mode': 'xhs_cli',
            'loop_gate': {'min_case_per_day': 0, 'min_asset_per_day': 0}}, 'strategy_profile': {'focus_keywords': ['AI meetings']}}}]
    return db


async def test_ready_cli_loop_drafts_once_with_same_execution_key(monkeypatch):
    m = module(monkeypatch)
    db = database()
    calls = []
    async def generate(task_id):
        calls.append(task_id)
        db.table('pipeline_tasks').update({'status': 'pending_review', 'stage': 'human_review'}).eq('id', task_id).execute()
        return {'status': 'ok'}
    kw = dict(account_id=ACCOUNT, domain_slug='ai_content', source_kind='hotspot', run_key='test-once',
        actor=Actor(user_id='qa', role='admin'), run_draft=generate)
    first = await m.run_cli_content_loop(db, **kw)
    second = await m.run_cli_content_loop(db, **kw)
    assert first['status'] == second['status'] == 'pending_review'
    assert first['pipeline_task_id'] == second['pipeline_task_id']
    assert len(calls) == len(db.tables['pipeline_tasks']) == 1


async def test_project_branch_snapshots_source_without_tutorial_intent(monkeypatch):
    m = module(monkeypatch)
    db = database()
    ref = {'name': 'Fooocus', 'source_url': 'https://github.com/lllyasviel/Fooocus',
           'summary': '基于 SDXL 生成图片', 'advantages': '离线运行',
           'input': '画面描述', 'output': '图片', 'technology_keywords': ['SDXL'],
           'checked_at': '2026-10-07T08:00:00Z'}
    db.tables['channel_accounts'][0]['config_jsonb']['strategy_profile']['prompt_overrides'] = {
        'content_branch': 'project_observer', 'project_reference': ref}
    async def generate(task_id):
        db.table('pipeline_tasks').update({'status': 'pending_review'}).eq('id', task_id).execute()
        return {'status': 'ok'}
    result = await m.run_cli_content_loop(db, **loop_args(generate, 'observer'))
    assert result['status'] == 'pending_review'
    intent = db.tables['pipeline_tasks'][0]['intent_jsonb']
    assert intent['content_branch'] == 'project_observer'
    assert intent['project_reference'] == ref
    assert '可执行方法' not in intent['source_opinion'] and '检查清单' not in intent['topic']
    ref['checked_at'] = '2026-10-07T09:00:00Z'
    ref['source_notes'] = '重新核对来源'
    reused = await m.run_cli_content_loop(db, **loop_args(generate, 'observer'))
    assert reused['pipeline_task_id'] == result['pipeline_task_id'] and reused['reused']
    db.tables['channel_accounts'][0]['config_jsonb']['strategy_profile']['prompt_overrides']['content_branch'] = 'tutorial'
    again = await m.run_cli_content_loop(db, **loop_args(generate, 'observer'))
    assert again['pipeline_task_id'] != result['pipeline_task_id']


async def test_project_branch_missing_source_stops_before_task_creation(monkeypatch):
    m = module(monkeypatch)
    db = database()
    db.tables['channel_accounts'][0]['config_jsonb']['strategy_profile']['prompt_overrides'] = {'content_branch': 'project_observer'}
    async def generate(_id):
        pytest.fail('missing source must not reach model')
    result = await m.run_cli_content_loop(db, **loop_args(generate, 'no-project'))
    assert result['status'] == 'blocked' and result['reason'] == 'project_reference_required'
    assert not db.tables.get('pipeline_tasks')


async def test_unplanned_account_stops_before_drafting(monkeypatch):
    m = module(monkeypatch, ready=False)
    async def generate(_task_id):
        pytest.fail('must not draft')
    result = await m.run_cli_content_loop(database(), account_id=ACCOUNT, domain_slug='ai_content',
        source_kind='hotspot', run_key='blocked', actor=Actor(user_id='qa', role='admin'), run_draft=generate)
    assert result['status'] == 'blocked'


async def test_empty_account_collects_before_gate_and_propagates_failure(monkeypatch):
    m = module(monkeypatch)
    db = database()
    db.tables['channel_accounts'][0]['config_jsonb']['collection_plan']['loop_gate'] = {'min_case_per_day': 1, 'min_asset_per_day': 1}
    events = []
    monkeypatch.setattr(m, '_compute_collection_health', lambda *_a, **_k: {'passed': bool(events), 'counts': {}})
    def collect(*_a, **_k):
        events.append('collected')
        return {'status': 'partial', 'warnings': ['comments failed']}
    monkeypatch.setattr(m, 'collect_account', collect)
    async def generate(_id):
        events.append('drafted')
        return {'status': 'ok', 'result': {'status': 'blocked', 'reason': 'insufficient evidence'}}
    result = await m.run_cli_content_loop(db, account_id=ACCOUNT, domain_slug='ai_content',
        source_kind='hotspot', run_key='collect-first', actor=Actor(user_id='qa', role='admin'), run_draft=generate)
    assert events == ['collected', 'drafted']
    assert result['status'] == 'blocked'


async def test_unknown_draft_write_is_not_replayed(monkeypatch):
    m = module(monkeypatch)
    db = database()
    calls = []
    async def generate(_id):
        calls.append(_id)
        raise TimeoutError()
    kw = dict(account_id=ACCOUNT, domain_slug='ai_content', source_kind='hotspot', run_key='uncertain',
        actor=Actor(user_id='qa', role='admin'), run_draft=generate)
    first = await m.run_cli_content_loop(db, **kw)
    second = await m.run_cli_content_loop(db, **kw)
    assert first['status'] == second['status'] == 'awaiting_status'
    assert len(calls) == 1


def loop_args(generate, run_key='bounded-loop'):
    return dict(account_id=ACCOUNT, domain_slug='ai_content', source_kind='hotspot', run_key=run_key,
        actor=Actor(user_id='qa', role='admin'), run_draft=generate)


def capture(db, item_id, note_id, captured_at):
    url = f'https://www.xiaohongshu.com/explore/{note_id}'
    row = {'id': item_id, 'account_id': ACCOUNT, 'domain_id': DOMAIN,
        'source_type': 'hotspot_xiaohongshu_cli', 'captured_at': captured_at,
        'meta_jsonb': {'browser_capture': {'source_url': url, 'title': 'Reference', 'body_text': 'Saved reference body'}}}
    db.tables.setdefault('intelligence_items', []).append(row)
    return row


async def test_bad_saved_note_is_warning_and_does_not_abort_valid_draft(monkeypatch):
    m = module(monkeypatch)
    db = database()
    capture(db, '44444444-4444-4444-8444-444444444444', '64abcdef0123456789abcdef', datetime.now(timezone.utc).isoformat())
    def bad_link(*args, **kwargs):
        from fastapi import HTTPException
        raise HTTPException(422, 'Invalid historical capture')
    monkeypatch.setattr(m, 'link_capture_to_knowledge', bad_link)
    calls = []
    async def generate(task_id):
        calls.append(task_id)
        return {'status': 'blocked'}
    result = await m.run_cli_content_loop(db, **loop_args(generate))
    assert calls
    assert result['collection']['warnings']
    assert db.tables['pipeline_tasks'][0]['intent_jsonb']['evidence_refs'] == []


async def test_draft_uses_only_today_scoped_valid_distinct_evidence(monkeypatch):
    m = module(monkeypatch)
    db = database()
    now = datetime.now(timezone.utc)
    current = capture(db, '44444444-4444-4444-8444-444444444444', '64abcdef0123456789abcdef', now.isoformat())
    stale = capture(db, '55555555-5555-4555-8555-555555555555', '65abcdef0123456789abcdef', (now - timedelta(days=2)).isoformat())
    future = capture(db, '66666666-6666-4666-8666-666666666666', '66abcdef0123456789abcdef', (now + timedelta(days=2)).isoformat())
    duplicate = capture(db, '77777777-7777-4777-8777-777777777777', '64abcdef0123456789abcdef', now.isoformat())
    from app.services.collection_knowledge import link_capture_to_knowledge
    for row in (current, stale, future, duplicate):
        link_capture_to_knowledge(db, account_id=ACCOUNT, domain_id=DOMAIN, item_id=row['id'], actor=Actor(user_id='qa', role='admin'))
    invalid = capture(db, '88888888-8888-4888-8888-888888888888', '67abcdef0123456789abcdef', now.isoformat())
    invalid['meta_jsonb']['knowledge_links'] = {'status': 'ok', 'source_url': invalid['meta_jsonb']['browser_capture']['source_url'],
        'case_ids': ['missing-case'], 'asset_ids': ['missing-asset']}
    foreign = deepcopy(current)
    foreign.update(id='foreign', account_id='other-account')
    db.tables['intelligence_items'].append(foreign)
    monkeypatch.setattr(m, 'link_capture_to_knowledge', lambda *args, **kwargs: None)
    async def generate(_id):
        return {'status': 'blocked'}
    await m.run_cli_content_loop(db, **loop_args(generate))
    refs = db.tables['pipeline_tasks'][0]['intent_jsonb']['evidence_refs']
    assert len(refs) == 1
    assert refs[0]['source_url'] == current['meta_jsonb']['browser_capture']['source_url']
    assert refs[0]['case_ids'] == current['meta_jsonb']['knowledge_links']['case_ids']


async def test_blocked_gate_without_draft_can_explicitly_resume_same_key(monkeypatch):
    m = module(monkeypatch)
    db = database()
    ready = []
    monkeypatch.setattr(m, '_compute_collection_health', lambda *a, **k: {'passed': bool(ready), 'reason': 'collection_insufficient'})
    monkeypatch.setattr(m, 'collect_account', lambda *a, **k: {'status': 'partial'})
    calls = []
    async def generate(task_id):
        calls.append(task_id)
        db.table('pipeline_tasks').update({'status': 'pending_review'}).eq('id', task_id).execute()
        return {'status': 'ok'}
    kw = loop_args(generate)
    first = await m.run_cli_content_loop(db, **kw)
    assert first['status'] == 'blocked'
    ready.append(True)
    second = await m.run_cli_content_loop(db, **kw)
    assert second['status'] == 'pending_review'
    assert second['run_id'] == first['run_id']
    assert len(db.tables['runs_daily']) == len(db.tables['pipeline_tasks']) == len(calls) == 1


async def test_known_collection_failure_without_draft_can_resume(monkeypatch):
    m = module(monkeypatch)
    db = database()
    monkeypatch.setattr(m, '_compute_collection_health', lambda *a, **k: {'passed': False, 'reason': 'collection_insufficient'})
    def failed_collect(*a, **k):
        raise TimeoutError('Collection failed before drafting')
    monkeypatch.setattr(m, 'collect_account', failed_collect)
    async def generate(_id):
        db.tables['pipeline_tasks'][0]['status'] = 'pending_review'
        return {'status': 'ok'}
    kw = loop_args(generate)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await m.run_cli_content_loop(db, **kw)
    monkeypatch.setattr(m, '_compute_collection_health', lambda *a, **k: {'passed': True})
    assert (await m.run_cli_content_loop(db, **kw))['status'] == 'pending_review'


async def test_unknown_task_insert_without_row_cannot_resume(monkeypatch):
    m = module(monkeypatch)
    db = database()
    original = db.table
    attempts = []
    def table(name):
        query = original(name)
        if name == 'pipeline_tasks':
            def insert(payload):
                attempts.append(payload)
                raise TimeoutError('Unknown task write')
            query.insert = insert
        return query
    db.table = table
    async def generate(_id):
        pytest.fail('Unknown task insert must not execute')
    kw = loop_args(generate)
    assert (await m.run_cli_content_loop(db, **kw))['status'] == 'awaiting_status'
    assert (await m.run_cli_content_loop(db, **kw))['status'] == 'awaiting_status'
    assert len(attempts) == 1


async def test_active_run_without_task_cannot_execute_again(monkeypatch):
    m = module(monkeypatch)
    db = database()
    monkeypatch.setattr(m, '_compute_collection_health', lambda *a, **k: {'passed': False, 'reason': 'collection_insufficient'})
    monkeypatch.setattr(m, 'collect_account', lambda *a, **k: {'status': 'partial'})
    async def generate(_id):
        pytest.fail('Active run must not execute again')
    kw = loop_args(generate)
    await m.run_cli_content_loop(db, **kw)
    db.tables['runs_daily'][0]['status'] = 'COLLECTING'
    assert (await m.run_cli_content_loop(db, **kw))['status'] == 'awaiting_status'


async def test_created_draft_has_standard_audit(monkeypatch):
    m = module(monkeypatch)
    db = database()
    async def generate(_id):
        return {'status': 'blocked'}
    result = await m.run_cli_content_loop(db, **loop_args(generate))
    audits = [row for row in db.tables.get('audit_logs', []) if row['action'] == 'pipeline.task_created']
    assert len(audits) == 1
    assert audits[0]['target_id'] == result['pipeline_task_id']
    assert audits[0]['actor'] == 'qa'
