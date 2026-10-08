from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import get_supabase
from app.main import app
from app.routers import browser_bridge as routes
from app.services.browser_bridge import BrowserBridgeStore
from fake_store import MemoryStore


def test_pair_poll_read_save_and_stop_through_real_api_contract(monkeypatch):
    database = MemoryStore()
    database.tables["domains"] = [{"id": "domain-ai", "slug": "ai_content"}]
    database.tables["channel_accounts"] = [{"id": "account-ai", "channel": "xiaohongshu", "is_active": True,
        "config_jsonb": {"onboarding": {"domain_slug": "ai_content"}, "collection_plan": {"mode": "browser_ui"}}}]
    broker = BrowserBridgeStore()
    monkeypatch.setattr(routes, "bridge_store", broker)
    app.dependency_overrides[get_supabase] = lambda: database
    project = {"x-user-id": "qa", "x-user-role": "admin", "origin": "http://127.0.0.1:3001"}
    extension_origin = "chrome-extension://" + "b" * 32
    try:
        with TestClient(app, client=("127.0.0.1", 54321)) as http:
            response = http.post("/api/browser-bridge/accounts/account-ai/pairing", headers=project, json={"domain_slug": "ai_content"})
            assert response.status_code == 200, response.text
            code = response.json()["code"]
            headers = {"x-extension-origin": extension_origin, "x-pairing-code": code}
            preview = http.post("/api/browser-bridge/extension/pair-info", headers=headers, json={"code": code})
            assert preview.status_code == 200
            assert "key" not in preview.text.lower()
            pair = http.post("/api/browser-bridge/extension/pair", headers=headers, json={"code": code, "tab_id": 5, "consent": True})
            assert pair.status_code == 200, pair.text
            token = pair.json()["token"]
            ext = {"x-extension-origin": extension_origin, "authorization": "Bearer " + token}
            job = broker.start_job("account-ai", "ai_content", ["AI 办公"], "hotspot")
            worker = {"authorization": "Bearer " + job["worker_token"]}
            prefix = "/api/browser-bridge/worker/" + job["id"]
            forbidden = http.post(prefix + "/save", headers=worker, json={"summary": "模型没有读取页面，不允许编造并保存摘要"})
            assert forbidden.status_code == 409
            command = http.post(prefix + "/commands", headers=worker, json={"operation": "read_note", "arguments": {"version": "v1"}})
            assert command.status_code == 200, command.text
            polled = http.post("/api/browser-bridge/extension/poll", headers=ext)
            assert polled.json()["command"]["id"] == command.json()["id"]
            assert http.post("/api/browser-bridge/extension/poll", headers=ext).json()["command"] is None
            observation = {"ok": True, "data": {"kind": "note", "version": "v2", "note": {
                "source_url": "https://www.xiaohongshu.com/explore/6a089a9d0000000038021ad6?xsec_token=do-not-store",
                "title": "AI 办公流程", "text": "先整理工作任务，再核查输入，并保留人工检查环节。",
                "captured_at": datetime.now(timezone.utc).isoformat()}}}
            result = http.post("/api/browser-bridge/extension/results", headers=ext, json={"command_id": command.json()["id"], "result": observation})
            assert result.status_code == 200, result.text
            saved = http.post(prefix + "/save", headers=worker, json={"summary": "可见正文建议先整理任务、核查输入，最后保留人工检查。", "model_name": "fake-integration-model"})
            assert saved.status_code == 200 and saved.json()["inserted"] == 1, saved.text
            repeated = http.post(prefix + "/save", headers=worker, json={"summary": "不应重复保存已采集的同一篇笔记正文摘要"})
            assert repeated.json()["saved"] is False
            row = database.tables["intelligence_items"][0]
            assert row["account_id"] == "account-ai"
            assert row["meta_jsonb"]["collection_method"] == "project_browser_agent"
            assert row["meta_jsonb"]["metrics_verified"] is False
            assert "xsec_token" not in row["source_url"]
            stopped = http.post(f"/api/browser-bridge/accounts/account-ai/jobs/{job['id']}/stop", headers=project)
            assert stopped.json()["status"] == "cancelled"
            late = http.post(prefix + "/save", headers=worker, json={"summary": "停止后不允许再保存其他内容，或伪报成功"})
            assert late.status_code == 410
            assert len(database.tables["intelligence_items"]) == 1
    finally:
        app.dependency_overrides.clear()


