from copy import deepcopy
import importlib
import json

import pytest
from fastapi import HTTPException

from app.models import Actor
from fake_store import MemoryStore


ACCOUNT = '11111111-1111-4111-8111-111111111111'
ITEM = '33333333-3333-4333-8333-333333333333'
DOMAIN = '22222222-2222-4222-8222-222222222222'
NOTE = '6a6191a4000000000103392a'
URL = 'https://www.xiaohongshu.com/explore/' + NOTE
ACTOR = Actor(user_id='qa', role='operator')


def module():
    return importlib.import_module('app.services.xhs_cli_collection')


def capture():
    return {'schema_version': 1, 'source_url': URL, 'title': 'Saved note',
        'body_text': 'Original author body', 'author_name': 'Author', 'tags': ['AI'],
        'body_status': 'complete', 'comments_status': 'partial', 'images_status': 'complete',
        'comments': [], 'image_candidates': [{'key': '7', 'index': 7,
            'url': 'https://sns-webpic-qc.xhscdn.com/public.webp'}],
        'truncation_reasons': [], 'captured_at': '2026-01-01T00:00:00+00:00'}


def comment(key, **changes):
    return {'id': key, 'note_id': NOTE, 'content': 'Comment ' + key,
        'user_info': {'nickname': 'Reader'}, 'create_time': 123, **changes}


def database():
    db = MemoryStore()
    db.tables['domains'][0]['slug'] = 'ai_content'
    db.tables['channel_accounts'] = [{'id': ACCOUNT, 'channel': 'xiaohongshu', 'is_active': True,
        'config_jsonb': {'onboarding': {'domain_slug': 'ai_content'},
            'collection_plan': {'mode': 'xhs_cli'}}}]
    saved = {**capture(), 'images': [{'id': 'image-id', 'index': 7, 'status': 'saved',
        'sha256': 'a' * 64, 'extension': 'webp', 'candidate_url': capture()['image_candidates'][0]['url']}],
        'revision': 'original', 'summary': 'Saved summary', 'collection_method': 'xiaohongshu_cli'}
    db.tables['intelligence_items'] = [{'id': ITEM, 'account_id': ACCOUNT, 'domain_id': DOMAIN,
        'source_type': 'hotspot_xiaohongshu_cli', 'source_url': URL, 'raw_text': 'Unchanged raw text',
        'deleted_at': None, 'meta_jsonb': {'browser_capture': saved, 'knowledge_link': {'case_id': 'case'}}}]
    return db


def paginate(pages, *, value=None, deadline=200, monkeypatch=None):
    mod = module()
    value = value if value is not None else capture()
    requests = []
    def call(command, *args, timeout):
        assert command == 'comments' and args[0] == NOTE
        assert '--all' not in args and '--xsec-token' not in args
        assert 0 < timeout <= 20
        requests.append(args)
        page = pages[len(requests) - 1]
        if isinstance(page, Exception):
            raise page
        return page
    if monkeypatch:
        monkeypatch.setattr(mod.time, 'monotonic', lambda: 0)
    assert hasattr(mod, 'collect_comments'), 'Bounded comment pagination is missing'
    mod.collect_comments(value, NOTE, call=call, deadline=deadline)
    return value, requests


def retry(db, tmp_path, call, **changes):
    mod = module()
    assert hasattr(mod, 'retry_capture_comments'), 'Comment-only capture retry is missing'
    return mod.retry_capture_comments(db, account_id=changes.pop('account_id', ACCOUNT),
        item_id=changes.pop('item_id', ITEM), actor=changes.pop('actor', ACTOR),
        call=call, media_root=tmp_path, **changes)


def authenticated(pages):
    requests = []
    def call(command, *args, timeout):
        assert 0 < timeout <= 20
        requests.append((command, args))
        if command == 'status':
            return {'authenticated': True, 'guest': False}
        assert command == 'comments' and args[0] == NOTE
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page
    return call, requests


