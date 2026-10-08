"""Bounded local CLI collection, sharing the existing capture and media viewer."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.services.browser_capture import PublicCapture, merge_public_capture, persist_public_capture, update_capture_metadata
from app.services.browser_intel import intel_item_id
from app.services.browser_media import ALLOWED_HOSTS, MEDIA_ROOT, download_image, store_image


class CliFailure(RuntimeError):
    def __init__(self, code):
        self.code = code if re.fullmatch(r"[a-z_]{1,60}", str(code)) else "cli_failed"
        super().__init__(self.code)


def call_cli(command, *args, timeout=55):
    executable = os.environ.get("XHS_CLI_EXECUTABLE") or shutil.which("xhs")
    if not executable:
        raise CliFailure("cli_not_installed")
    try:
        result = subprocess.run([executable, command, *args, "--json"], capture_output=True,
            text=True, encoding="utf-8", errors="strict", timeout=min(55, timeout),
            env=dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="utf-8"),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        envelope = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise CliFailure("cli_timeout") from None
    except (OSError, ValueError):
        raise CliFailure("cli_invalid_response") from None
    if not isinstance(envelope, dict):
        raise CliFailure("cli_invalid_response")
    if result.returncode or not envelope.get("ok"):
        raise CliFailure((envelope.get("error") or {}).get("code", "cli_failed"))
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise CliFailure("cli_invalid_response")
    return data


def uses_cli(client, account_id):
    rows = client.table("channel_accounts").select("config_jsonb").eq("id", account_id).limit(1).execute().data or []
    return bool(rows and ((rows[0].get("config_jsonb") or {}).get("collection_plan") or {}).get("mode") == "xhs_cli")


@contextmanager
def collection_lock(root):
    # The CLI session and search context cache are shared across API processes.
    Path(root).mkdir(parents=True, exist_ok=True)
    with (Path(root) / ".collection.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise HTTPException(409, "已有一轮本地采集正在执行，请稍后重试") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def image_url(row):
    candidates = [row.get("url_default"), row.get("url_pre"), row.get("url")]
    candidates += [i.get("url") for i in row.get("info_list", []) if isinstance(i, dict)]
    for value in candidates:
        if not isinstance(value, str):
            continue
        try:
            parts = urlsplit(value)
            if (parts.hostname in ALLOWED_HOSTS and parts.scheme in {"http", "https"}
                    and not parts.username and not parts.password and parts.port in {None, 443}
                    and not parts.query and len(value) <= 2000):
                return urlunsplit(("https", parts.netloc, parts.path, "", ""))
        except ValueError:
            continue
    return None


def read_capture(detail, note_id):
    items = detail.get("items") or []
    item = items[0] if items else {}
    card = item.get("note_card") or detail.get("note_card") or {}
    ids = [v for v in (item.get("id"), card.get("note_id")) if v]
    if not ids or any(v != note_id for v in ids) or not isinstance(card.get("desc"), str):
        raise CliFailure("note_detail_mismatch")
    body = card["desc"]
    if not body.strip():
        raise CliFailure("note_body_missing")
    images = []
    for row in (card.get("image_list") or [])[:24]:
        if isinstance(row, dict) and (url := image_url(row)):
            images.append({"key":str(len(images)), "index":len(images), "url":url})
    reasons = []
    if len(body) > 50000:
        reasons.append("正文达到 50000 字上限")
    if len(images) != len(card.get("image_list") or []):
        reasons.append("部分配图地址未通过公开域名校验或达到 24 张上限")
    if card.get("type") == "video":
        reasons.append("视频仅保存文字说明与封面，不包含视频转录或视频文件")
    return PublicCapture(source_url="https://www.xiaohongshu.com/explore/"+note_id,
        title=str(card.get("title") or body.splitlines()[0])[:300], body_text=body[:50000],
        author_name=str((card.get("user") or {}).get("nickname") or "")[:200],
        tags=[str(t.get("name") or "")[:200] for t in (card.get("tag_list") or [])[:100] if isinstance(t, dict)],
        body_status="partial" if len(body)>50000 else "complete", comments_status="partial",
        image_candidates=images, images_status="partial" if reasons else "complete" if images else "empty",
        truncation_reasons=reasons, captured_at=datetime.now(timezone.utc)).model_dump(mode="json")


def add_comments(capture, page, note_id):
    comments = []
    seen = set()
    if not isinstance(page, dict) or (page.get("note_id") and page["note_id"] != note_id):
        raise CliFailure("comment_source_mismatch")
    partial = bool(page.get("has_more"))
    def add(row, parent=None):
        nonlocal partial
        if not isinstance(row, dict):
            raise CliFailure("comment_shape_unknown")
        if row.get("note_id") and row["note_id"] != note_id:
            raise CliFailure("comment_source_mismatch")
        text = row.get("content")
        key = row.get("id")
        if not isinstance(key, str) or not key or not isinstance(text, str) or not text.strip():
            partial = True
            return False
        identity = (key[:120], parent)
        if identity in seen:
            return True
        if len(comments) >= 100:
            partial = True
            return False
        seen.add(identity)
        partial |= len(text) > 2000
        user = row.get("user_info") or {}
        if not isinstance(user, dict):
            raise CliFailure("comment_shape_unknown")
        comments.append({"key":key[:120],"parent_key":parent,"text":text[:2000],
            "author_name":str(user.get("nickname") or "")[:200],
            "observed_time":str(row.get("create_time") or "")[:100],"truncated":len(text)>2000})
        return True
    if not isinstance(page.get("comments"), list):
        raise CliFailure("comment_shape_unknown")
    for row in page["comments"]:
        if not add(row):
            continue
        replies = row.get("sub_comments") or []
        if not isinstance(replies, list):
            raise CliFailure("comment_shape_unknown")
        partial |= bool(row.get("sub_comment_has_more")) or int(row.get("sub_comment_count") or 0)>len(replies)
        for reply in replies:
            add(reply, str(row.get("id") or "")[:120] or None)
    capture["comments"] = comments
    capture["comments_status"] = "partial" if partial else "complete" if comments else "empty"
    if partial:
        capture["truncation_reasons"] = (capture["truncation_reasons"] + ["评论或回复未完整返回，或达到 100 条/2000 字上限"])[-20:]


def collect_comments(capture, note_id, *, call=call_cli, deadline=None):
    """Read at most five explicit-ID pages, using the CLI's existing note context cache."""
    if not isinstance(note_id, str) or not re.fullmatch(r"[a-f0-9]{24}", note_id):
        raise CliFailure("comment_source_mismatch")
    deadline = time.monotonic() + 200 if deadline is None else deadline
    comments = {}
    reasons = []
    cursor = ""
    seen_cursors = {cursor}
    status = "partial"
    for page_number in range(5):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reasons.append("评论采集达到本轮时间上限，未确认全部评论")
            break
        args = (note_id, "--cursor", cursor) if cursor else (note_id,)
        try:
            page = call("comments", *args, timeout=min(20, remaining))
            if (not isinstance(page, dict) or type(page.get("has_more")) not in {bool, int}
                    or page["has_more"] not in (False, True)):
                raise CliFailure("comment_shape_unknown")
            # Continuation alone is not evidence that replies or text are incomplete.
            checked = {"truncation_reasons": []}
            add_comments(checked, {**page, "has_more": False}, note_id)
        except (CliFailure, ValueError, TypeError, KeyError, TimeoutError, OSError) as exc:
            code = exc.code if isinstance(exc, CliFailure) else "cli_timeout" if isinstance(exc, TimeoutError) else "comment_shape_unknown"
            reasons.append("评论读取失败：" + code + "；并非没有评论，请检查登录状态、读取上下文或平台限制")
            status = "failed"
            break
        for row in checked["comments"]:
            identity = (row["key"], row.get("parent_key"))
            if identity not in comments and len(comments) >= 100:
                reasons.append("评论与回复达到 100 条上限，未确认全部评论")
                break
            comments.setdefault(identity, row)
        reasons.extend(checked["truncation_reasons"])
        if time.monotonic() >= deadline:
            reasons.append("评论采集达到本轮时间上限，未确认全部评论")
            break
        if not page["has_more"]:
            status = "partial" if reasons else "complete" if comments else "empty"
            break
        if len(comments) >= 100:
            reasons.append("评论与回复达到 100 条上限，未确认全部评论")
            break
        next_cursor = page.get("cursor")
        if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 2000 or next_cursor in seen_cursors:
            reasons.append("评论游标重复、缺失或无效，已停止分页，未确认全部评论")
            break
        if page_number == 4:
            reasons.append("评论达到 5 页上限，未确认全部评论")
            break
        cursor = next_cursor
        seen_cursors.add(cursor)
    capture["comments"] = list(comments.values())
    capture["comments_status"] = status
    capture["truncation_reasons"] = list(dict.fromkeys(capture.get("truncation_reasons", []) + reasons))[-20:]
    return capture


