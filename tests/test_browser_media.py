from importlib import import_module
from pathlib import Path
from uuid import uuid4
import socket

import pytest


def media():
    return import_module('app.services.browser_media')


def test_media_url_rejects_private_dns_unverified_host_credentials():
    module=media()
    public=lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
    assert module.validate_image_url('https://sns-webpic-qc.xhscdn.com/p.webp',resolver=public)[1]=='93.184.216.34'
    for url in ['http://sns-webpic-qc.xhscdn.com/p','https://evil.test/p','https://user:pw@sns-webpic-qc.xhscdn.com/p','https://sns-webpic-qc.xhscdn.com:8000/p']:
        with pytest.raises(ValueError): module.validate_image_url(url,resolver=public)
    private=lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
    with pytest.raises(ValueError): module.validate_image_url('https://sns-webpic-qc.xhscdn.com/p',resolver=private)


def test_signature_mime_and_size_limits():
    module=media()
    assert module.image_extension(b'\x89PNG\r\n\x1a\n' + b'x'*20,'image/png')=='png'
    for data,mime in [(b'<html>','image/png'),(b'\xff\xd8\xff','text/html'),(b'GIF89a','image/png')]:
        with pytest.raises(ValueError): module.image_extension(data,mime)
    assert module.MAX_IMAGE_BYTES==10*1024*1024
    assert module.MAX_JOB_BYTES==100*1024*1024


def test_generated_media_path_stays_under_root(tmp_path):
    module=media(); account=str(uuid4()); item=str(uuid4())
    path=module.media_path(tmp_path,account,item,'a'*64,'png')
    assert path.is_relative_to(tmp_path.resolve())
    for bad in ['../outside','/etc','not-a-uuid']:
        with pytest.raises(ValueError): module.media_path(tmp_path,bad,item,'a'*64,'png')
    with pytest.raises(ValueError): module.media_path(tmp_path,account,item,'../outside','png')


def test_image_write_and_stop_before_write(tmp_path):
    module=media()
    candidate={'index':0,'key':'image-0','url':'https://sns-webpic-qc.xhscdn.com/public.png'}
    job={'account_id':str(uuid4()),'media_bytes':0}; item=str(uuid4())
    download=lambda url,remaining,**kwargs:(b'\x89PNG\r\n\x1a\n'+b'x'*20,'image/png')
    saved=module.store_image(candidate,job=job,item_id=item,media_root=tmp_path,should_continue=lambda:True,download=download)
    assert saved['status']=='saved' and saved['index']==0
    assert module.media_path(tmp_path,job['account_id'],item,saved['sha256'],saved['extension']).read_bytes().startswith(b'\x89PNG')
    with pytest.raises(RuntimeError): module.store_image(candidate,job=job,item_id=item,media_root=tmp_path,should_continue=lambda:False,download=download)


def test_pinned_https_connects_to_checked_ip_with_original_tls_name(monkeypatch):
    module=media(); seen=[]
    monkeypatch.setattr(module.socket,'create_connection',lambda address,timeout:seen.append(address) or object())
    class TLS:
        def wrap_socket(self,sock,server_hostname):
            seen.append(server_hostname); return sock
    connection=module.PinnedHTTPSConnection('sns-webpic-qc.xhscdn.com','93.184.216.34',timeout=5,context=TLS())
    connection.connect()
    assert seen==[('93.184.216.34',443),'sns-webpic-qc.xhscdn.com']


def test_redirect_to_localhost_rejected_without_second_request():
    module=media(); calls=[]
    class Response:
        status=302
        def getheader(self,name): return 'https://127.0.0.1/private' if name=='Location' else None
    class Connection:
        def __init__(self,*args,**kwargs): calls.append(args)
        def request(self,*args,**kwargs): assert 'Cookie' not in kwargs['headers']
        def getresponse(self): return Response()
        def close(self): pass
    resolver=lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
    with pytest.raises(ValueError): module.download_image('https://sns-webpic-qc.xhscdn.com/public',100,resolver=resolver,connection_factory=Connection)
    assert len(calls)==1


def test_media_read_rejects_other_account_and_missing_files(tmp_path):
    from fake_store import MemoryStore
    from app.models import Actor
    from fastapi import HTTPException
    module=media(); client=MemoryStore(); account=str(uuid4()); item=str(uuid4())
    client.tables['intelligence_items']=[{'id':item,'account_id':account,'meta_jsonb':{'browser_capture':{'images':[{'id':'media','status':'saved','sha256':'a'*64,'extension':'png','content_type':'image/png'}]}}}]
    for target in [account,str(uuid4())]:
        with pytest.raises(HTTPException): module.read_account_media(client,account_id=target,item_id=item,media_id='media',actor=Actor(user_id='qa',role='admin'),media_root=tmp_path)


def download_fixture(response):
    class Connection:
        sock = None
        def __init__(self,*args,**kwargs): pass
        def request(self,*args,**kwargs): pass
        def getresponse(self): return response
        def close(self): pass
    resolver=lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
    return {'resolver':resolver,'connection_factory':Connection}


def test_download_rejects_deadline_exceeded_during_eof():
    module=media();now=[0]
    class Response:
        status=200
        count=0
        def getheader(self,name): return 'image/png' if name=='Content-Type' else None
        def read(self,size):
            self.count+=1
            if self.count==1: return b'\x89PNG\r\n\x1a\n'
            now[0]=20
            return b''
    with pytest.raises(TimeoutError):
        module.download_image('https://sns-webpic-qc.xhscdn.com/p',100,clock=lambda:now[0],**download_fixture(Response()))


def test_failed_oversized_transfers_charge_the_job_budget(tmp_path):
    module=media();job={'account_id':str(uuid4()),'media_bytes':0};item=str(uuid4())
    class Response:
        status=200
        def getheader(self,name): return 'image/png' if name=='Content-Type' else None
        def read(self,size): return b'x'*size
    def download(url,remaining,**kwargs):
        return module.download_image(url,remaining,**kwargs,**download_fixture(Response()))
    for _ in range(11):
        with pytest.raises(ValueError):
            module.store_image({'url':'https://sns-webpic-qc.xhscdn.com/p','index':0},job=job,item_id=item,media_root=tmp_path,should_continue=lambda:True,download=download)
    assert module.MAX_JOB_BYTES <= job['media_bytes'] <= module.MAX_JOB_BYTES+1