def test_pagination_follows_explicit_cursor_and_deduplicates_replies(monkeypatch):
    parent = comment('c1', sub_comments=[comment('r1')], sub_comment_count=1)
    result, requests = paginate([
        {'has_more': True, 'cursor': 'next', 'comments': [parent]},
        {'has_more': False, 'cursor': '', 'comments': [parent, comment('c2')]},
    ], monkeypatch=monkeypatch)
    assert requests == [(NOTE,), (NOTE, '--cursor', 'next')]
    assert [(c['key'], c['parent_key']) for c in result['comments']] == [('c1', None), ('r1', 'c1'), ('c2', None)]
    assert result['comments_status'] == 'complete'
    assert result['body_text'] == 'Original author body'
    assert result['truncation_reasons'] == []


def test_pagination_stops_after_five_pages(monkeypatch):
    result, requests = paginate([{'has_more': True, 'cursor': str(i), 'comments': [comment(str(i))]}
        for i in range(6)], monkeypatch=monkeypatch)
    assert len(requests) == 5 and len(result['comments']) == 5
    assert result['comments_status'] == 'partial' and result['truncation_reasons']


def test_comment_limit_includes_replies_and_stops_before_next_page(monkeypatch):
    rows = [comment(str(i), sub_comments=[comment('r' + str(i))], sub_comment_count=1) for i in range(51)]
    result, requests = paginate([{'has_more': True, 'cursor': 'next', 'comments': rows}], monkeypatch=monkeypatch)
    assert len(requests) == 1 and len(result['comments']) == 100
    assert result['comments'][-1]['key'] == 'r49'
    assert result['comments_status'] == 'partial'


@pytest.mark.parametrize('cursor', ['same', '', None])
def test_repeated_or_missing_cursor_stops_with_partial_status(monkeypatch, cursor):
    pages = [{'has_more': True, 'cursor': 'same', 'comments': [comment('c1')]},
        {'has_more': True, 'cursor': cursor, 'comments': [comment('c2')]}]
    result, requests = paginate(pages, monkeypatch=monkeypatch)
    assert len(requests) == 2 and result['comments_status'] == 'partial'
    assert result['truncation_reasons']


@pytest.mark.parametrize('row', [comment('c1', sub_comment_count=2, sub_comments=[comment('r1')]),
    comment('c1', content='x' * 2001), comment('c1', sub_comment_has_more=True)])
def test_incomplete_replies_and_truncated_text_stay_partial(monkeypatch, row):
    result, _ = paginate([{'has_more': False, 'comments': [row]}], monkeypatch=monkeypatch)
    assert result['comments_status'] == 'partial' and result['truncation_reasons']


@pytest.mark.parametrize('page', [
    {'has_more': False, 'comments': [comment('bad', note_id='a' * 24)]},
    {'has_more': False, 'comments': [comment('bad', sub_comments=[comment('r', note_id='a' * 24)])]},
    {'has_more': False, 'note_id': 'a' * 24, 'comments': []},
    {'has_more': False, 'comments': [None]},
    {'has_more': False, 'comments': None},
    {'comments': []},
    {'has_more': 'false', 'comments': []},
])
def test_invalid_page_fails_without_admitting_foreign_or_unknown_evidence(monkeypatch, page):
    result, _ = paginate([page], monkeypatch=monkeypatch)
    assert result['comments_status'] == 'failed' and result['comments'] == []
    assert result['truncation_reasons']


def test_second_page_failure_retains_first_page(monkeypatch):
    result, _ = paginate([{'has_more': True, 'cursor': 'next', 'comments': [comment('c1')]},
        module().CliFailure('api_error')], monkeypatch=monkeypatch)
    assert result['comments_status'] == 'failed'
    assert result['comments'][0]['key'] == 'c1'
    assert 'api_error' in ' '.join(result['truncation_reasons'])


