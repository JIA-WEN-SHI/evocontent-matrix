from copy import deepcopy
from hashlib import sha256
import http.client
import ipaddress
from pathlib import Path
import re
import socket
import ssl
import time
from threading import RLock, Timer
from urllib.parse import urljoin, urlsplit
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from fastapi import HTTPException

from app.services.browser_capture import update_capture_metadata

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_JOB_BYTES = 100 * 1024 * 1024
ALLOWED_HOSTS = frozenset({"sns-webpic-qc.xhscdn.com"})
MEDIA_ROOT = Path(__file__).resolve().parents[4] / "data" / "browser-media"


def validate_image_url(url, *, resolver=socket.getaddrinfo):
    parts = urlsplit(url)
    if len(url)>2000 or any(ord(c)<32 for c in url) or parts.scheme!="https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password or parts.port not in {None,443}:
        raise ValueError("配图地址不在已核对的公开域名内")
    addresses = resolver(parts.hostname,443,type=socket.SOCK_STREAM)
    ips = [entry[4][0] for entry in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in ips):
        raise ValueError("配图域名解析到禁止访问的地址")
    return parts,ips[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self,host,checked_ip,**kwargs):
        super().__init__(host,**kwargs)
        self.checked_ip = checked_ip

    def connect(self):
        # Never resolve the hostname again after checking DNS; TLS still verifies it.
        sock = socket.create_connection((self.checked_ip,443),self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock,server_hostname=self.host)
            self.transport_socket = self.sock
        except Exception:
            sock.close()
            raise


def download_image(url,remaining,*,resolver=socket.getaddrinfo,connection_factory=PinnedHTTPSConnection,clock=time.monotonic,on_bytes=None):
    started = clock()
    budget = min(MAX_IMAGE_BYTES,remaining)
    if budget <= 0:
        raise ValueError("本轮图片已达到 100 MiB 上限")
    for redirect in range(3):
        parts,ip = validate_image_url(url,resolver=resolver)
        left = 15-(clock()-started)
        if left <= 0:
            raise TimeoutError("图片下载超时")
        connection = connection_factory(parts.hostname,ip,timeout=left,context=ssl.create_default_context())
        def abort():
            transport = getattr(connection,"transport_socket",None) or getattr(connection,"sock",None)
            if transport:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        timer = Timer(left,abort)
        timer.daemon = True
        timer.start()
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?"+parts.query
            connection.request("GET",path,headers={"Accept":"image/*","User-Agent":"EvoContent/1.1","Referer":"https://www.xiaohongshu.com/"})
            response = connection.getresponse()
            if response.status in {301,302,303,307,308}:
                if redirect >= 2 or not response.getheader("Location"):
                    raise ValueError("配图重定向超过限制")
                url = urljoin(url,response.getheader("Location"))
                continue
            if response.status != 200:
                raise ValueError(f"公开配图下载被拒绝（HTTP {response.status}）")
            length = response.getheader("Content-Length")
            if length and int(length) > budget:
                raise ValueError("图片超过大小或本轮下载上限")
            data = bytearray()
            while True:
                left = 15-(clock()-started)
                if left <= 0:
                    raise TimeoutError("图片下载超时")
                transport = getattr(connection,"transport_socket",None) or getattr(connection,"sock",None)
                if transport:
                    transport.settimeout(left)
                size = min(65536,budget-len(data))
                if size <= 0:
                    if length and int(length) == len(data):
                        break
                    raise ValueError("图片达到大小或本轮下载上限，未确认完整")
                reader = getattr(response,"read1",response.read)
                chunk = reader(size)
                if chunk and on_bytes:
                    on_bytes(len(chunk))
                if clock()-started >= 15:
                    raise TimeoutError("图片下载超时")
                if not chunk:
                    break
                data.extend(chunk)
                if len(data)>budget:
                    raise ValueError("图片超过大小或本轮下载上限")
            return bytes(data),(response.getheader("Content-Type") or "").split(";")[0].strip().lower()
        finally:
            timer.cancel()
            connection.close()
    raise ValueError("配图下载未完成")


def image_extension(data,mime):
    signatures = {"image/jpeg":("jpg",data.startswith(b"\xff\xd8\xff")),"image/png":("png",data.startswith(b"\x89PNG\r\n\x1a\n")),"image/gif":("gif",data.startswith((b"GIF87a",b"GIF89a"))),"image/webp":("webp",data.startswith(b"RIFF") and data[8:12]==b"WEBP")}
    if mime not in signatures or not signatures[mime][1] or len(data)>MAX_IMAGE_BYTES:
        raise ValueError("图片内容类型或文件特征不符合要求")
    return signatures[mime][0]