def _comment_retry_item(client, account_id, item_id):
    accounts = client.table("channel_accounts").select("*").eq("id", account_id).limit(1).execute().data or []
    if not accounts or not accounts[0].get("is_active") or accounts[0].get("channel") != "xiaohongshu":
        raise HTTPException(409, "请选择启用中的小红书账号")
    config = accounts[0].get("config_jsonb") or {}
    if (config.get("collection_plan") or {}).get("mode") != "xhs_cli":
        raise HTTPException(409, "此账号未启用本地 CLI 采集")
    slug = (config.get("onboarding") or {}).get("domain_slug")
    domains = client.table("domains").select("id").eq("slug", slug).limit(1).execute().data or []
    rows = client.table("intelligence_items").select("*").eq("account_id", account_id).eq("id", item_id).limit(1).execute().data or []
    if not rows or rows[0].get("deleted_at"):
        raise HTTPException(404, "资料不存在或不属于此账号")
    row = rows[0]
    if not slug or not domains or row.get("domain_id") != domains[0]["id"]:
        raise HTTPException(409, "资料赛道与账号规划不一致")
    if row.get("source_type") not in {"hotspot_xiaohongshu_cli", "viewpoint_xiaohongshu_cli"}:
        raise HTTPException(409, "请选择本地 CLI 采集的公开笔记")
    saved = (row.get("meta_jsonb") or {}).get("browser_capture")
    try:
        if not isinstance(saved, dict) or saved.get("source_url") != row.get("source_url"):
            raise ValueError("capture source mismatch")
        checked = PublicCapture(**{key: value for key, value in saved.items() if key in PublicCapture.model_fields})
        if checked.source_url != row["source_url"]:
            raise ValueError("capture source must be canonical")
    except (ValueError, TypeError, KeyError):
        raise HTTPException(409, "资料缺少有效且一致的公开笔记来源") from None
    return row