def test_deadline_prevents_call_and_shrinks_remaining_timeout(monkeypatch):
    mod = module()
    assert hasattr(mod, 'collect_comments'), 'Bounded comment pagination is missing'
    value = capture()
    clock = [199.5]
    monkeypatch.setattr(mod.time, 'monotonic', lambda: clock[0])
    def call(command, *args, timeout):
        assert timeout == .5
        clock[0] = 200
        return {'has_more': True, 'cursor': 'next', 'comments': [comment('c1')]}
    mod.collect_comments(value, NOTE, call=call, deadline=200)
    assert value['comments_status'] == 'partial' and len(value['comments']) == 1
    empty = capture()
    mod.collect_comments(empty, NOTE, call=lambda *_a, **_k: pytest.fail('Deadline expired'), deadline=200)
    assert empty['comments_status'] == 'partial' and empty['comments'] == []


def test_retry_only_comments_preserves_body_images_metadata_and_updates_bundle(tmp_path):
    db = database()
    original = deepcopy(db.tables['intelligence_items'][0])
    call, requests = authenticated([{'has_more': False, 'comments': [comment('new')]}])
    result = retry(db, tmp_path, call)
    row = db.tables['intelligence_items'][0]
    saved = row['meta_jsonb']['browser_capture']
    assert requests == [('status', ()), ('comments', (NOTE,))]
    assert result['status'] == 'ok' and result['comments_status'] == 'complete'
    assert result['capture_status'] == 'complete' and result['warnings'] == []
    assert result['database_status'] == 'saved' and result['capture'] == saved
    for key in ['body_text', 'title', 'tags', 'captured_at', 'image_candidates', 'images', 'summary']:
        assert saved[key] == original['meta_jsonb']['browser_capture'][key]
    assert row['raw_text'] == original['raw_text']
    assert row['meta_jsonb']['knowledge_link'] == original['meta_jsonb']['knowledge_link']
    bundle = json.loads((tmp_path / ACCOUNT / ITEM / 'capture.json').read_text(encoding='utf-8'))
    assert bundle['comments'][0]['key'] == 'new' and bundle['images'][0]['index'] == 7
    assert bundle['database_status'] == 'saved'
    assert 'Original author body' in (tmp_path / ACCOUNT / ITEM / 'note.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('code', ['api_error', 'need_verify', 'not_authenticated', 'cli_timeout'])
def test_retry_failure_retains_prior_comments_without_empty_claim(tmp_path, code):
    db = database()
    prior = {'key': 'old', 'parent_key': None, 'text': 'Prior comment', 'author_name': 'Reader'}
    db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']['comments'] = [prior]
    call, requests = authenticated([module().CliFailure(code)])
    result = retry(db, tmp_path, call)
    assert len(requests) == 2 and result['status'] == 'partial'
    assert result['comments_status'] == 'failed' and result['capture']['comments'][0]['key'] == 'old'
    assert code in ' '.join(result['warnings'])
    assert result['capture']['comments_status'] != 'empty'
    bundle = json.loads((tmp_path / ACCOUNT / ITEM / 'capture.json').read_text(encoding='utf-8'))
    assert bundle['comments'][0]['key'] == 'old' and bundle['comments_status'] == 'failed'


def test_unauthenticated_retry_never_calls_comments(tmp_path):
    def call(command, *args, timeout):
        assert command == 'status'
        return {'authenticated': False, 'guest': True}
    result = retry(database(), tmp_path, call)
    assert result['comments_status'] == 'failed'
    assert 'not_authenticated' in ' '.join(result['warnings'])


@pytest.mark.parametrize('change,status', [('role', 403), ('account', 404), ('domain', 409),
    ('inactive', 409), ('channel', 409), ('mode', 409), ('deleted', 404),
    ('source', 409), ('url', 409), ('capture_url', 409), ('uuid', 422)])
def test_invalid_scope_rejected_before_cli_and_disk(tmp_path, change, status):
    db = database()
    account = db.tables['channel_accounts'][0]
    row = db.tables['intelligence_items'][0]
    changes = {}
    if change == 'role': changes['actor'] = Actor(user_id='qa', role='viewer')
    if change == 'account': row['account_id'] = 'other'
    if change == 'domain': row['domain_id'] = 'other'
    if change == 'inactive': account['is_active'] = False
    if change == 'channel': account['channel'] = 'douyin'
    if change == 'mode': account['config_jsonb']['collection_plan']['mode'] = 'browser_ui'
    if change == 'deleted': row['deleted_at'] = '2026-10-07'
    if change == 'source': row['source_type'] = 'hotspot_chrome_browser_ui'
    if change == 'url': row['source_url'] = 'https://evil.example/explore/' + NOTE
    if change == 'capture_url': row['meta_jsonb']['browser_capture']['source_url'] = URL[:-1] + 'b'
    if change == 'uuid': changes['item_id'] = '../outside'
    with pytest.raises(HTTPException) as error:
        retry(db, tmp_path, lambda *_a, **_k: pytest.fail('No CLI for invalid scope'), **changes)
    assert error.value.status_code == status
    assert not list(tmp_path.iterdir())


def test_retry_keeps_new_comments_locally_when_database_write_is_unconfirmed(tmp_path):
    db = database()
    table = db.table
    def unavailable(name):
        query = table(name)
        execute = query.execute
        def run():
            if name == 'intelligence_items' and query.action == 'update':
                raise TimeoutError('unavailable')
            return execute()
        query.execute = run
        return query
    db.table = unavailable
    call, _ = authenticated([{'has_more': False, 'comments': [comment('new')]}])
    result = retry(db, tmp_path, call)
    assert result['status'] == 'partial' and result['database_status'] == 'unconfirmed'
    assert result['warnings']
    assert db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']['comments'] == []
    bundle = json.loads((tmp_path / ACCOUNT / ITEM / 'capture.json').read_text(encoding='utf-8'))
    assert bundle['comments'][0]['key'] == 'new' and bundle['database_status'] == 'unconfirmed'


def test_collection_uses_pagination_and_keeps_tokens_out_of_capture(tmp_path):
    from test_xhs_cli_collection import cli, collect, database as collection_database
    requests = []
    def call(command, *args, **kwargs):
        if command == 'comments':
            requests.append(args)
            if len(requests) == 1:
                return {'has_more': True, 'cursor': 'next', 'comments': [comment('c1')]}
            return {'has_more': False, 'comments': [comment('c2')]}
        return cli(command, *args, **kwargs)
    db = collection_database()
    result = collect(db, tmp_path, call=call)
    saved = db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']
    assert requests == [(NOTE,), (NOTE, '--cursor', 'next')]
    assert saved['comments_status'] == 'complete' and len(saved['comments']) == 2
    assert result['preview'][0]['knowledge_status'] in {'linked', 'partial', 'pending', 'ok'}
    assert 'NEVER_SAVE_THIS' not in json.dumps(result) + json.dumps(db.tables) + next(tmp_path.rglob('capture.json')).read_text(encoding='utf-8')


def test_verified_empty_page_is_distinct_from_failure(monkeypatch):
    result, requests = paginate([{'has_more': False, 'comments': []}], monkeypatch=monkeypatch)
    assert len(requests) == 1 and result['comments_status'] == 'empty'
    assert result['comments'] == [] and result['truncation_reasons'] == []


def test_same_reply_id_under_different_parents_is_retained(monkeypatch):
    result, _ = paginate([{'has_more': False, 'comments': [
        comment('a', sub_comments=[comment('reply')], sub_comment_count=1),
        comment('b', sub_comments=[comment('reply')], sub_comment_count=1),
    ]}], monkeypatch=monkeypatch)
    assert [(c['key'], c['parent_key']) for c in result['comments']] == [
        ('a', None), ('reply', 'a'), ('b', None), ('reply', 'b')]


def test_reply_without_verified_parent_is_not_saved(monkeypatch):
    result, _ = paginate([{'has_more': False, 'comments': [
        comment('', sub_comments=[comment('orphan')], sub_comment_count=1),
    ]}], monkeypatch=monkeypatch)
    assert result['comments'] == [] and result['comments_status'] == 'partial'


def test_retry_reloads_current_body_images_and_metadata_after_cli(tmp_path):
    db = database()
    def call(command, *args, timeout):
        if command == 'status':
            return {'authenticated': True, 'guest': False}
        assert command == 'comments'
        saved = db.tables['intelligence_items'][0]['meta_jsonb']
        saved['knowledge_link'] = {'case_id': 'updated-case'}
        saved['browser_capture']['body_text'] = 'Current author body'
        saved['browser_capture']['images'][0]['id'] = 'updated-image'
        return {'has_more': False, 'comments': [comment('new')]}
    result = retry(db, tmp_path, call)
    assert result['capture']['body_text'] == 'Current author body'
    assert result['capture']['images'][0]['id'] == 'updated-image'
    assert db.tables['intelligence_items'][0]['meta_jsonb']['knowledge_link'] == {'case_id': 'updated-case'}


def test_retry_keeps_non_comment_truncation_reasons(tmp_path):
    db = database()
    db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']['truncation_reasons'] = ['Image download incomplete']
    call, _ = authenticated([{'has_more': False, 'comments': [comment('new')]}])
    result = retry(db, tmp_path, call)
    assert 'Image download incomplete' in result['capture']['truncation_reasons']
    assert result['comments_status'] == 'complete'


def test_retry_new_comments_survive_full_saved_capacity(tmp_path):
    db = database()
    db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']['comments'] = [
        {'key': str(i), 'text': 'Saved comment'} for i in range(100)]
    call, _ = authenticated([{'has_more': False, 'comments': [comment('new')]}])
    result = retry(db, tmp_path, call)
    assert len(result['capture']['comments']) == 100
    assert result['capture']['comments'][0]['key'] == 'new'
    assert result['comments_status'] == 'partial' and result['warnings']


