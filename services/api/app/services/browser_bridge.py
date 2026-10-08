from __future__ import annotations

from copy import deepcopy
import re
import secrets
from threading import RLock
import time
from uuid import uuid4

from fastapi import HTTPException


OPERATIONS = {"observe", "search", "open_note", "scroll_note", "close_note", "read_note", "expand_body", "scroll_comments", "expand_reply", "next_image"}
TERMINAL = {"completed", "partial", "cancelled", "failed", "model_unavailable"}
EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")


class BrowserBridgeStore:
    """Single-process capability broker; credentials never enter public job views."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = RLock()
        self.pairings = {}
        self.sessions = {}
        self.jobs = {}
        self.commands = {}

    def _fail(self, message, code=409):
        raise HTTPException(code, message)

    def _prune(self):
        now = self.clock()
        self.pairings = {k: v for k, v in self.pairings.items() if v["expires"] > now}
        for job in self.jobs.values():
            if job["status"] not in TERMINAL and now >= job["deadline"]:
                self._finish(job, "partial" if job["collected"] else "failed", "已达到 10 分钟采集时限")
        if len(self.jobs) > 100:
            for job_id in list(self.jobs):
                if self.jobs[job_id]["status"] in TERMINAL:
                    del self.jobs[job_id]
                    self.commands = {k: c for k, c in self.commands.items() if c["job_id"] != job_id}
                    if len(self.jobs) <= 100:
                        break

    def create_pairing(self, account_id, domain_slug, actor, mode="foreground_visual"):
        with self.lock:
            if mode not in {"foreground_visual", "background_text"}:
                self._fail("采集模式无效", 422)
            self._prune()
            self.pairings = {k: v for k, v in self.pairings.items() if v["account_id"] != account_id}
            if len(self.pairings) >= 20:
                self._fail("配对请求过多，请稍后再试", 429)
            code = secrets.token_urlsafe(18)
            self.pairings[code] = {"account_id": account_id, "domain_slug": domain_slug, "actor": actor, "mode": mode, "protocol_version": 2, "expires": self.clock() + 300}
            return {"code": code, "expires_in": 300, "account_id": account_id}

    def pair(self, code, origin, tab_id, protocol_version=2):
        with self.lock:
            self._prune()
            if not EXTENSION_ORIGIN.fullmatch(origin or "") or not isinstance(tab_id, int) or tab_id < 0:
                self._fail("请通过项目 Chrome 插件连接", 403)
            pairing = self.pairings.get(code)
            if not pairing:
                self._fail("配对码已失效，请在项目中重新生成", 401)
            if protocol_version != 2 and (protocol_version != 1 or pairing["mode"] != "foreground_visual"):
                self._fail("请更新 Chrome 插件到 1.1.0 后重新配对", 409)
            self.pairings.pop(code)
            if any(s["origin"] == origin and s["tab_id"] == tab_id for s in self.sessions.values()):
                self._fail("此标签页已连接，请先在插件中断开")
            self.disconnect(pairing["account_id"])
            token = secrets.token_urlsafe(32)
            session = {**pairing, "protocol_version":protocol_version, "id": str(uuid4()), "origin": origin, "tab_id": tab_id, "last_seen": self.clock()}
            self.sessions[token] = session
            return {"token": token, "account_id": session["account_id"], "domain_slug": session["domain_slug"], "tab_id": tab_id, "mode": session["mode"], "protocol_version": session["protocol_version"]}

    def pairing_info(self, code):
        with self.lock:
            self._prune()
            value = self.pairings.get(code)
            if not value:
                self._fail("配对码已失效，请重新生成", 401)
            return {k: value[k] for k in ("account_id", "domain_slug", "mode", "protocol_version")}

    def extension_session(self, token, origin):
        with self.lock:
            session = self.sessions.get(token)
            if not session or session["origin"] != origin or self.clock() - session["last_seen"] > 60:
                if session:
                    self.disconnect(session["account_id"])
                self._fail("浏览器连接已失效，请重新配对", 401)
            session["last_seen"] = self.clock()
            return session

    def status(self, account_id):
        with self.lock:
            self._prune()
            session = next((s for s in self.sessions.values() if s["account_id"] == account_id), None)
            if session and self.clock() - session["last_seen"] > 60:
                self.disconnect(account_id)
                session = None
            jobs = [j for j in self.jobs.values() if j["account_id"] == account_id]
            return {"connected": bool(session), "account_id": account_id,
                    "mode": session["mode"] if session else None, "protocol_version": session["protocol_version"] if session else 2,
                    "domain_slug": session["domain_slug"] if session else None,
                    "tab_id": session["tab_id"] if session else None,
                    "job": self._view(jobs[-1]) if jobs else None}

    def disconnect(self, account_id):
        with self.lock:
            self.sessions = {k: s for k, s in self.sessions.items() if s["account_id"] != account_id}
            for job in self.jobs.values():
                if job["account_id"] == account_id and job["status"] not in TERMINAL:
                    self._finish(job, "cancelled", "浏览器已断开")

    def start_job(self, account_id, domain_slug, queries, source_kind):
        with self.lock:
            self._prune()
            if not self.status(account_id)["connected"]:
                self._fail("请先在已登录的小红书标签页连接插件")
            session = next(s for s in self.sessions.values() if s["account_id"] == account_id)
            if session["domain_slug"] != domain_slug:
                self._fail("浏览器连接赛道与账号不一致")
            if any(j["account_id"] == account_id and j["status"] not in TERMINAL for j in self.jobs.values()):
                self._fail("此账号已有采集任务，请先停止或等待完成")
            job = {"id": str(uuid4()), "account_id": account_id, "domain_slug": domain_slug,
                   "actor": session["actor"], "session_id": session["id"], "worker_token": secrets.token_urlsafe(32),
                   "status": "queued", "message": "正在启动项目浏览器 Agent", "queries": queries[:3],
                   "source_kind": source_kind, "deadline": self.clock() + 600, "mode": session["mode"], "protocol_version": session["protocol_version"],
                   "collected": 0, "inserted": 0, "duplicates": 0, "updated": 0, "events": [], "sequence": 0, "saved_urls": [], "media_bytes": 0}
            self.jobs[job["id"]] = job
            return deepcopy(job)

    def _view(self, job):
        keys = ("id", "account_id", "domain_slug", "status", "message", "collected", "inserted", "duplicates", "updated", "events", "mode", "protocol_version", "sequence")
        return deepcopy({k: job[k] for k in keys})

    def job_view(self, job_id, account_id):
        with self.lock:
            self._prune()
            job = self.jobs.get(job_id)
            if not job or job["account_id"] != account_id:
                self._fail("任务已失效或不属于当前账号，请重新连接", 404)
            return self._view(job)

    def worker_job(self, job_id, token):
        with self.lock:
            self._prune()
            job = self.jobs.get(job_id)
            if not job or not secrets.compare_digest(token or "", job["worker_token"]):
                self._fail("采集任务授权已失效", 401)
            return job

    def active(self, job):
        self._prune()
        if job["status"] in TERMINAL:
            self._fail("采集任务已结束", 410)
        if job["status"] == "awaiting_user":
            self._fail("采集正在等待用户操作")
        if not any(s["id"] == job["session_id"] for s in self.sessions.values()):
            self._finish(job, "cancelled", "浏览器连接失效")
            self._fail("浏览器连接失效", 410)

    def enqueue(self, job_id, operation, arguments):
        with self.lock:
            job = self.jobs[job_id]
            self.active(job)
            if operation not in OPERATIONS:
                self._fail("此浏览器操作不允许", 403)
            if any(c["job_id"] == job_id and c["result"] is None for c in self.commands.values()):
                self._fail("上一步操作尚未完成")
            job["sequence"] += 1
            if job["sequence"] > 120:
                self._fail("已达到浏览器操作上限")
            command = {"id": str(uuid4()), "job_id": job_id, "sequence": job["sequence"], "operation": operation,
                       "arguments": deepcopy(arguments), "leased": False, "expires": self.clock() + 45, "result": None}
            command["mode"] = job["mode"]
            command["protocol_version"] = job["protocol_version"]
            self.commands[command["id"]] = command
            job["status"] = "running"
            job["message"] = {"observe": "正在查看公开页面", "search": "正在搜索公开笔记", "open_note": "正在打开笔记", "read_note": "正在读取公开资料", "scroll_note": "正在查看更多正文", "close_note": "返回搜索结果", "expand_body": "正在展开正文", "scroll_comments": "正在加载公开评论", "expand_reply": "正在展开公开回复", "next_image": "正在查看笔记配图"}[operation]
            job["events"] = (job["events"] + [job["message"]])[-30:]
            return deepcopy({k: command[k] for k in ("id", "sequence", "operation", "arguments", "mode", "protocol_version")})

    def lease_command(self, token, origin):
        with self.lock:
            session = self.extension_session(token, origin)
            self._prune()
            for command in self.commands.values():
                job = self.jobs.get(command["job_id"])
                if not job or job["session_id"] != session["id"] or job["status"] != "running" or command["result"] is not None:
                    continue
                if self.clock() >= command["expires"]:
                    self._finish(job, "partial" if job["collected"] else "failed", "浏览器操作超时，请重新开始")
                    continue
                if not command["leased"]:
                    command["leased"] = True
                    return deepcopy({k: command[k] for k in ("id", "sequence", "operation", "arguments", "mode", "protocol_version")})
            return None

    def pause_session(self, token, origin, message):
        with self.lock:
            session = self.extension_session(token, origin)
            self._prune()
            for job in self.jobs.values():
                if job["session_id"] != session["id"] or job["status"] in TERMINAL:
                    continue
                job["status"] = "awaiting_user"
                job["message"] = message
                for command in self.commands.values():
                    if command["job_id"] == job["id"] and command["result"] is None:
                        command["result"] = {"ok":False,"paused":True,"message":message}

    def complete_command(self, token, origin, command_id, result):
        with self.lock:
            session = self.extension_session(token, origin)
            command = self.commands.get(command_id)
            job = self.jobs.get(command["job_id"]) if command else None
            if not job or job["session_id"] != session["id"]:
                self._fail("浏览器操作不属于此连接", 403)
            self.active(job)
            if not command["leased"] or command["result"] is not None or self.clock() >= command["expires"]:
                self._fail("浏览器操作已过期或已确认")
            command["result"] = deepcopy(result)
            if result.get("paused"):
                job["status"] = "awaiting_user"
                job["message"] = result.get("message") or "请处理小红书页面后点击继续"

    def command_result(self, job, command_id):
        with self.lock:
            self._prune()
            command = self.commands.get(command_id)
            if not command or command["job_id"] != job["id"]:
                self._fail("浏览器操作已失效", 404)
            if job["status"] not in TERMINAL and command["result"] is None and self.clock() >= command["expires"]:
                self._finish(job, "partial" if job["collected"] else "failed", "浏览器操作超时")
            return {"result": deepcopy(command["result"]), "status": job["status"]}

    def resume(self, job_id, account_id):
        with self.lock:
            self.job_view(job_id, account_id)
            job = self.jobs[job_id]
            if job["status"] != "awaiting_user":
                self._fail("此任务当前不需要继续")
            job["status"] = "running"
            job["message"] = "已继续，将重新检查公开页面"
            return self._view(job)

    def stop(self, job_id, account_id):
        with self.lock:
            self.job_view(job_id, account_id)
            job = self.jobs[job_id]
            if job["status"] not in TERMINAL:
                self._finish(job, "cancelled", "用户已停止采集")
            return self._view(job)

    def _finish(self, job, status, message):
        job["status"] = status
        job["message"] = message

    def finish(self, job, status, message):
        with self.lock:
            self._prune()
            if job["status"] not in TERMINAL:
                if status not in TERMINAL:
                    self._fail("任务结束状态不合法", 422)
                self._finish(job, status, message[:300])
            return self._view(job)


bridge_store = BrowserBridgeStore()
