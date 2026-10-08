import json
import pytest
from fastapi import HTTPException
from app.models import Actor, PendingStrategyApplyRequest
from app.routers import ops
from app.services import pipeline_service, strategy_confirmation
from fake_store import MemoryStore, Query

ACCOUNT = '11111111-1111-4111-8111-111111111111'
TASK = '33333333-3333-4333-8333-333333333333'

def test_approval_does_not_discard_concurrent_image_upload(monkeypatch):
    db = MemoryStore()
    db.tables['pipeline_tasks'] = [{'id': TASK, 'account_id': ACCOUNT, 'domain_id': db.tables['domains'][0]['id'],
        'channel': 'xiaohongshu', 'status': 'pending_review', 'payload_jsonb': {'title': 'Test', 'body': 'Body'}, 'updated_at': 'old'}]
    original = Query.execute
    def race(query):
        if query.action == 'update' and query.payload.get('status') == 'approved':
            db.tables['pipeline_tasks'][0]['updated_at'] = 'new'
            db.tables['pipeline_tasks'][0]['payload_jsonb']['draft_media'] = {'images': [{'id': 'image'}]}
        return original(query)
    monkeypatch.setattr(Query, 'execute', race)
    with pytest.raises(HTTPException) as error:
        pipeline_service.approve_pipeline_task(db, TASK, Actor(user_id='qa', role='admin'))
    assert error.value.status_code == 409
    assert db.tables['pipeline_tasks'][0]['payload_jsonb']['draft_media']['images']
    assert db.tables['pipeline_tasks'][0]['status'] == 'pending_review'

def test_confirmation_route_recovers_receipt_before_regenerating(monkeypatch):
    db = MemoryStore()
    domain = db.tables['domains'][0]['id']
    db.tables['memory_items'] = [{'id': TASK, 'domain_id': domain, 'account_id': ACCOUNT, 'status': 'pending', 'content': '{}'}]
    db.tables['channel_accounts'] = [{'id': ACCOUNT, 'config_jsonb': {'strategy_confirmation_receipts': {TASK: {'stage': 'configured'}}}}]
    monkeypatch.setattr(ops, '_build_pending_strategy_proposals', lambda *a, **k: [])
    monkeypatch.setattr(strategy_confirmation, 'apply_confirmed_strategy', lambda *a, **k: {'version': 2})
    result = ops.apply_pending_strategy_item(TASK, PendingStrategyApplyRequest(domain_slug='japan_immigration', account_id=ACCOUNT, target='feedback_plan'), db, Actor(user_id='qa', role='admin'))
    assert result['version'] == 2

def test_proposal_display_uses_persisted_change_and_hold_is_non_executable():
    proposal = {'target': 'feedback_plan', 'before': {}, 'after': {'checkpoints_hours': [2, 24]}}
    row = {'content': json.dumps({'proposed_changes': [proposal]})}
    resolved = ops._resolve_pending_strategy_proposals(row, account_id=ACCOUNT, account_config={})
    assert resolved[0]['after'] == proposal['after']
    assert resolved[0]['proposal_fingerprint']
    assert ops._resolve_pending_strategy_proposals({'content': '{"hold":true}'}, account_id=ACCOUNT, account_config={}) == []
