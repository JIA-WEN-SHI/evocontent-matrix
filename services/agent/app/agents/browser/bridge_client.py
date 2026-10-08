import time
from urllib.parse import urlsplit

import httpx

from app.config import get_settings


TERMINAL = {"completed", "partial", "cancelled", "failed", "model_unavailable"}


class BridgeClient:
    def __init__(self, job_id, token):
        base = get_settings().browser_bridge_api_url.rstrip("/")
        url = urlsplit(base)
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password:
            raise ValueError("浏览器连接仅限本机")
        self.url = base + "/api/browser-bridge/worker/" + job_id
        self.client = httpx.Client(trust_env=False, timeout=20, headers={"Authorization": "Bearer " + token})
        self.deadline = time.monotonic() + 600

    def call(self, path="", body=None):
        response = self.client.get(self.url + path) if body is None else self.client.post(self.url + path, json=body)
        response.raise_for_status()
        return response.json()

    def state(self):
        return self.call()

    def wait_active(self):
        while time.monotonic() < self.deadline:
            state = self.state()
            if state["status"] in TERMINAL:
                raise RuntimeError("任务已结束")
            if state["status"] != "awaiting_user":
                return
            time.sleep(1)
        raise TimeoutError("任务已超时")

    def execute(self, operation, arguments):
        self.wait_active()
        command = self.call("/commands", {"operation": operation, "arguments": arguments})
        while time.monotonic() < self.deadline:
            data = self.call("/commands/" + command["id"])
            if data["status"] in TERMINAL:
                raise RuntimeError("任务已结束")
            result = data.get("result")
            if result:
                if result.get("paused"):
                    self.wait_active()
                    refreshed = self.execute("observe", {})
                    refreshed["reobserved"] = True
                    return refreshed
                return result
            time.sleep(0.5)
        raise TimeoutError("浏览器操作超时")

    def save(self, summary, model_name):
        state = self.state()
        if state.get("mode") == "background_text" and state.get("sequence",0) < 120:
            result = self.execute("observe",{})
            if not result.get("ok") or result.get("reobserved"):
                raise RuntimeError("保存前的公开页面状态未确认")
        receipt = self.call("/save", {"summary": summary, "model_name": model_name})
        if receipt.get("saved") and receipt.get("item_id"):
            for index in receipt.get("image_indices",[])[:24]:
                state = self.state()
                if time.monotonic() >= self.deadline or state["status"] in TERMINAL or state.get("sequence",0) >= 120:
                    break
                try:
                    result = self.execute("observe",{})
                    if not result.get("ok") or result.get("reobserved"):
                        break
                    self.call(f"/media/{receipt['item_id']}/{index}",{})
                except (httpx.HTTPError,RuntimeError,TimeoutError):
                    # Preserve confirmed text and reconcile media via the detail view.
                    break
        return receipt

    def update_summary(self, item_id, summary, model_name):
        return self.call(f"/summary/{item_id}",{"summary":summary,"model_name":model_name})

    def finish(self, status, message):
        try:
            return self.call("/finish", {"status": status, "message": message})
        except httpx.HTTPError:
            return {"status": "failed", "message": "项目连接已失效，请重新连接并核对资料"}

    def close(self):
        self.client.close()
