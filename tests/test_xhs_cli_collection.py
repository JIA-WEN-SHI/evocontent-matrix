import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import Actor
from fake_store import MemoryStore

ACCOUNT = '11111111-1111-4111-8111-111111111111'
NOTE = '6a6191a4000000000103392a'
URL = 'https://www.xiaohongshu.com/explore/' + NOTE


def module():
    assert importlib.util.find_spec('app.services.xhs_cli_collection'), 'CLI collection adapter is missing'
    return importlib.import_module('app.services.xhs_cli_collection')


def database():
    db = MemoryStore()
    db.tables['domains'][0]['slug'] = 'ai_content'
    db.tables['channel_accounts'] = [{'id': ACCOUNT, 'channel': 'xiaohongshu', 'is_active': True,
        'config_jsonb': {'onboarding': {'domain_slug': 'ai_content'}, 'collection_plan': {
            'mode': 'xhs_cli', 'steps': [{'tool': 'xhs_cli_search', 'query': 'AI 工作流', 'limit': 3}]}}}]
    return db


def cli(command, *args, **kwargs):
    if command == 'status':
        return {'authenticated': True, 'guest': False}
    if command == 'search':
        return {'items': [{'id': NOTE, 'xsec_token': 'NEVER_SAVE_THIS', 'note_card': {'title': '工作流'}}]}
    if command == 'read':
        assert args[0] == NOTE  # Never use the globally mutable short-index cache.
        return {'items': [{'id': NOTE, 'note_card': {'note_id': NOTE, 'title': 'AI 工作流',
            'desc': '这是作者的完整正文。' * 600, 'type': 'normal', 'xsec_token': 'NEVER_SAVE_THIS',
            'image_list': [{'url_default': 'http://sns-webpic-qc.xhscdn.com/public.webp'}]}}]}
    raise module().CliFailure('api_error')


def collect(db, tmp_path, **changes):
    return module().collect_account(db, account_id=ACCOUNT, domain_slug='ai_content',
        source_kind='hotspot', actor=Actor(user_id='qa', role='admin'), media_root=tmp_path,
        call=changes.pop('call', cli), download=changes.pop('download', lambda *_a, **_k: (b'RIFF0000WEBPdata', 'image/webp')),
        **changes)


def test_cli_plan_is_preserved_and_account_scoped():
    from app.services.account_service import _normalize_collection_plan
    plan = _normalize_collection_plan({'mode': 'xhs_cli', 'steps': [{'tool': 'xhs_cli_search', 'query': 'AI 工作流'}]})
    assert plan['mode'] == 'xhs_cli'
    assert plan['steps'][0]['scope'] == 'account'


def test_cli_saves_full_body_locally_and_in_database_despite_comment_failure(tmp_path):
    db = database()
    result = collect(db, tmp_path)
    row = db.tables['intelligence_items'][0]
    capture = row['meta_jsonb']['browser_capture']
    assert result['status'] == 'partial' and result['inserted'] == 1
    assert row['source_type'] == 'hotspot_xiaohongshu_cli'
    assert row['raw_text'].endswith('这是作者的完整正文。' * 600)
    assert capture['comments_status'] == 'failed'
    assert capture['collection_method'] == 'xiaohongshu_cli'
    folder = tmp_path / ACCOUNT / row['id']
    assert (folder / 'note.md').read_text(encoding='utf-8').startswith('# AI 工作流')
    saved = json.loads((folder / 'capture.json').read_text(encoding='utf-8'))
    assert saved['body_text'] == capture['body_text']
    assert len(list(folder.glob('*.webp'))) == 1
    assert saved['images'][0]['status'] == 'saved'
    assert 'NEVER_SAVE_THIS' not in json.dumps(row) + (folder / 'capture.json').read_text(encoding='utf-8')


def test_repeated_cli_collection_updates_one_record_and_preserves_images(tmp_path):
    db = database()
    collect(db, tmp_path)
    result = collect(db, tmp_path)
    assert len(db.tables['intelligence_items']) == 1
    assert result['inserted'] == 0 and result['updated'] == 1
    assert len(list(tmp_path.rglob('*.webp'))) == 1


def test_image_failure_keeps_body_and_records_failure(tmp_path):
    def bad_download(*args, **kwargs):
        raise ValueError('private address')
    db = database()
    result = collect(db, tmp_path, download=bad_download)
    assert result['status'] == 'partial'
    row = db.tables['intelligence_items'][0]
    assert row['meta_jsonb']['browser_capture']['images'][0]['status'] == 'failed'
    assert (tmp_path / ACCOUNT / row['id'] / 'note.md').is_file()


def test_local_body_survives_database_write_failure(tmp_path):
    db = database()
    original = db.table
    def table(name):
        query = original(name)
        execute = query.execute
        def run():
            if name == 'intelligence_items' and query.action == 'insert':
                raise TimeoutError('database unavailable')
            return execute()
        query.execute = run
        return query
    db.table = table
    result = collect(db, tmp_path)
    assert result['status'] == 'partial' and result['inserted'] == 0
    assert len(list(tmp_path.rglob('note.md'))) == 1
    saved = json.loads(next(tmp_path.rglob('capture.json')).read_text(encoding='utf-8'))
    assert saved['database_status'] == 'unconfirmed'