def test_background_raw_capture_media_contract_and_stop(monkeypatch,tmp_path):
    from uuid import uuid4
    from app.services import browser_media
    account_id=str(uuid4()); domain_id=str(uuid4())
    database=MemoryStore()
    database.tables['domains']=[{'id':domain_id,'slug':'ai_content'}]
    database.tables['channel_accounts']=[{'id':account_id,'channel':'xiaohongshu','is_active':True,'config_jsonb':{'onboarding':{'domain_slug':'ai_content'},'collection_plan':{'mode':'browser_ui'}}}]
    broker=BrowserBridgeStore(); monkeypatch.setattr(routes,'bridge_store',broker)
    app.dependency_overrides[get_supabase]=lambda:database
    project={'x-user-id':'qa','x-user-role':'admin','origin':'http://127.0.0.1:3001'}
    origin='chrome-extension://'+'c'*32
    pairing=broker.create_pairing(account_id,'ai_content','qa',mode='background_text')
    session=broker.pair(pairing['code'],origin,5)
    job=broker.start_job(account_id,'ai_content',['AI'],'hotspot')
    worker={'authorization':'Bearer '+job['worker_token']}; ext={'x-extension-origin':origin,'authorization':'Bearer '+session['token']}
    prefix='/api/browser-bridge/worker/'+job['id']
    source='https://www.xiaohongshu.com/explore/6a6191a4000000000103392a'
    def store(client,**kwargs):
        kwargs['media_root']=tmp_path
        kwargs['download']=lambda url,remaining,**options:(b'\x89PNG\r\n\x1a\n'+b'x'*20,'image/png')
        return browser_media.save_public_image(client,**kwargs)
    monkeypatch.setattr(routes,'save_public_image',store,raising=False)
    try:
        with TestClient(app,client=('127.0.0.1',54321)) as http:
            command=broker.enqueue(job['id'],'read_note',{'version':'v'})
            broker.lease_command(session['token'],origin)
            note={'source_url':source,'title':'公开正文','text':'这是一篇已在公开页面读取的实际正文','captured_at':datetime.now(timezone.utc).isoformat()}
            capture={'schema_version':1,'source_url':source,'title':note['title'],'body_text':note['text'],'comments':[{'key':'c','text':'公开评论'}],'image_candidates':[{'key':'image-0','index':0,'url':'https://sns-webpic-qc.xhscdn.com/public.png'}],'captured_at':note['captured_at']}
            result=http.post('/api/browser-bridge/extension/results',headers=ext,json={'command_id':command['id'],'result':{'ok':True,'data':{'kind':'note','version':'v2','note':note,'capture':capture}}})
            assert result.status_code==200,result.text
            saved=http.post(prefix+'/save',headers=worker,json={'summary':None,'model_name':'test'})
            assert saved.status_code==200,saved.text
            item=saved.json()['item_id']
            summarized=http.post(prefix+'/summary/'+item,headers=worker,json={'summary':'正文已经保存，随后补充模型生成的摘要内容。','model_name':'test'})
            assert summarized.status_code==200,summarized.text
            picture=http.post(prefix+'/media/'+item+'/0',headers=worker)
            assert picture.status_code==200,picture.text
            assert picture.json()['status']=='saved'
            details=http.get(f'/api/browser-bridge/accounts/{account_id}/items/{item}/capture',headers=project)
            assert details.status_code==200,details.text
            assert details.json()['body_text']==note['text']
            assert details.json()['summary_status']=='available'
            assert 'image_candidates' not in details.json()
            paused=http.post('/api/browser-bridge/extension/pause',headers=ext,json={'message':'采集标签页已冻结，请恢复后继续'})
            assert paused.status_code==200,paused.text
            assert broker.job_view(job['id'],account_id)['status']=='awaiting_user'
            assert http.post(prefix+'/media/'+item+'/0',headers=worker).status_code==409
            broker.resume(job['id'],account_id)
            broker.stop(job['id'],account_id)
            assert http.post(prefix+'/media/'+item+'/0',headers=worker).status_code==410
            assert http.post(prefix+'/summary/'+item,headers=worker,json={'summary':'停止后不允许再追加摘要内容。'}).status_code==410
    finally:
        app.dependency_overrides.clear()
