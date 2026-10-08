from importlib import import_module
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

SOURCE = "https://www.xiaohongshu.com/explore/6a6191a4000000000103392a"


def payload(**changes):
    return {"schema_version": 1, "source_url": SOURCE, "title": "AI 方法", "body_text": "公开原文不是模型摘要" * 2,
            "captured_at": datetime.now(timezone.utc).isoformat(), **changes}


def test_background_mode_bound_to_pairing_session_job():
    bridge = import_module("app.services.browser_bridge").BrowserBridgeStore(clock=lambda: 100)
    code = bridge.create_pairing("a", "ai_content", "qa", mode="background_text")["code"]
    assert bridge.pairing_info(code)["mode"] == "background_text"
    with pytest.raises(HTTPException):
        bridge.pair(code, "chrome-extension://" + "a" * 32, 7, protocol_version=1)
    session = bridge.pair(code, "chrome-extension://" + "a" * 32, 7, protocol_version=2)
    assert session["mode"] == "background_text"
    job = bridge.start_job("a", "ai_content", ["AI"], "hotspot")
    assert job["deadline"] == 700
    assert bridge.job_view(job["id"], "a")["mode"] == "background_text"
    assert bridge.enqueue(job["id"], "observe", {})["mode"] == "background_text"


def test_public_capture_bounds():
    cls = import_module("app.services.browser_capture").PublicCapture
    comment = {"key": "1", "text": "公开评论"}
    assert len(cls(**payload(body_text="文" * 50000, comments=[comment] * 100)).body_text) == 50000
    for change in [{"body_text": "文" * 50001}, {"comments": [comment] * 101}, {"comments": [{**comment, "text": "文" * 2001}]}, {"image_candidates": [{"key": "1", "index": 0, "url": "https://example.com/a.jpg"}] * 25}]:
        with pytest.raises(ValidationError):
            cls(**payload(**change))


def test_capture_merge_comments_does_not_append_duplicate_or_mix_into_body():
    merge = import_module('app.services.browser_capture').merge_public_capture
    first = payload(comments=[{"key":"c","text":"相同评论","parent_key":None}])
    merged = merge(first,payload(comments=[{"key":"c","text":"相同评论","parent_key":None},{"key":"r","text":"相同评论","parent_key":"c"}]))
    assert len(merged['comments']) == 2
    assert '相同评论' not in merged['body_text']
    with pytest.raises(ValueError):
        merge(first,payload(source_url=SOURCE.replace('6a6191a4','6a089a9d')))


def capture_db():
    from fake_store import MemoryStore
    client = MemoryStore()
    client.tables['domains'][0]['slug'] = 'ai_content'
    client.tables['channel_accounts'] = [{"id":"a","channel":"xiaohongshu","is_active":True,"config_jsonb":{"onboarding":{"domain_slug":"ai_content"}}}]
    return client


def test_capture_metadata_keeps_new_knowledge_links():
    module = import_module('app.services.browser_capture')
    db = capture_db()
    db.tables['intelligence_items'] = [{'id': 'i', 'account_id': 'a', 'meta_jsonb': {
        'browser_capture': {'revision': 'old'}, 'knowledge_links': {'linked_at': 'new', 'case_ids': ['saved']}}}]
    result = module.update_capture_metadata(db, account_id='a', item_id='i',
        meta={'browser_capture': {'revision': 'new'}, 'knowledge_links': {'linked_at': 'stale', 'case_ids': []}}, revision='new', expected_revision='old')
    assert result['meta_jsonb']['knowledge_links']['case_ids'] == ['saved']


def test_capture_metadata_cas_rejects_intervening_link():
    module = import_module('app.services.browser_capture')
    db = capture_db()
    db.tables['intelligence_items'] = [{'id': 'i', 'account_id': 'a', 'meta_jsonb': {'browser_capture': {'revision': 'old'}}}]
    original = db.table
    def table(name):
        query = original(name)
        execute = query.execute
        def concurrent():
            if name == 'intelligence_items' and query.action == 'update':
                db.tables[name][0]['meta_jsonb']['knowledge_links'] = {'linked_at': 'new', 'case_ids': ['saved']}
            return execute()
        query.execute = concurrent
        return query
    db.table = table
    with pytest.raises(HTTPException):
        module.update_capture_metadata(db, account_id='a', item_id='i', meta={'browser_capture': {'revision': 'new'}}, revision='new', expected_revision='old')
    assert db.tables['intelligence_items'][0]['meta_jsonb']['knowledge_links']['case_ids'] == ['saved']


def test_capture_metadata_rejects_stale_caller_before_initial_read():
    module = import_module('app.services.browser_capture')
    db = capture_db()
    db.tables['intelligence_items'] = [{'id': 'i', 'account_id': 'a', 'meta_jsonb': {
        'browser_capture': {'revision': 'newer', 'comments': [{'text': 'new reply'}]}}}]
    with pytest.raises(HTTPException) as error:
        module.update_capture_metadata(db, account_id='a', item_id='i',
            meta={'browser_capture': {'revision': 'stale-write', 'comments': []}},
            revision='stale-write', expected_revision='original')
    assert error.value.status_code == 409
    assert db.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']['comments'] == [{'text': 'new reply'}]


