from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.agents.browser.bridge_client import BridgeClient
from app.agents.browser.collector import run_collection
from app.agents.browser.model import BrowserModel

router = APIRouter()
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chrome-agent")
active = {}
lock = Lock()


class StartRequest(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    worker_token: str = Field(min_length=30, max_length=100, repr=False)


def run(job_id, token):
    bridge = BridgeClient(job_id, token)
    model = None
    def create_model():
        nonlocal model
        model = BrowserModel()
        return model
    try:
        return run_collection(bridge, create_model)
    finally:
        if model:
            model.close()
        bridge.close()
        with lock:
            active.pop(job_id, None)


@router.post("/browser-collection/start")
def start(request: Request, body: StartRequest):
    if not request.client or request.client.host not in {"127.0.0.1", "::1"} or request.headers.get("origin"):
        raise HTTPException(403, "浏览器采集仅允许本机项目调用")
    bridge = BridgeClient(body.job_id, body.worker_token)
    try:
        state = bridge.state()
        if state["status"] != "queued":
            raise HTTPException(409, "任务已开始或已结束")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "采集任务授权无效")
    finally:
        bridge.close()
    with lock:
        if body.job_id in active:
            raise HTTPException(409, "任务已启动")
        if len(active) >= 2:
            raise HTTPException(409, "已有两个浏览器任务，请稍后再试")
        active[body.job_id] = executor.submit(run, body.job_id, body.worker_token)
    return {"status": "queued", "job_id": body.job_id}