@pytest.mark.parametrize('change,status', [('inactive',409),('wrong_domain',409),('wrong_channel',409)])
def test_wrong_account_scope_rejected_before_cli_or_disk(tmp_path, change, status):
    db = database()
    account = db.tables['channel_accounts'][0]
    if change == 'inactive': account['is_active'] = False
    if change == 'wrong_channel': account['channel'] = 'douyin'
    if change == 'wrong_domain': account['config_jsonb']['onboarding']['domain_slug'] = 'other'
    with pytest.raises(HTTPException) as error:
        collect(db, tmp_path, call=lambda *_a, **_k: pytest.fail('CLI must not run'))
    assert error.value.status_code == status
    assert not list(tmp_path.iterdir())


def test_mismatched_detail_is_not_saved(tmp_path):
    def mismatch(command, *args, **kwargs):
        result = cli(command, *args, **kwargs)
        if command == 'read': result['items'][0]['id'] = '6ac3b3a200000000140395fd'
        return result
    result = collect(database(), tmp_path, call=mismatch)
    assert result['status'] == 'partial' and result['collected'] == 0
    assert not list(tmp_path.rglob('note.md'))


def test_bounded_comment_page_does_not_claim_all_replies(tmp_path):
    def with_comments(command, *args, **kwargs):
        if command == 'comments':
            return {'has_more': False, 'comments': [{'id':'c1','note_id':NOTE,'content':'用户问题',
                'sub_comment_count':2,'sub_comments':[{'id':'r1','content':'回复内容'}]}]}
        return cli(command, *args, **kwargs)
    db = database()
    result = collect(db, tmp_path, call=with_comments)
    capture = db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']
    assert capture['comments_status'] == 'partial'
    assert capture['comments'][1]['parent_key'] == 'c1'
    assert '用户问题' not in capture['body_text']
    assert result['status'] == 'partial'


def test_missing_previously_saved_image_is_not_counted_as_saved(tmp_path):
    db = database()
    collect(db,tmp_path)
    next(tmp_path.rglob('*.webp')).unlink()
    def bad(*args,**kwargs): raise ValueError('download failed')
    result = collect(db,tmp_path,download=bad)
    image = db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']['images'][0]
    assert result['images_saved'] == 0 and image['status'] == 'failed'


def test_saved_comments_are_retained_but_latest_failure_remains_visible():
    from app.services.browser_capture import merge_public_capture
    original = {'schema_version':1,'source_url':URL,'title':'AI 工作流','body_text':'作者正文',
        'captured_at':'2026-01-01T00:00:00+00:00','comments_status':'partial','comments':[{'key':'1','text':'用户问题'}]}
    merged = merge_public_capture(original,{**original,'comments':[],'comments_status':'failed'})
    assert len(merged['comments']) == 1 and merged['comments_status'] == 'failed'


def test_new_comments_survive_full_historical_comment_capacity():
    from app.services.browser_capture import merge_public_capture
    original = {'schema_version':1,'source_url':URL,'title':'AI 工作流','body_text':'作者正文',
        'captured_at':'2026-01-01T00:00:00+00:00','comments_status':'partial',
        'comments':[{'key':str(i),'text':'历史评论'} for i in range(100)]}
    merged = merge_public_capture(original,{**original,'comments':[{'key':'new','text':'最新问题'}]})
    assert len(merged['comments']) == 100
    assert any(c['key']=='new' for c in merged['comments'])


def test_full_body_write_requires_matching_text_readback():
    from app.services.browser_capture import update_capture_metadata
    db = database()
    db.tables['intelligence_items'] = [{'id':'i','account_id':ACCOUNT,'raw_text':'short',
        'meta_jsonb':{'browser_capture':{'revision':'same'}}}]
    original = db.table
    def table(name):
        query = original(name)
        execute = query.execute
        def run():
            if query.action=='update': raise TimeoutError('write failed before commit')
            return execute()
        query.execute = run
        return query
    db.table = table
    with pytest.raises(TimeoutError):
        update_capture_metadata(db,account_id=ACCOUNT,item_id='i',meta={'browser_capture':{'revision':'same'}},revision='same',expected_revision='same',raw_text='full author body')


def test_cli_runner_uses_json_utf8_timeout_and_sanitizes_errors(monkeypatch):
    mod = module()
    monkeypatch.setattr(mod.shutil, 'which', lambda _name: 'C:/tools/xhs.exe')
    def run(argv, **kwargs):
        assert argv == ['C:/tools/xhs.exe','search','AI 工作流','--json']
        assert kwargs['env']['PYTHONIOENCODING'] == 'utf-8'
        assert kwargs['timeout'] <= 55 and not kwargs.get('shell')
        return SimpleNamespace(returncode=1,stdout=json.dumps({'ok':False,'error':{'code':'api_error','message':'secret=xsec_token'}}))
    monkeypatch.setattr(mod.subprocess, 'run', run)
    with pytest.raises(mod.CliFailure) as error: mod.call_cli('search','AI 工作流')
    assert error.value.code == 'api_error'
    assert 'secret' not in str(error.value)


def test_cli_request_dispatches_even_when_old_extension_is_connected(monkeypatch):
    import asyncio
    from app.routers import ops
    from app.models import AccountCollectionRunRequest
    from starlette.requests import Request
    mod = module()
    db = database()
    monkeypatch.setattr(mod, 'collect_account', lambda *args, **kwargs: {'status':'ok','provider':'xiaohongshu_cli'})
    request = Request({'type':'http','method':'POST','path':'/','headers':[(b'origin',b'http://127.0.0.1:3001')],'client':('127.0.0.1',1234)})
    result = asyncio.run(ops.run_account_collection_ops(request,ACCOUNT,AccountCollectionRunRequest(domain_slug='ai_content'),
        SimpleNamespace(agent_service_url='http://must-not-run'),Actor(user_id='qa',role='admin'),db))
    assert result['provider'] == 'xiaohongshu_cli'