def test_duplicate_legacy_record_enriched_without_metadata_loss(monkeypatch):
    from app.models import Actor
    module = import_module('app.services.browser_capture')
    client = capture_db()
    client.tables['intelligence_items'] = [{"id":"i","account_id":"a","domain_id":client.tables['domains'][0]['id'],"source_url":SOURCE,"source_type":"hotspot_chrome_browser_ui","raw_text":"旧摘要","meta_jsonb":{"unrelated":"preserved","collection_method":"manual_browser_copy"}}]
    monkeypatch.setattr(module,'write_audit_log',lambda *args,**kwargs:None)
    job = {"account_id":"a","domain_slug":"ai_content","source_kind":"hotspot","queries":["AI"],"mode":"background_text"}
    receipt = module.persist_public_capture(client,job=job,capture=payload(),summary=None,model_name='test',actor=Actor(user_id='qa',role='admin'))
    assert receipt['saved'] and receipt['updated'] == 1
    row = client.tables['intelligence_items'][0]
    assert row['raw_text'] == '旧摘要'
    assert row['meta_jsonb']['unrelated'] == 'preserved'
    assert row['meta_jsonb']['collection_method'] == 'manual_browser_copy'
    assert row['meta_jsonb']['browser_capture']['body_text'] == payload()['body_text']
    assert row['meta_jsonb']['browser_capture']['summary_status'] == 'unavailable'


def test_uncertain_capture_update_is_read_back_without_write_replay(monkeypatch):
    from app.models import Actor
    module = import_module('app.services.browser_capture')
    client = capture_db()
    client.tables['intelligence_items'] = [{"id":"i","account_id":"a","domain_id":client.tables['domains'][0]['id'],"source_url":SOURCE,"source_type":"hotspot_chrome_browser_ui","raw_text":"旧摘要","meta_jsonb":{}}]
    monkeypatch.setattr(module,'write_audit_log',lambda *args,**kwargs:None)
    original = client.table
    writes = []
    def table(name):
        q = original(name); run=q.execute
        def execute():
            result=run()
            if name=='intelligence_items' and q.action=='update':
                writes.append(q.payload); raise TimeoutError('lost committed receipt')
            return result
        q.execute=execute; return q
    client.table=table
    receipt=module.persist_public_capture(client,job={"account_id":"a","domain_slug":"ai_content","source_kind":"hotspot","queries":["AI"],"mode":"background_text"},capture=payload(),summary=None,model_name='',actor=Actor(user_id='qa',role='admin'))
    assert receipt['saved']
    assert len(writes)==1


def test_new_capture_is_in_initial_insert_not_a_second_write(monkeypatch):
    from app.models import Actor
    module = import_module('app.services.browser_capture')
    client = capture_db()
    monkeypatch.setattr(module,'write_audit_log',lambda *args,**kwargs:None)
    original = client.table
    def table(name):
        query = original(name)
        run = query.execute
        def execute():
            if name == 'intelligence_items' and query.action == 'update':
                raise TimeoutError('second write unavailable')
            return run()
        query.execute = execute
        return query
    client.table = table
    receipt = module.persist_public_capture(client,job={"account_id":"a","domain_slug":"ai_content","source_kind":"hotspot","queries":["AI"],"mode":"background_text"},capture=payload(),summary=None,model_name='',actor=Actor(user_id='qa',role='admin'))
    assert receipt['saved'] and receipt['inserted'] == 1
    assert client.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']['body_text'] == payload()['body_text']


def test_partial_recollection_keeps_previously_confirmed_evidence(monkeypatch):
    from app.models import Actor
    module = import_module('app.services.browser_capture')
    client = capture_db()
    previous = payload(comments=[{'key':'c','text':'先前已确认评论'}],image_candidates=[{'key':'image-0','index':0,'url':'https://sns-webpic-qc.xhscdn.com/a.png'}],body_status='complete')
    previous['images'] = [{'id':'media','index':0,'status':'saved','sha256':'a'*64,'extension':'png'}]
    previous['summary'] = '先前已确认的模型摘要内容'
    client.tables['intelligence_items'] = [{'id':'i','account_id':'a','domain_id':client.tables['domains'][0]['id'],'source_url':SOURCE,'source_type':'hotspot_chrome_browser_ui','meta_jsonb':{'browser_capture':previous}}]
    monkeypatch.setattr(module,'write_audit_log',lambda *args,**kwargs:None)
    module.persist_public_capture(client,job={'account_id':'a','domain_slug':'ai_content','source_kind':'hotspot','queries':['AI'],'mode':'background_text'},capture=payload(body_text='新读取不完整',comments_status='partial',images_status='partial'),summary=None,model_name='',actor=Actor(user_id='qa',role='admin'))
    capture = client.tables['intelligence_items'][0]['meta_jsonb']['browser_capture']
    assert capture['comments'][0]['key'] == 'c'
    assert capture['images'][0]['id'] == 'media'
    assert capture['image_candidates'][0]['index'] == 0
    assert capture['body_text'] == previous['body_text']
    assert capture['summary'] == previous['summary']
    assert capture['previous_captured_at'] == previous['captured_at']
