import importlib
import importlib.util
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from app.models import Actor
from fake_store import MemoryStore

ACCOUNT = '11111111-1111-4111-8111-111111111111'
DOMAIN = '22222222-2222-4222-8222-222222222222'
TASK = '33333333-3333-4333-8333-333333333333'
NOTE = '6a6191a4000000000103392a'
URL = 'https://www.xiaohongshu.com/explore/'+NOTE


def module():
    assert importlib.util.find_spec('app.services.task_observations'), 'observation service is missing'
    return importlib.import_module('app.services.task_observations')


def database():
    db = MemoryStore()
    db.tables['pipeline_tasks'] = [{'id': TASK, 'account_id': ACCOUNT, 'domain_id': DOMAIN,
        'status': 'published', 'published_at': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),
        'metrics_jsonb': {}, 'publish_jsonb': {'identity': {'feed_id': NOTE, 'published_url': URL}}}]
    return db


def observation(**changes):
    return {'values': {'likes': 0}, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'provider': 'manual', 'source_ref': URL, 'provenance': 'creator_dashboard', **changes}


def save(db, data):
    return module().save_task_observation(db, pipeline_task_id=TASK, account_id=ACCOUNT,
        domain_slug='japan_immigration', observation=data, actor=Actor(user_id='qa', role='admin'))


def test_public_read_keeps_zero_and_leaves_missing_counters_unknown():
    def read(*_a, **_k):
        return {'items': [{'id': NOTE, 'note_card': {'note_id': NOTE,
            'interact_info': {'liked_count': '0', 'collected_count': '15', 'comment_count': '57'}}}]}
    result = module().read_cli_observation(note_id=NOTE, call=read)
    assert result['values'] == {'likes': 0, 'collects': 15, 'comments_count': 57}
    assert result['source_ref'] == URL
    assert 'views' not in result['values'] and 'followers_delta' not in result['values']


def test_public_follow_count_is_not_attributed_follower_growth():
    def read(*_a, **_k):
        return {'items': [{'id': NOTE, 'note_card': {'note_id': NOTE,
            'interact_info': {'liked_count': '0', 'follows': 300}}}]}
    assert module().read_cli_observation(note_id=NOTE, call=read)['values'] == {'likes': 0}


def test_invalid_url_port_returns_validation_error():
    with pytest.raises(HTTPException) as error:
        save(database(), observation(source_ref='https://www.xiaohongshu.com:no/explore/'+NOTE))
    assert error.value.status_code == 422


def test_snapshot_idempotent_and_signed_follows_preserved():
    db = database()
    data = observation(values={'likes': 0, 'followers_delta': -2})
    first, second = save(db, data), save(db, data)
    assert first['metrics_jsonb']['post_metrics'] == {'likes': 0, 'followers_delta': -2}
    assert first['metrics_jsonb'] == second['metrics_jsonb']
    assert len(db.tables['task_metrics']) == 1


@pytest.mark.parametrize('values', [{'likes': True}, {'views': float('nan')}, {'shares': -2}, {'likes': '1.2万'}, {}])
def test_invalid_or_empty_observations_rejected(values):
    with pytest.raises(HTTPException):
        save(database(), observation(values=values))


def test_stale_read_and_foreign_source_do_not_overwrite():
    db = database()
    data = observation()
    save(db, data)
    with pytest.raises(HTTPException) as error:
        save(db, observation(observed_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        save(db, observation(source_ref='https://www.xiaohongshu.com/explore/6ac3b3a200000000140395fd'))
    assert db.tables['pipeline_tasks'][0]['metrics_jsonb']['post_metrics'] == {'likes': 0}