def media_path(root,account_id,item_id,checksum,extension):
    account_id,item_id = str(UUID(str(account_id))),str(UUID(str(item_id)))
    if not re.fullmatch(r"[a-f0-9]{64}",checksum) or extension not in {"jpg","png","webp","gif"}:
        raise ValueError("媒体标识无效")
    root = Path(root).resolve()
    path = (root/account_id/item_id/(checksum+"."+extension)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("媒体路径超出项目目录")
    return path


def store_image(candidate,*,job,item_id,media_root,should_continue,download=download_image):
    with job.setdefault("_media_lock",RLock()):
        return _store_image(candidate,job=job,item_id=item_id,media_root=media_root,should_continue=should_continue,download=download)


def _store_image(candidate,*,job,item_id,media_root,should_continue,download):
    if not should_continue():
        raise RuntimeError("任务已停止")
    charged = 0
    def charge(size):
        nonlocal charged
        charged += size
        job["media_bytes"] = job.get("media_bytes",0)+size
    data,mime = download(candidate["url"],MAX_JOB_BYTES-job.get("media_bytes",0),on_bytes=charge)
    if charged < len(data):
        charge(len(data)-charged)
    if len(data)>MAX_IMAGE_BYTES or job["media_bytes"]>MAX_JOB_BYTES:
        raise ValueError("图片超过下载上限")
    extension = image_extension(data,mime)
    checksum = sha256(data).hexdigest()
    path = media_path(media_root,job["account_id"],item_id,checksum,extension)
    if not should_continue():
        raise RuntimeError("任务已停止")
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        temporary = path.with_name(str(uuid4())+".tmp")
        with temporary.open("xb") as output:
            output.write(data)
        if not should_continue():
            raise RuntimeError("任务已停止，未引用的临时文件不会计入成果")
        temporary.replace(path)
    return {"id":str(uuid5(NAMESPACE_URL,f"{job['account_id']}:{item_id}:{checksum}")),"index":candidate["index"],"status":"saved","sha256":checksum,"extension":extension,"content_type":mime,"bytes":len(data),"candidate_url":candidate["url"]}


def item_capture(client,account_id,item_id):
    rows = client.table("intelligence_items").select("*").eq("id",item_id).eq("account_id",account_id).limit(1).execute().data or []
    if not rows or not (rows[0].get("meta_jsonb") or {}).get("browser_capture"):
        raise HTTPException(404,"此账号资料尚无原文采集记录")
    return rows[0]


def save_public_image(client,*,job,item_id,index,should_continue,lock,media_root=MEDIA_ROOT,download=download_image):
    with job.setdefault("_media_lock",RLock()):
        return _save_public_image(client,job=job,item_id=item_id,index=index,should_continue=should_continue,lock=lock,media_root=media_root,download=download)


def _save_public_image(client,*,job,item_id,index,should_continue,lock,media_root,download):
    with lock:
        if not should_continue():
            raise HTTPException(410,"采集任务已结束")
        if item_id not in job.get("saved_item_ids",[]):
            raise HTTPException(403,"图片不属于本轮已确认资料")
        row = item_capture(client,job["account_id"],item_id)
        capture = row["meta_jsonb"]["browser_capture"]
        candidate = next((i for i in capture.get("image_candidates",[]) if i["index"]==index),None)
        if not candidate:
            raise HTTPException(404,"没有已观察到的配图")
        revision = capture["revision"]
        previous = next((i for i in capture.get("images",[]) if i["index"]==index),None)
        if previous and previous.get("status")=="saved" and previous.get("candidate_url") == candidate["url"]:
            path = media_path(media_root,job["account_id"],item_id,previous["sha256"],previous["extension"])
            if path.is_file():
                return previous
    try:
        result = store_image(candidate,job=job,item_id=item_id,media_root=media_root,should_continue=should_continue,download=download)
    except RuntimeError:
        raise HTTPException(410,"采集已停止，图片未追加")
    except (ValueError,TimeoutError,OSError,http.client.HTTPException):
        result = {"index":index,"status":"failed","message":"公开图片下载失败：域名、权限、文件类型或大小未通过校验"}
        if previous and previous.get("status") == "saved":
            result = {**previous,"last_attempt":result}
    with lock:
        if not should_continue():
            raise HTTPException(410,"采集已停止，图片未追加")
        row = item_capture(client,job["account_id"],item_id)
        meta = deepcopy(row["meta_jsonb"])
        current = meta["browser_capture"]
        if current["revision"] != revision:
            raise HTTPException(409,"资料版本已变化，图片未追加")
        current["images"] = [i for i in current.get("images",[]) if i["index"]!=index]+[result]
        current["images"].sort(key=lambda i:i["index"])
        current["revision"] = sha256((revision+repr(result)).encode()).hexdigest()
        update_capture_metadata(client,account_id=job["account_id"],item_id=item_id,meta=meta,revision=current["revision"],expected_revision=revision)
        return result


def read_account_media(client,*,account_id,item_id,media_id,actor,media_root=MEDIA_ROOT):
    if actor.role not in {"admin","operator"}:
        raise HTTPException(403,"此操作需要管理员或运营权限")
    row = item_capture(client,account_id,item_id)
    image = next((i for i in row["meta_jsonb"]["browser_capture"].get("images",[]) if i.get("id")==media_id and i.get("status")=="saved"),None)
    if not image:
        raise HTTPException(404,"图片未保存或不属于此账号")
    path = media_path(media_root,account_id,item_id,image["sha256"],image["extension"])
    if not path.is_file():
        raise HTTPException(404,"本机图片文件缺失，请重新采集")
    return {"path":path,"content_type":image["content_type"]}