def retry_capture_comments(client, *, account_id, item_id, actor, call=call_cli, media_root=MEDIA_ROOT) -> dict:
    if actor.role not in {"admin", "operator"}:
        raise HTTPException(403, "评论重试需要管理员或运营权限")
    try:
        account_id, item_id = str(UUID(account_id)), str(UUID(item_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "账号或资料 ID 无效") from None
    row = _comment_retry_item(client, account_id, item_id)
    note_id = urlsplit(row["source_url"]).path.rsplit("/", 1)[-1]
    deadline = time.monotonic() + 200
    with collection_lock(media_root):
        attempt = {"truncation_reasons": []}
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CliFailure("cli_timeout")
            auth = call("status", timeout=min(20, remaining))
            if not isinstance(auth, dict) or not auth.get("authenticated") or auth.get("guest"):
                raise CliFailure("not_authenticated")
        except (CliFailure, TimeoutError, OSError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, CliFailure) else "cli_timeout" if isinstance(exc, TimeoutError) else "cli_invalid_response"
            attempt.update(comments=[], comments_status="failed",
                truncation_reasons=["评论读取失败：" + code + "；并非没有评论，请检查 CLI 登录状态或平台限制"])
        else:
            collect_comments(attempt, note_id, call=call, deadline=deadline)
        # Re-read after CLI IO so current body, image indices and knowledge links survive.
        row = _comment_retry_item(client, account_id, item_id)
        if urlsplit(row["source_url"]).path.rsplit("/", 1)[-1] != note_id:
            raise HTTPException(409, "资料来源已变化，评论未保存")
        meta = deepcopy(row["meta_jsonb"])
        previous = meta["browser_capture"]
        incoming = {key: deepcopy(value) for key, value in previous.items() if key in PublicCapture.model_fields}
        incoming.update(attempt)
        incoming["truncation_reasons"] = ([reason for reason in previous.get("truncation_reasons", [])
            if not reason.startswith(("评论", "本轮仅获取一页评论"))] + attempt["truncation_reasons"])[-20:]
        merged = merge_public_capture(previous, incoming)
        saved = {**previous, **{key: merged[key] for key in ("comments", "comments_status", "truncation_reasons")},
            "comments_attempted_at": datetime.now(timezone.utc).isoformat()}
        saved["revision"] = sha256(json.dumps(saved, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        meta["browser_capture"] = saved
        warnings = list(attempt["truncation_reasons"])
        if saved["comments_status"] == "partial":
            warnings.append("评论仅获取了部分内容，未确认全部评论和回复")
        folder = None
        try:
            folder = save_bundle(media_root, account_id, item_id, saved, "unconfirmed")
        except OSError:
            warnings.append("本地评论资料包保存失败，请检查存储目录")
        database_status = "unconfirmed"
        try:
            update_capture_metadata(client, account_id=account_id, item_id=item_id, meta=meta, revision=saved["revision"], expected_revision=previous.get("revision"))
            database_status = "saved"
        except Exception:
            warnings.append("评论数据库保存未确认，请刷新核对后再重试")
        if database_status == "saved":
            try:
                folder = save_bundle(media_root, account_id, item_id, saved, database_status)
            except OSError:
                warnings.append("本地评论资料包更新失败，请检查存储目录")
        return {"status": "partial" if warnings or saved["comments_status"] not in {"complete", "empty"} else "ok",
            "provider": "xiaohongshu_cli", "account_id": account_id, "item_id": item_id,
            "capture_status": saved["comments_status"], "comments_status": saved["comments_status"],
            "capture": saved, "database_status": database_status, "local_saved": folder is not None,
            "warnings": list(dict.fromkeys(warnings))}


def save_bundle(root, account_id, item_id, capture, database_status):
    root = Path(root).resolve()
    folder = (root / str(UUID(account_id)) / str(UUID(item_id))).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("本地资料路径无效")
    folder.mkdir(parents=True, exist_ok=True)
    bundle = {**capture, "account_id":account_id, "item_id":item_id,
              "collection_method":"xiaohongshu_cli", "database_status":database_status}
    lines = ["# "+capture["title"], "", "来源："+capture["source_url"],
        "采集时间："+capture["captured_at"], "数据库状态："+database_status, "", "## 正文", "", capture["body_text"],
        "", "## 评论", "", "获取状态："+capture["comments_status"]]
    lines += [f"- {c.get('author_name', '')}：{c['text']}" for c in capture.get("comments", [])]
    lines += ["", "## 图片", ""]
    for image in capture.get("images", []):
        if image.get("status") == "saved":
            lines.append(f"![配图 {image['index']+1}]({image['sha256']}.{image['extension']})")
        else:
            lines.append(f"- 配图 {image['index']+1}：保存失败")
    lines += ["", "## 采集说明", ""] + capture.get("truncation_reasons", [])
    for name, content in [("note.md", "\n".join(lines)+"\n"), ("capture.json", json.dumps(bundle,ensure_ascii=False,indent=2))]:
        temp = folder / (str(uuid4())+".tmp")
        try:
            temp.write_text(content, encoding="utf-8")
            temp.replace(folder / name)
        finally:
            temp.unlink(missing_ok=True)
    return folder


def collect_account(client, *, account_id, domain_slug, source_kind, actor, media_root=MEDIA_ROOT, call=call_cli, download=download_image):
    if actor.role not in {"admin", "operator"}:
        raise HTTPException(403,"采集需要管理员或运营权限")
    if source_kind not in {"hotspot", "viewpoint"}:
        raise HTTPException(422,"采集类型无效")
    account_id = str(UUID(account_id))
    rows = client.table("channel_accounts").select("*").eq("id",account_id).limit(1).execute().data or []
    if not rows or not rows[0].get("is_active") or rows[0].get("channel") != "xiaohongshu":
        raise HTTPException(409,"请选择启用中的小红书账号")
    config = rows[0].get("config_jsonb") or {}
    if (config.get("onboarding") or {}).get("domain_slug") != domain_slug:
        raise HTTPException(409,"采集赛道与账号规划不一致")
    plan = config.get("collection_plan") or {}
    if plan.get("mode") != "xhs_cli":
        raise HTTPException(409,"此账号未启用本地 CLI 采集")
    steps = [s for s in plan.get("steps",[]) if isinstance(s,dict) and s.get("tool")=="xhs_cli_search" and str(s.get("query") or "").strip()]
    if not steps:
        raise HTTPException(409,"请先配置采集关键词")
    query = str(steps[0]["query"]).strip()[:300]
    limit = max(1,min(3,int(steps[0].get("limit") or 3)))
    domains = client.table("domains").select("id").eq("slug",domain_slug).limit(1).execute().data or []
    if not domains:
        raise HTTPException(404,"赛道不存在")
    result = {"status":"ok","provider":"xiaohongshu_cli","account_id":account_id,
        "domain_slug":domain_slug,"source_kind":source_kind,"collected":0,"inserted":0,"updated":0,
        "images_saved":0,"comments_failed":0,"local_saved":0,"warnings":[],"preview":[]}
    deadline = time.monotonic()+200
    job = {"account_id":account_id,"domain_slug":domain_slug,"source_kind":source_kind,
        "queries":[query],"mode":"xhs_cli","collection_method":"xiaohongshu_cli"}
    with collection_lock(media_root):
        try:
            auth = call("status",timeout=20)
            if not auth.get("authenticated") or auth.get("guest"):
                raise CliFailure("not_authenticated")
            search = call("search",query,timeout=55)
        except CliFailure as exc:
            raise HTTPException(503,"本地采集不可用："+exc.code+"；请检查 CLI 安装和登录状态") from None
        notes = []
        for row in search.get("items") or []:
            note_id = row.get("id") or (row.get("note_card") or {}).get("note_id")
            if isinstance(note_id,str) and re.fullmatch(r"[a-f0-9]{24}",note_id) and note_id not in notes:
                notes.append(note_id)
        for note_id in notes[:limit]:
            if time.monotonic() >= deadline:
                result["warnings"].append("本轮达到时间上限，剩余笔记未采集")
                break
            try:
                capture = read_capture(call("read",note_id,timeout=min(55,max(1,deadline-time.monotonic()))),note_id)
            except (CliFailure,ValueError,TypeError):
                result["warnings"].append("一篇笔记正文读取失败，未计入采集成果")
                continue
            collect_comments(capture, note_id, call=call, deadline=deadline)
            if capture["comments_status"] == "failed":
                result["comments_failed"] += 1
                result["warnings"].extend(capture["truncation_reasons"][-1:])
            item_id = intel_item_id(domains[0]["id"],account_id,f"{source_kind}_xiaohongshu_cli",capture["source_url"])
            save_bundle(media_root,account_id,item_id,capture,"unconfirmed")
            result["local_saved"] += 1
            result["collected"] += 1
            receipt = None
            try:
                receipt = persist_public_capture(client,job=job,capture=capture,summary=None,model_name="",actor=actor)
                item_id = receipt["item_id"]
                result["inserted"] += receipt["inserted"]
                result["updated"] += receipt["updated"]
                saved_rows = client.table("intelligence_items").select("meta_jsonb").eq("account_id",account_id).eq("id",item_id).limit(1).execute().data
                capture = deepcopy(saved_rows[0]["meta_jsonb"]["browser_capture"])
            except Exception:
                result["warnings"].append("数据库保存未确认，本地原文已保留；请核对后再重试")
            previous_images = {i['index']:i for i in capture.get('images',[])}
            images = []
            for candidate in capture["image_candidates"]:
                previous = previous_images.get(candidate['index'])
                path = None
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    if previous and previous.get('status')=='saved' and previous.get('candidate_url')==candidate['url']:
                        from app.services.browser_media import media_path
                        path = media_path(media_root,account_id,item_id,previous['sha256'],previous['extension'])
                    image = previous if path and path.is_file() else store_image(candidate,job=job,item_id=item_id,
                        media_root=media_root,should_continue=lambda:time.monotonic()<deadline,download=download)
                    result["images_saved"] += 1
                except (ValueError,TimeoutError,OSError,RuntimeError,http.client.HTTPException):
                    image = previous if path and path.is_file() else {"index":candidate["index"],"status":"failed","message":"公开图片保存失败或达到本轮上限"}
                    result["warnings"].append("部分图片本轮未保存成功")
                images.append(image)
            capture["images"] = images
            if capture["comments_status"] == "partial":
                result["warnings"].append("评论仅获取了部分内容，未确认全部评论和回复")
            capture["revision"] = sha256(json.dumps(capture,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            db_status = "unconfirmed"
            if receipt:
                try:
                    meta = deepcopy(saved_rows[0]["meta_jsonb"])
                    previous_revision = (meta.get("browser_capture") or {}).get("revision")
                    meta["browser_capture"] = capture
                    update_capture_metadata(client,account_id=account_id,item_id=item_id,meta=meta,revision=capture["revision"],expected_revision=previous_revision)
                    db_status = "saved"
                except Exception:
                    result["warnings"].append("图片索引数据库保存未确认，本地文件已保留")
            folder = save_bundle(media_root,account_id,item_id,capture,db_status)
            knowledge_status = 'pending'
            if db_status == 'saved':
                try:
                    from app.services.collection_knowledge import link_capture_to_knowledge
                    linked = link_capture_to_knowledge(client, account_id=account_id, domain_id=domains[0]['id'], item_id=item_id, actor=actor)
                    knowledge_status = linked['status']
                    result['warnings'].extend(linked['warnings'])
                except Exception:
                    result['warnings'].append('正文已保存，但案例/素材关联未完成，请重试整理资料')
            result["preview"].append({"item_id":item_id,"title":capture["title"],"source_url":capture["source_url"],
                "local_folder":str(folder),"database_status":db_status,"knowledge_status":knowledge_status,"comments_status":capture["comments_status"],"images_saved":sum(i.get('status')=='saved' for i in images)})
        if not notes:
            result["warnings"].append("搜索未返回可读取的公开笔记")
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    if result["warnings"] or result["comments_failed"]:
        result["status"] = "partial"
    result["message"] = f"本地保存 {result['local_saved']} 篇，数据库新增 {result['inserted']} 篇、更新 {result['updated']} 篇，图片保存 {result['images_saved']} 张。"
    if result["comments_failed"]:
        result["message"] += f" {result['comments_failed']} 篇的评论读取失败，正文与图片仍已保留。"
    return result
