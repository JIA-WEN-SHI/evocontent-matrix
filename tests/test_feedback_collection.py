from datetime import datetime, timedelta, timezone
from app.models import Actor
from fake_store import MemoryStore

def test_feedback_collection_is_scoped_due_and_preserves_partial_errors(monkeypatch):
    from app.services import feedback_collection as service
    db = MemoryStore()
    domain = db.tables['domains'][0]['id']
    now = datetime.now(timezone.utc)
    db.tables['channel_accounts'] = [{'id': 'a', 'is_active': True, 'config_jsonb': {'collection_plan': {'mode': 'xhs_cli'}}}]
    db.tables['pipeline_tasks'] = [
        {'id': 'due', 'domain_id': domain, 'account_id': 'a', 'status': 'published', 'published_at': (now-timedelta(hours=2)).isoformat(), 'publish_jsonb': {}},
        {'id': 'future', 'domain_id': domain, 'account_id': 'a', 'status': 'published', 'published_at': (now-timedelta(minutes=5)).isoformat(), 'publish_jsonb': {}},
        {'id': 'other', 'domain_id': domain, 'account_id': 'b', 'status': 'published', 'published_at': (now-timedelta(hours=2)).isoformat(), 'publish_jsonb': {}},
    ]
    called = []
    monkeypatch.setattr(service, 'sync_task_observation', lambda client, **kwargs: called.append(kwargs['pipeline_task_id']))
    result = service.collect_due_observations(db, domain_slug='japan_immigration', account_id='a', limit=20, actor=Actor(user_id='qa', role='admin'))
    assert called == ['due']
    assert result['synced'] == 1
