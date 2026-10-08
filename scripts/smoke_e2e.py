from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
API_BASE = "http://127.0.0.1:8000"
AGENT_BASE = "http://127.0.0.1:8100"
WEB_BASE = "http://127.0.0.1:3001"


def wait_http(url: str, timeout_sec: int = 120) -> None:
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            response = requests.get(url, timeout=5)
            if response.ok:
                return
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"Timeout waiting for {url}")


def post_with_retry(url: str, headers: dict[str, str], timeout: int = 120, attempts: int = 3) -> requests.Response:
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            response = requests.post(url, headers=headers, timeout=timeout)
            if response.status_code < 500:
                return response
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(1.5 + i)
    if last_exc:
        raise last_exc
    response.raise_for_status()
    return response


def start_proc(cmd: list[str], cwd: Path, out_log: Path, err_log: Path) -> subprocess.Popen:
    out_log.parent.mkdir(parents=True, exist_ok=True)
    out = out_log.open("w", encoding="utf-8")
    err = err_log.open("w", encoding="utf-8")
    return subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=err)


def stop_proc(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> None:
    env = {**os.environ}
    env_file = dotenv_values(ROOT / ".env")
    for k, v in env_file.items():
        if v is not None and k not in env:
            env[k] = v

    api_proc = agent_proc = web_proc = None
    headers = {"x-user-id": "smoke-admin", "x-user-role": "admin", "Content-Type": "application/json"}

    try:
        api_proc = start_proc(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--app-dir", "services/api"],
            ROOT,
            LOG_DIR / "smoke.api.out.log",
            LOG_DIR / "smoke.api.err.log",
        )
        agent_proc = start_proc(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8100", "--app-dir", "services/agent"],
            ROOT,
            LOG_DIR / "smoke.agent.out.log",
            LOG_DIR / "smoke.agent.err.log",
        )
        web_proc = start_proc(
            ["npm.cmd", "run", "dev", "--", "--hostname", "127.0.0.1"],
            ROOT / "apps" / "web",
            LOG_DIR / "smoke.web.out.log",
            LOG_DIR / "smoke.web.err.log",
        )

        wait_http(f"{API_BASE}/health", 120)
        wait_http(f"{AGENT_BASE}/health", 120)
        wait_http(f"{WEB_BASE}/dashboard", 180)
        wait_http(f"{WEB_BASE}/approval", 180)

        queued = requests.get(
            f"{API_BASE}/api/pipeline/tasks",
            headers=headers,
            params={"status": "queued", "limit": 20},
            timeout=60,
        )
        queued.raise_for_status()
        queued_items = queued.json().get("items", [])
        if not queued_items:
            raise RuntimeError("No queued task found. Seed demo data first.")
        queued_id = queued_items[0]["id"]

        run_draft = post_with_retry(f"{API_BASE}/api/agents/run-task/{queued_id}", headers=headers, timeout=120, attempts=3)
        run_draft.raise_for_status()

        task_after_draft = requests.get(f"{API_BASE}/api/pipeline/tasks/{queued_id}", headers=headers, timeout=60)
        task_after_draft.raise_for_status()
        draft_status = task_after_draft.json().get("status")

        # Approve the same task we just drafted to keep smoke deterministic.
        review_id = queued_id

        approve = requests.post(f"{API_BASE}/api/pipeline/tasks/{review_id}/approve", headers=headers, timeout=60)
        approve.raise_for_status()
        run_publish = post_with_retry(f"{API_BASE}/api/agents/run-task/{review_id}", headers=headers, timeout=120, attempts=3)
        run_publish.raise_for_status()

        task_after_publish = requests.get(f"{API_BASE}/api/pipeline/tasks/{review_id}", headers=headers, timeout=60)
        task_after_publish.raise_for_status()
        published_task = task_after_publish.json()

        webhook_secret = env.get("WEBHOOK_SHARED_SECRET", "local-dev-webhook-secret")
        payload: dict[str, Any] = {
            "utm_code": ((published_task.get("payload_jsonb") or {}).get("utm_code") if isinstance(published_task.get("payload_jsonb"), dict) else None) or "utm_demo_r_001",
            "form_id": ((published_task.get("payload_jsonb") or {}).get("form_id") if isinstance(published_task.get("payload_jsonb"), dict) else None) or "form_demo_001",
            "channel": published_task.get("channel") or "xiaohongshu",
            "event_time": datetime.now(timezone.utc).isoformat(),
            "contact_fields": {"phone": "13800138000", "wechat": "smoke_wechat_id"},
            "meta_jsonb": {"source": "smoke_e2e"},
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sig = hmac.new(webhook_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        lead = requests.post(
            f"{API_BASE}/api/webhooks/leads",
            data=raw,
            headers={"Content-Type": "application/json", "x-signature": sig},
            timeout=60,
        )
        lead.raise_for_status()
        lead_data = lead.json()

        final_task = requests.get(f"{API_BASE}/api/pipeline/tasks/{review_id}", headers=headers, timeout=60)
        final_task.raise_for_status()
        final_data = final_task.json()

        summary = {
            "web_dashboard": 200,
            "web_approval": 200,
            "queued_task_id": queued_id,
            "draft_status": draft_status,
            "review_task_id": review_id,
            "post_publish_status": published_task.get("status"),
            "published_utm": ((published_task.get("payload_jsonb") or {}).get("utm_code") if isinstance(published_task.get("payload_jsonb"), dict) else None),
            "published_form_id": ((published_task.get("payload_jsonb") or {}).get("form_id") if isinstance(published_task.get("payload_jsonb"), dict) else None),
            "lead_event_pipeline_task_id": (lead_data.get("meta_jsonb") or {}).get("pipeline_task_id") if isinstance(lead_data.get("meta_jsonb"), dict) else None,
            "final_leads_generated": ((final_data.get("metrics_jsonb") or {}).get("leads_generated") if isinstance(final_data.get("metrics_jsonb"), dict) else None),
        }
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        stop_proc(web_proc)
        stop_proc(agent_proc)
        stop_proc(api_proc)


if __name__ == "__main__":
    main()
