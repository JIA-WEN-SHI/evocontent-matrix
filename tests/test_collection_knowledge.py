import importlib
import importlib.util
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from app.models import Actor
from fake_store import MemoryStore

ACCOUNT = '11111111-1111-4111-8111-111111111111'
DOMAIN = '22222222-2222-4222-8222-222222222222'
ITEM = '33333333-3333-4333-8333-333333333333'


def service():
    assert importlib.util.find_spec('app.services.collection_knowledge'), 'capture knowledge linker is missing'
    return importlib.import_module('app.services.collection_knowledge')


def database():
    db = MemoryStore()
    db.tables['intelligence_items'] = [{'id': ITEM, 'domain_id': DOMAIN, 'account_id': ACCOUNT,
        'source_type': 'hotspot_xiaohongshu_cli', 'captured_at': datetime.now(timezone.utc).isoformat(),
        'meta_jsonb': {'browser_capture': {'title': 'Workflow', 'body_text': 'An example with original source.',
            'source_url': 'https://www.xiaohongshu.com/explore/6a6191a4000000000103392a',
            'author_name': 'Author', 'tags': []}}}]
    return db


def link(db):
    return service().link_capture_to_knowledge(db, account_id=ACCOUNT, domain_id=DOMAIN,
        item_id=ITEM, actor=Actor(user_id='qa', role='admin'))


def test_capture_links_case_and_unverified_reference_asset():
    db = database()
    result = link(db)
    assert result['status'] == 'ok'
    assert len(result['case_ids']) == len(result['asset_ids']) == 1
    assert db.tables['assets'][0]['is_verified'] is False
    assert db.tables['cases'][0]['account_id'] == ACCOUNT
    assert db.tables['cases'][0]['source_ref'] == ITEM
    assert db.tables['intelligence_items'][0]['meta_jsonb']['knowledge_links']['case_ids'] == result['case_ids']


def test_retry_reuses_ids_and_preserves_manual_analysis():
    db = database()
    first = link(db)
    db.tables['cases'][0]['analysis'] = 'Human interpretation'
    second = link(db)
    assert first['case_ids'] == second['case_ids']
    assert first['asset_ids'] == second['asset_ids']
    assert len(db.tables['cases']) == len(db.tables['assets']) == 1
    assert db.tables['cases'][0]['analysis'] == 'Human interpretation'


def test_link_uses_declared_schema_without_updated_at():
    db = database()
    original = db.table
    def table(name):
        query = original(name)
        if name == 'intelligence_items':
            update = query.update
            def checked(values):
                assert 'updated_at' not in values, 'intelligence_items has no declared updated_at column'
                return update(values)
            query.update = checked
        return query
    db.table = table
    assert link(db)['status'] == 'ok'


def test_link_does_not_overwrite_intervening_capture():
    db = database()
    original = db.table
    def table(name):
        query = original(name)
        execute = query.execute
        def concurrent():
            if name == 'intelligence_items' and query.action == 'update':
                db.tables[name][0]['meta_jsonb']['browser_capture'].update(revision='new-capture', comments=[{'text': 'new comment'}])
            return execute()
        query.execute = concurrent
        return query
    db.table = table
    with pytest.raises((RuntimeError, HTTPException)):
        link(db)
    capture = db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']
    assert capture['comments'] == [{'text': 'new comment'}]


def test_foreign_case_conflict_never_overwrites():
    db = database()
    db.tables['cases'] = [{'id': 'foreign', 'domain_id': DOMAIN, 'account_id': 'other',
        'platform': 'xiaohongshu', 'url': db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']['source_url'],
        'title': 'Foreign', 'deleted_at': None}]
    with pytest.raises(HTTPException) as error:
        link(db)
    assert error.value.status_code == 409
    assert db.tables['cases'][0]['title'] == 'Foreign'


def test_asset_failure_does_not_mark_complete_link():
    db = database()
    original = db.table
    def table(name):
        query = original(name)
        if name == 'assets':
            query.insert = lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError())
        return query
    db.table = table
    with pytest.raises(TimeoutError):
        link(db)
    assert 'knowledge_links' not in db.tables['intelligence_items'][0]['meta_jsonb']


def test_cli_gate_counts_unique_persisted_links_not_attempts():
    from app.services.account_service import _compute_collection_health
    db = database()
    link(db)
    link(db)
    result = _compute_collection_health(db, domain_id=DOMAIN, account_id=ACCOUNT,
        collection_plan={'mode': 'xhs_cli', 'loop_gate': {'min_case_per_day': 3, 'min_asset_per_day': 3}})
    assert result['counts'] == {'case': 1, 'asset': 1}
    assert result['passed'] is False
    db.tables['cases'][0]['deleted_at'] = datetime.now(timezone.utc).isoformat()
    result = _compute_collection_health(db, domain_id=DOMAIN, account_id=ACCOUNT, collection_plan={'mode': 'xhs_cli'})
    assert result['counts']['case'] == 0


