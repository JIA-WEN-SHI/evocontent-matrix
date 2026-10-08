from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from app.main import app
from app.db import get_supabase
from fake_store import MemoryStore

ACCOUNT = '11111111-1111-4111-8111-111111111111'
TASK = '33333333-3333-4333-8333-333333333333'
HEADERS = {'x-user-id': 'qa', 'x-user-role': 'admin'}


def test_new_workflow_routes_are_real_and_role_protected():
    required = [
        f'/api/pipeline/tasks/{TASK}/manual-publication',
        f'/api/pipeline/tasks/{TASK}/feedback/observations',
        f'/api/pipeline/tasks/{TASK}/feedback/sync',
        f'/api/pipeline/tasks/{TASK}/images',
        f'/api/ops/accounts/{ACCOUNT}/collection/reconcile-knowledge',
    ]
    with TestClient(app) as client:
        for path in required:
            response = client.post(path, json={}, headers={'x-user-id': 'qa', 'x-user-role': 'reviewer'})
            assert response.status_code == 403, (path, response.text)


def test_manual_publication_endpoint_advances_task_without_publishing():
    db = MemoryStore()
    domain_id = db.tables['domains'][0]['id']
    db.tables['channel_accounts'] = [{'id': ACCOUNT, 'channel': 'xiaohongshu', 'is_active': True,
        'config_jsonb': {'onboarding': {'domain_slug': 'japan_immigration'}}}]
    now = datetime.now(timezone.utc).isoformat()
    db.tables['pipeline_tasks'] = [{'id': TASK, 'domain_id': domain_id, 'account_id': ACCOUNT,
        'channel': 'xiaohongshu', 'content_type': 'post', 'status': 'approved', 'stage': 'human_review',
        'created_by': 'qa', 'created_at': now, 'updated_at': now,
        'payload_jsonb': {'title': 'Test', 'body': 'Original draft'}}]
    app.dependency_overrides[get_supabase] = lambda: db
    try:
        with TestClient(app) as client:
            response = client.post(f'/api/pipeline/tasks/{TASK}/manual-publication', headers=HEADERS, json={
                'account_id': ACCOUNT, 'domain_slug': 'japan_immigration', 'confirmed': True,
                'published_url': 'https://www.xiaohongshu.com/explore/6a6191a4000000000103392a',
                'published_at': (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()})
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'published'
            assert response.json()['stage'] == 'feedback_pending'
    finally:
        app.dependency_overrides.clear()


def test_strategy_apply_route_delegates_to_confirmed_snapshot_service(monkeypatch):
    from app.routers import ops
    from app.models import PendingStrategyApplyRequest, Actor
    from app.services import strategy_confirmation
    db = MemoryStore()
    domain = db.tables['domains'][0]['id']
    db.tables['memory_items'] = [{'id': TASK, 'domain_id': domain, 'account_id': ACCOUNT, 'status': 'pending', 'content': '{}'}]
    db.tables['channel_accounts'] = [{'id': ACCOUNT, 'account_name': 'Test', 'config_jsonb': {}}]
    called = []
    monkeypatch.setattr(ops, '_build_pending_strategy_proposals', lambda *args, **kwargs: [{'target': 'feedback_plan', 'before': {}, 'after': {}}])
    monkeypatch.setattr(strategy_confirmation, 'apply_confirmed_strategy', lambda *args, **kwargs: called.append(kwargs) or {'status': 'ok', 'version': 17})
    result = ops.apply_pending_strategy_item(TASK, PendingStrategyApplyRequest(domain_slug='japan_immigration', account_id=ACCOUNT, target='feedback_plan'), db, Actor(user_id='qa', role='admin'))
    assert result['version'] == 17
    assert called[0]['account_id'] == ACCOUNT
