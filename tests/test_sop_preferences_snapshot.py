from copy import deepcopy

import pytest

from app.models import Actor
from app.services import account_service
from fake_store import MemoryStore


ACCOUNT = '11111111-1111-4111-8111-111111111111'


def test_sop_snapshot_copies_publication_preferences():
    config = {'publish_preferences': {'next_publish_slot_local': '20:30', 'nested': {'gap': 2}}}
    snapshot = account_service._append_sop_snapshot(config, reason='Confirmed',
        changed_fields=['publish_preferences'], actor='operator')
    assert snapshot['publish_preferences'] == {'next_publish_slot_local': '20:30', 'nested': {'gap': 2}}
    config['publish_preferences']['nested']['gap'] = 9
    assert snapshot['publish_preferences']['nested']['gap'] == 2


@pytest.mark.parametrize('included,previous,expected', [
    (True, {'next_publish_slot_local': '20:30'}, {'next_publish_slot_local': '20:30'}),
    (True, {}, {}),
    (False, None, {'next_publish_slot_local': '09:00'}),
])
def test_sop_rollback_restores_included_preferences_and_preserves_historical_missing(monkeypatch, included, previous, expected):
    db = MemoryStore()
    snapshot = {'version': 1, 'strategy_profile': {}, 'collection_plan': {}, 'feedback_plan': {}}
    if included:
        snapshot['publish_preferences'] = deepcopy(previous)
    db.tables['channel_accounts'] = [{'id': ACCOUNT, 'config_jsonb': {
        'publish_preferences': {'next_publish_slot_local': '09:00'}, 'sop_snapshots': [snapshot],
    }}]
    monkeypatch.setattr(account_service, '_ensure_schema_ready', lambda client: None)
    result = account_service.rollback_account_sop_snapshot(db, account_id=ACCOUNT,
        domain_slug='japan_immigration', version=1, actor=Actor(user_id='operator', role='operator'), reason='Restore')
    config = db.tables['channel_accounts'][0]['config_jsonb']
    assert result['status'] == 'ok'
    assert config['publish_preferences'] == expected
    assert config['sop_snapshots'][-1]['publish_preferences'] == expected
    assert ('publish_preferences' in config['sop_snapshots'][-1]['changed_fields']) is included
    assert db.tables['audit_logs'][-1]['action'] == 'account.sop_rollback'
