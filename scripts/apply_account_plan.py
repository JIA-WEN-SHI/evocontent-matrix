"""Apply an explicit account plan without touching login credentials or history."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sys

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "services" / "api"))
    from app.db import get_supabase
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
    with closing(get_supabase()) as connection:
        apply_plan(next(connection), plan, args.account_id)


def apply_plan(client, plan, account_id):
    from app.models import AuditLogEntry
    from app.services.audit import write_audit_log

    slug = plan["domain_slug"]
    accounts = client.table("channel_accounts").select("id,is_active,channel").eq("id", account_id).limit(1).execute().data or []
    if not accounts or not accounts[0].get("is_active") or accounts[0].get("channel") != "xiaohongshu":
        raise RuntimeError("An active Xiaohongshu account is required")
    domains = client.table("domains").select("id").eq("slug", slug).limit(1).execute().data or []
    if not domains:
        profile = plan["strategy_profile"]
        config = {key: profile[key] for key in ["audience", "pain_points", "forbidden_claims", "focus_keywords", "hotspot_queries", "viewpoint_queries"]}
        config.update({"brand_tone": profile["tone_style"], "hooks": ["AI写周报别一口气问：拆成4步"],
                       "automation_policy": {"auto_approve_publish": False},
                       "crawler_template": {"sources": [{"key": "xiaohongshu_search", "enabled": False}, {"key": "google_news_rss", "enabled": False}]},
                       "channel_rules": {"xiaohongshu": "原创实操教程，标明示例和限制；只提交人工审核，不自动发布"}})
        rows = client.table("domains").insert({"slug": slug, "name": plan["domain_name"], "config_jsonb": config}).execute().data or []
        if not rows:
            raise RuntimeError("Domain creation was not confirmed")
        write_audit_log(client, AuditLogEntry(actor="local-ai-replan", action="domain.created", target_type="domain",
                        target_id=rows[0]["id"], diff_jsonb={"slug": slug, "reason": "User requested AI content sharing"}))
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=90,
                      headers={"x-user-id": "local-ai-replan", "x-user-role": "admin"}) as api:
        def send(method, path, body):
            response = api.request(method, path, json=body)
            if not response.is_success:
                raise RuntimeError(f"{path}: {response.status_code} {response.text[:500]}")
            return response.json()

        profile = plan["strategy_profile"]
        onboarding_keys = ["primary_goal", "persona_name", "ip_positioning", "tone_style", "cta_style", "audience",
                           "pain_points", "content_pillars", "forbidden_claims", "focus_keywords", "hotspot_queries",
                           "viewpoint_queries", "publish_constraints"]
        onboarding = {key: profile[key] for key in onboarding_keys}
        onboarding.update({"domain_slug": slug, "posts_per_day": 1, "publish_time_slots": ["20:30"],
                           "checkpoints_hours": plan["feedback_plan"]["checkpoints_hours"],
                           "reason": "用户指定改为 AI 内容分享赛道，保留旧赛道历史"})
        send("POST", f"/api/accounts/{account_id}/onboarding/commit", onboarding)
        send("PUT", f"/api/accounts/{account_id}/strategy", {"strategy_profile": profile, "feedback_plan": plan["feedback_plan"], "reason": "AI 内容分享定位与人工审核约束"})
        send("PUT", f"/api/accounts/{account_id}/collection-plan", {"collection_plan": plan["collection_plan"], "reason": "用户要求直接操作已登录的 Google Chrome，不使用旧接口爬取"})
        send("PATCH", f"/api/accounts/{account_id}", {"account_name": plan["account_name"], "notes": plan["bio"], "tags": ["AI", "工作流", "实用教程"]})
        imported = send("POST", f"/api/ops/accounts/{account_id}/browser-intel", {"domain_slug": slug, "items": plan["browser_samples"], "reason": "Chrome 公开笔记页面观察摘要，不复制正文、不读取登录凭据"})
        saved = api.get(f"/api/accounts/{account_id}/strategy")
        saved.raise_for_status()
        data = saved.json()
        assert data["collection_plan"]["mode"] == "browser_ui"
        assert data["strategy_profile"]["persona_name"] == profile["persona_name"]
        print(json.dumps({"status": "ok", "account_id": account_id, "account_name": data["account_name"], "domain_slug": slug,
                          "collection_mode": data["collection_plan"]["mode"], "intel_import": imported}, ensure_ascii=False))


if __name__ == "__main__":
    main()
