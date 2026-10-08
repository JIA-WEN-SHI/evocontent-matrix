from datetime import datetime, timezone
from typing import Literal

from supabase import Client

from app.model_router import get_router
from app.orchestration.reflection import reflect_and_upgrade
from app.tools.publishing.publisher import publish_with_fallback
from app.types import GraphState


def load_domain_strategy(state: GraphState, client: Client) -> GraphState:
    task_id = state["task_id"]
    task_res = client.table("pipeline_tasks").select("*").eq("id", task_id).limit(1).execute()
    task = task_res.data[0] if task_res.data else None
    if not task:
        return {"error": "task_not_found"}

    domain_res = client.table("domains").select("*").eq("id", task["domain_id"]).limit(1).execute()
    domain = domain_res.data[0] if domain_res.data else None
    if not domain:
        return {"task": task, "error": "domain_not_found"}

    strategy = domain.get("config_jsonb", {}).get("prompt_templates", {})
    return {"task": task, "domain": domain, "strategy": strategy}


def fetch_recent_intel(state: GraphState, client: Client) -> GraphState:
    domain = state.get("domain")
    if not domain:
        return {"intel_items": []}
    res = (
        client.table("intelligence_items")
        .select("raw_text,source_type,source_url,captured_at")
        .eq("domain_id", domain["id"])
        .order("captured_at", desc=True)
        .limit(8)
        .execute()
    )
    return {"intel_items": res.data or []}


def draft_writer(state: GraphState) -> GraphState:
    task = state["task"]
    strategy = state.get("strategy", {})
    intel = state.get("intel_items", [])
    router = get_router()
    draft = router.generate_draft(strategy=strategy, intel=intel, channel=task["channel"])
    return {"draft_title": draft.get("title", ""), "draft_body": draft.get("body", "")}


def create_assets_prompt(state: GraphState) -> GraphState:
    title = state.get("draft_title", "")
    body = state.get("draft_body", "")
    return {
        "image_prompt": f"日系高信任风格封面，标题：{title}，正文主题：{body[:80]}，清晰信息层次，专业服务感"
    }


def human_review_waiter(state: GraphState, client: Client) -> GraphState:
    task = state["task"]
    now = datetime.now(timezone.utc).isoformat()
    payload = task.get("payload_jsonb") if isinstance(task.get("payload_jsonb"), dict) else {}
    next_payload = {
        **payload,
        "title": state.get("draft_title"),
        "body": state.get("draft_body"),
        "image_prompt": state.get("image_prompt"),
    }
    client.table("pipeline_tasks").update(
        {
            "status": "pending_review",
            "stage": "human_review",
            "payload_jsonb": next_payload,
            "updated_at": now,
        }
    ).eq("id", task["id"]).execute()
    return {"task": {**task, "status": "pending_review", "payload_jsonb": next_payload}}


def publisher_playwright(state: GraphState, client: Client) -> GraphState:
    task = state["task"]
    if task["status"] != "approved":
        return {"publish_result": {"status": "skipped", "reason": "task_not_approved"}}

    client.table("pipeline_tasks").update({"status": "publishing", "stage": "publishing"}).eq("id", task["id"]).execute()
    result = publish_with_fallback(task)
    now = datetime.now(timezone.utc).isoformat()
    payload = task.get("payload_jsonb") if isinstance(task.get("payload_jsonb"), dict) else {}
    publish = task.get("publish_jsonb") if isinstance(task.get("publish_jsonb"), dict) else {}
    client.table("pipeline_tasks").update(
        {
            "status": "published" if str(result.get("status")) == "success" else "publish_failed",
            "stage": "feedback_pending" if str(result.get("status")) == "success" else "publishing",
            "published_at": now if str(result.get("status")) == "success" else None,
            "payload_jsonb": {**payload, "last_publish_result": result},
            "publish_jsonb": {**publish, "last_result": result},
            "updated_at": now,
        }
    ).eq("id", task["id"]).execute()
    return {"publish_result": result}


def collect_metrics(state: GraphState, client: Client) -> GraphState:
    task = state["task"]
    latest_task_res = client.table("pipeline_tasks").select("*").eq("id", task["id"]).limit(1).execute()
    latest_task = latest_task_res.data[0] if latest_task_res.data else task
    if latest_task["status"] == "published":
        client.table("pipeline_tasks").update({"stage": "feedback_pending"}).eq("id", task["id"]).execute()
    return {"task": latest_task}


def reflect_and_upgrade_strategy(state: GraphState, client: Client) -> GraphState:
    domain = state.get("domain")
    if not domain:
        return {"reflection_result": {"status": "skipped", "reason": "domain_missing"}}
    result = reflect_and_upgrade(client, domain["slug"])
    task = state.get("task")
    if task and result.get("status") == "ok":
        client.table("pipeline_tasks").update({"status": "done", "stage": "closed"}).eq("id", task["id"]).execute()
    return {"reflection_result": result}


def route_by_status(state: GraphState) -> Literal["draft_path", "publish_path", "already_pending", "stop"]:
    task = state.get("task")
    if not task:
        return "stop"
    status = task["status"]
    if status in {"queued", "intel_ready", "drafting", "review_rejected"}:
        return "draft_path"
    if status == "approved":
        return "publish_path"
    if status == "pending_review":
        return "already_pending"
    return "stop"