def test_zero_gate_threshold_is_not_replaced_with_three():
    from app.services.account_service import _compute_collection_health
    result = _compute_collection_health(MemoryStore(), domain_id=DOMAIN, account_id=ACCOUNT,
        collection_plan={'mode': 'xhs_cli', 'loop_gate': {'min_case_per_day': 0, 'min_asset_per_day': 0}})
    assert result['passed'] is True


@pytest.mark.parametrize('threshold,expected', [(0, 0), (None, 3), ('invalid', 3)])
def test_gate_normalization_and_evaluation_agree(threshold, expected):
    from app.services.account_service import _compute_collection_health, _normalize_collection_plan
    raw = {'mode': 'xhs_cli', 'loop_gate': {'min_case_per_day': threshold, 'min_asset_per_day': threshold}}
    plan = _normalize_collection_plan(raw)
    assert plan['loop_gate'] == {'min_case_per_day': expected, 'min_asset_per_day': expected}
    result = _compute_collection_health(MemoryStore(), domain_id=DOMAIN, account_id=ACCOUNT, collection_plan=raw)
    assert result['thresholds'] == plan['loop_gate']
    assert result['passed'] is (expected == 0)


def test_cli_gate_uses_utc_plus_eight_day_and_excludes_future_deleted_items(monkeypatch):
    from app.services import account_service
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(account_service, 'datetime', Clock)
    db = database()
    link(db)
    item = db.tables['intelligence_items'][0]
    item['captured_at'] = '2026-10-06T16:00:00+00:00'
    result = account_service._compute_collection_health(db, domain_id=DOMAIN, account_id=ACCOUNT, collection_plan={'mode': 'xhs_cli'})
    assert result['since'] == '2026-10-06T16:00:00+00:00'
    assert result['counts'] == {'case': 1, 'asset': 1}
    for timestamp in ('2026-10-06T15:59:59+00:00', '2026-10-07T16:00:00+00:00', '2026-10-07T01:00:00+00:00'):
        item['captured_at'] = timestamp
        assert account_service._compute_collection_health(db, domain_id=DOMAIN, account_id=ACCOUNT,
            collection_plan={'mode': 'xhs_cli'})['counts'] == {'case': 0, 'asset': 0}
    item['captured_at'] = '2026-10-06T16:00:00+00:00'
    item['deleted_at'] = '2026-10-07T00:00:00+00:00'
    assert account_service._compute_collection_health(db, domain_id=DOMAIN, account_id=ACCOUNT,
        collection_plan={'mode': 'xhs_cli'})['counts'] == {'case': 0, 'asset': 0}


def test_cli_gate_requires_valid_capture_and_scoped_linked_records():
    from app.services.account_service import _compute_collection_health
    db = database()
    link(db)
    item = db.tables['intelligence_items'][0]
    original = deepcopy(item)
    for changes in ({'body_text': ''}, {'source_url': 'https://evil.test/explore/64abcdef0123456789abcdef'}):
        item['meta_jsonb']['browser_capture'].update(changes)
        assert _compute_collection_health(db, domain_id=DOMAIN, account_id=ACCOUNT,
            collection_plan={'mode': 'xhs_cli'})['counts'] == {'case': 0, 'asset': 0}
        item.update(deepcopy(original))
    db.tables['assets'][0]['account_id'] = 'foreign'
    assert _compute_collection_health(db, domain_id=DOMAIN, account_id=ACCOUNT,
        collection_plan={'mode': 'xhs_cli'})['counts'] == {'case': 1, 'asset': 0}


def test_cli_gate_deduplicates_same_source_across_capture_records():
    from app.services.account_service import _compute_collection_health
    db = database()
    link(db)
    duplicate = deepcopy(db.tables['intelligence_items'][0])
    duplicate['id'] = 'other-item'
    for table, field in (('cases', 'case_ids'), ('assets', 'asset_ids')):
        entity = deepcopy(db.tables[table][0])
        entity['id'] = 'other-' + table
        db.tables[table].append(entity)
        duplicate['meta_jsonb']['knowledge_links'][field] = [entity['id']]
    db.tables['intelligence_items'].append(duplicate)
    assert _compute_collection_health(db, domain_id=DOMAIN, account_id=ACCOUNT,
        collection_plan={'mode': 'xhs_cli'})['counts'] == {'case': 1, 'asset': 1}