def test_retry_authentication_consumes_same_overall_deadline(tmp_path, monkeypatch):
    clock = [0]
    monkeypatch.setattr(module().time, 'monotonic', lambda: clock[0])
    def call(command, *args, timeout):
        if command == 'status':
            assert timeout == 20
            clock[0] = 199.75
            return {'authenticated': True, 'guest': False}
        assert command == 'comments' and timeout == .25
        clock[0] = 200
        return {'has_more': True, 'cursor': 'next', 'comments': [comment('new')]}
    result = retry(database(), tmp_path, call)
    assert result['comments_status'] == 'partial' and result['capture']['comments'][0]['key'] == 'new'


def test_retry_rechecks_deleted_item_after_cli_before_writing_bundle(tmp_path):
    db = database()
    def call(command, *args, timeout):
        if command == 'status':
            return {'authenticated': True, 'guest': False}
        db.tables['intelligence_items'][0]['deleted_at'] = 'deleted'
        return {'has_more': False, 'comments': [comment('new')]}
    with pytest.raises(HTTPException) as error:
        retry(db, tmp_path, call)
    assert error.value.status_code == 404
    assert not list(tmp_path.rglob('capture.json'))


def test_retry_reconciles_lost_database_write_receipt(tmp_path):
    db = database()
    table = db.table
    def lost_receipt(name):
        query = table(name)
        execute = query.execute
        def run():
            result = execute()
            if name == 'intelligence_items' and query.action == 'update':
                raise TimeoutError('receipt lost after commit')
            return result
        query.execute = run
        return query
    db.table = lost_receipt
    call, _ = authenticated([{'has_more': False, 'comments': [comment('new')]}])
    result = retry(db, tmp_path, call)
    assert result['status'] == 'ok' and result['database_status'] == 'saved'
    assert result['capture']['comments'][0]['key'] == 'new'


@pytest.mark.parametrize('note_id', ['1', URL, 'invalid', None])
def test_pagination_rejects_short_indices_and_non_note_ids_before_cli(note_id):
    with pytest.raises(module().CliFailure) as error:
        module().collect_comments(capture(), note_id,
            call=lambda *_a, **_k: pytest.fail('Explicit note ID required'))
    assert error.value.code == 'comment_source_mismatch'
