from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from supabase import Client, create_client


ROOT = Path(__file__).resolve().parents[1]
DOMAIN_SLUG = "japan_immigration"
DOMAIN_NAME = "日本移民内容增长实验室"
SEED_MARKER = "demo_closed_loop_v1"
RUN_KEY_PREFIX = "demo-loop-"


def load_env_file() -> Dict[str, str]:
    env: Dict[str, str] = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return env
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            env[key] = value
    return env


def get_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    from_file = load_env_file().get(name, "").strip()
    if from_file:
        return from_file
    raise RuntimeError(f"Missing required env var: {name}")


def apply_network_overrides(supabase_url: str) -> None:
    # Some local proxy tools break TLS handshake to Supabase.
    # Default behavior: keep current environment. Set DEMO_SEED_DISABLE_PROXY=true to force bypass.
    disable_proxy = os.getenv("DEMO_SEED_DISABLE_PROXY", "false").strip().lower() not in {"0", "false", "no", "off"}
    if not disable_proxy:
        return
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    host = supabase_url.replace("https://", "").replace("http://", "").split("/")[0]
    no_proxy_values = ["localhost", "127.0.0.1", host]
    current = os.getenv("NO_PROXY", "")
    if current:
        no_proxy_values.append(current)
    os.environ["NO_PROXY"] = ",".join(dict.fromkeys([item for item in no_proxy_values if item]))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


def table_ready(client: Client, table_name: str) -> bool:
    try:
        client.table(table_name).select("id").limit(1).execute()
        return True
    except Exception:
        return False


def safe_insert(client: Client, table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows or not table_ready(client, table):
        return []
    res = client.table(table).insert(rows).execute()
    data = getattr(res, "data", None)
    return data or []


def ensure_domain(client: Client) -> Dict[str, Any]:
    rows = client.table("domains").select("id,slug,name,config_jsonb").eq("slug", DOMAIN_SLUG).limit(1).execute().data or []
    if rows:
        domain = rows[0]
    else:
        inserted = (
            client.table("domains")
            .insert(
                {
                    "slug": DOMAIN_SLUG,
                    "name": DOMAIN_NAME,
                    "config_jsonb": {},
                }
            )
            .execute()
            .data
            or []
        )
        if not inserted:
            raise RuntimeError("Failed to create domain")
        domain = inserted[0]

    config = domain.get("config_jsonb") if isinstance(domain.get("config_jsonb"), dict) else {}
    changed = False
    if not isinstance(config.get("focus_keywords"), list) or not config.get("focus_keywords"):
        config["focus_keywords"] = ["日本移民", "经营管理签证", "日本永住", "赴日工作"]
        changed = True
    if not isinstance(config.get("hotspot_queries"), list) or not config.get("hotspot_queries"):
        config["hotspot_queries"] = ["日本移民", "日本经营管理签证", "日本高度人才签证", "日本永住", "日本工签"]
        changed = True
    if not isinstance(config.get("viewpoint_queries"), list) or not config.get("viewpoint_queries"):
        config["viewpoint_queries"] = ["日本移民避坑", "经营管理签证真实经历", "日本永住流程", "日本工作签证政策"]
        changed = True
    if changed:
        client.table("domains").update({"config_jsonb": config}).eq("id", domain["id"]).execute()
        domain["config_jsonb"] = config
    return domain


def _merge_defaults(base: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
            continue
        if isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_defaults(merged[key], value)
    return merged


def ensure_account(client: Client) -> Dict[str, Any]:
    rows = (
        client.table("channel_accounts")
        .select("id,channel,account_name,account_handle,is_active,config_jsonb")
        .eq("channel", "xiaohongshu")
        .eq("is_active", True)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    defaults = {
        "strategy_profile": {
            "persona_name": "日本移民实操顾问",
            "ip_positioning": "给家庭用户做日本身份与路径规划",
            "tone_style": "理性、可执行、反焦虑",
            "primary_goal": "曝光优先",
            "mcp_mode": "disabled",
        },
        "collection_plan": {
            "keywords": ["日本移民", "日本经营管理签证", "日本高度人才签证", "日本永住"],
            "platforms": ["xiaohongshu"],
            "daily_limit": 30,
        },
        "feedback_plan": {"checkpoints_hours": [1, 3, 24]},
        "publish_preferences": {
            "next_publish_slot_local": "20:30",
            "min_action_gap_seconds": 2,
            "max_action_gap_seconds": 6,
        },
    }
    if rows:
        account = rows[0]
        config = account.get("config_jsonb") if isinstance(account.get("config_jsonb"), dict) else {}
        merged = _merge_defaults(config, defaults)
        if merged != config:
            updated = (
                client.table("channel_accounts")
                .update({"config_jsonb": merged})
                .eq("id", account["id"])
                .execute()
                .data
                or []
            )
            if updated:
                account = updated[0]
        return account

    inserted = (
        client.table("channel_accounts")
        .insert(
            {
                "channel": "xiaohongshu",
                "account_name": "日本移民增长演示号",
                "account_handle": "@demo_japan_growth",
                "login_mode": "storage_state",
                "storage_state_path": "artifacts/playwright/demo.storage.json",
                "publish_selector": "button:has-text('发布')",
                "is_active": True,
                "tags": ["demo", "single_account"],
                "config_jsonb": defaults,
                "notes": f"generated by {SEED_MARKER}",
            }
        )
        .execute()
        .data
        or []
    )
    if not inserted:
        raise RuntimeError("Failed to create active xiaohongshu account")
    return inserted[0]


def _delete_with_filters(client: Client, table: str, filters: Iterable[tuple[str, str, Any]]) -> None:
    if not table_ready(client, table):
        return
    query = client.table(table).delete()
    for mode, key, value in filters:
        if mode == "eq":
            query = query.eq(key, value)
        elif mode == "like":
            query = query.like(key, value)
    query.execute()


def clear_demo_data(client: Client, domain_id: str, account_id: str) -> None:
    _delete_with_filters(
        client,
        "pipeline_tasks",
        [("eq", "domain_id", domain_id), ("eq", "created_by", SEED_MARKER)],
    )
    _delete_with_filters(
        client,
        "memory_items",
        [("eq", "domain_id", domain_id), ("eq", "created_by", SEED_MARKER)],
    )
    _delete_with_filters(
        client,
        "topics",
        [("eq", "domain_id", domain_id), ("eq", "created_by", SEED_MARKER)],
    )
    _delete_with_filters(
        client,
        "reviews",
        [("eq", "domain_id", domain_id), ("eq", "created_by", SEED_MARKER)],
    )
    _delete_with_filters(
        client,
        "assets",
        [("eq", "domain_id", domain_id), ("eq", "created_by", SEED_MARKER)],
    )
    _delete_with_filters(
        client,
        "cases",
        [("eq", "domain_id", domain_id), ("eq", "created_by", SEED_MARKER)],
    )
    _delete_with_filters(
        client,
        "prompt_versions",
        [("eq", "account_id", account_id), ("like", "version", "demo-%")],
    )
    _delete_with_filters(
        client,
        "daily_ops_reports",
        [("eq", "domain_slug", DOMAIN_SLUG), ("like", "run_key", f"{RUN_KEY_PREFIX}%")],
    )
    _delete_with_filters(
        client,
        "runs_daily",
        [("eq", "domain_id", domain_id), ("like", "run_key", f"{RUN_KEY_PREFIX}%")],
    )
    _delete_with_filters(
        client,
        "intelligence_items",
        [("eq", "domain_id", domain_id), ("like", "source_type", "demo_%")],
    )
    _delete_with_filters(
        client,
        "ingestion_logs",
        [("eq", "domain_id", domain_id), ("eq", "source", SEED_MARKER)],
    )


def seed_demo_data(client: Client, domain: Dict[str, Any], account: Dict[str, Any]) -> Dict[str, Any]:
    domain_id = str(domain["id"])
    account_id = str(account["id"])
    now = datetime.now(timezone.utc)

    public_intel_titles = [
        "日本高度人才签证积分门槛更新，咨询量明显上涨",
        "经营管理签证资金证明审查趋严，资料一致性成关键",
        "日本工签配偶就业新问答发布，家庭决策窗口出现",
        "小红书“赴日工作”话题近7天讨论热度上升",
        "教育焦虑相关内容在移民赛道互动率显著更高",
        "签证驳回复盘帖偏好“案例+清单”结构",
    ]
    account_intel_titles = [
        "对比型标题（A/B路径）比单点说明更容易被收藏",
        "带预算区间的内容比纯政策解读评论更高",
        "开头先给结论再给流程，平均停留更好",
        "“踩坑复盘”题材审核通过后互动稳定",
        "晚间20:30发布的曝光更集中",
        "用户最关心：预算、时间、家庭落地难度",
        "近期高赞内容普遍使用三段式结构",
        "关键词“经营管理签证”仍是主流入口",
    ]

    intel_rows: List[Dict[str, Any]] = []
    for idx, title in enumerate(public_intel_titles):
        intel_rows.append(
            {
                "domain_id": domain_id,
                "account_id": None,
                "source_type": "demo_public_hotspot",
                "source_url": f"https://demo.local/public/{idx + 1}",
                "captured_at": (now - timedelta(hours=idx + 2)).isoformat(),
                "raw_text": f"{title}。建议作为公共热点输入用于今日选题。",
                "meta_jsonb": {
                    "title": title,
                    "source": "public",
                    "demo_marker": SEED_MARKER,
                },
            }
        )
    for idx, title in enumerate(account_intel_titles):
        intel_rows.append(
            {
                "domain_id": domain_id,
                "account_id": account_id,
                "source_type": "demo_account_signal",
                "source_url": f"https://demo.local/account/{idx + 1}",
                "captured_at": (now - timedelta(minutes=idx * 20 + 30)).isoformat(),
                "raw_text": f"{title}。这是账号专属输入，优先用于本轮草稿优化。",
                "meta_jsonb": {
                    "title": title,
                    "source": "account",
                    "demo_marker": SEED_MARKER,
                },
            }
        )
    safe_insert(client, "intelligence_items", intel_rows)

    case_rows = [
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "platform": "xiaohongshu",
            "author": "demo_author_1",
            "url": "https://demo.local/case/1",
            "title": "预算30万以内，经营管理签证怎么规划",
            "content": "案例拆解：预算、时间线、家庭落地、常见误区。",
            "metrics": {"likes": 342, "collects": 211, "comments_count": 68},
            "hook": "先看预算，再定路径",
            "structure": "问题-对比-结论",
            "analysis": "高收藏来自明确预算区间与步骤清单。",
            "source_type": "demo_case",
            "source_ref": "demo:case:1",
            "created_by": SEED_MARKER,
        },
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "platform": "xiaohongshu",
            "author": "demo_author_2",
            "url": "https://demo.local/case/2",
            "title": "日本永住误区：不是年限到了就一定过",
            "content": "复盘3个失败原因和补救动作。",
            "metrics": {"likes": 289, "collects": 175, "comments_count": 91},
            "hook": "年限够了但还是被卡？",
            "structure": "误区-原因-修正",
            "analysis": "评论高，说明“误区型内容”适合曝光。",
            "source_type": "demo_case",
            "source_ref": "demo:case:2",
            "created_by": SEED_MARKER,
        },
    ]
    safe_insert(client, "cases", case_rows)

    asset_rows = [
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "type": "hook_template",
            "content": "先给结论：你现在最该做的是先确认预算和身份路径，不是先找中介。",
            "source": "demo_case",
            "usable_scene": "开头3秒抓注意力",
            "is_verified": True,
            "summary": "反常识开头模板",
            "source_type": "demo_asset",
            "source_ref": "demo:asset:hook:1",
            "created_by": SEED_MARKER,
        },
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "type": "title_template",
            "content": "预算X万，走哪条日本身份路径更稳？",
            "source": "demo_case",
            "usable_scene": "曝光型标题",
            "is_verified": True,
            "summary": "预算对比标题模板",
            "source_type": "demo_asset",
            "source_ref": "demo:asset:title:1",
            "created_by": SEED_MARKER,
        },
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "type": "structure_sop",
            "content": "结构：结论先行 -> 2条对比 -> 1条风险提醒 -> CTA私信关键词。",
            "source": "demo_review",
            "usable_scene": "正文结构",
            "is_verified": True,
            "summary": "三段式正文 SOP",
            "source_type": "demo_asset",
            "source_ref": "demo:asset:structure:1",
            "created_by": SEED_MARKER,
        },
    ]
    safe_insert(client, "assets", asset_rows)

    topic_rows = [
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "title": "预算30万家庭，经营管理签证可行性实测",
            "topic_description": "以家庭视角做路径对比，突出预算与时间。",
            "target_user": "有孩子的中产家庭",
            "platform": "xiaohongshu",
            "structure_type": "对比清单",
            "status": "todo",
            "reason": "公共热点+账号历史高收藏主题重合",
            "source_type": "demo_topic",
            "source_ref": "demo:topic:1",
            "created_by": SEED_MARKER,
        },
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "title": "高度人才签证：积分不够还有什么替代路线",
            "topic_description": "高频咨询问题，适合转化评论讨论。",
            "target_user": "职场技术人",
            "platform": "xiaohongshu",
            "structure_type": "问答拆解",
            "status": "drafted",
            "reason": "近7天讨论热度上升",
            "source_type": "demo_topic",
            "source_ref": "demo:topic:2",
            "created_by": SEED_MARKER,
        },
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "title": "日本永住失败复盘：这3个细节最容易忽略",
            "topic_description": "误区型内容，适合拉评论互动。",
            "target_user": "已在准备永住的人群",
            "platform": "xiaohongshu",
            "structure_type": "复盘清单",
            "status": "published",
            "reason": "过往同类内容互动稳定",
            "source_type": "demo_topic",
            "source_ref": "demo:topic:3",
            "created_by": SEED_MARKER,
        },
    ]
    inserted_topics = safe_insert(client, "topics", topic_rows)
    topic_id_map = {str(item.get("source_ref")): str(item.get("id")) for item in inserted_topics if item.get("id")}

    pending_titles = [
        "预算25万能不能做日本经营管理签证？",
        "日本工签转永住，什么时候开始准备最稳",
        "日本移民别只看政策，先看家庭目标是否一致",
    ]
    pipeline_rows: List[Dict[str, Any]] = []
    for idx, title in enumerate(pending_titles):
        pipeline_rows.append(
            {
                "domain_id": domain_id,
                "account_id": account_id,
                "channel": "xiaohongshu",
                "content_type": "post",
                "status": "pending_review",
                "stage": "pending_review",
                "intent_jsonb": {
                    "topic": title,
                    "goal": "exposure",
                    "demo_marker": SEED_MARKER,
                },
                "payload_jsonb": {
                    "title": title,
                    "body": "先给结论，再给步骤，最后给风险提醒和行动建议。",
                    "topic": title,
                    "channel_account_id": account_id,
                    "analysis_jsonb": {"angle": "曝光优先", "source": "KB+FB"},
                    "demo_tag": SEED_MARKER,
                },
                "review_jsonb": {"state": "pending", "reviewer": "human_reviewer"},
                "publish_jsonb": {},
                "metrics_jsonb": {},
                "scheduled_at": (now + timedelta(hours=idx + 1)).isoformat(),
                "created_by": SEED_MARKER,
                "created_at": (now - timedelta(hours=idx + 1)).isoformat(),
                "updated_at": (now - timedelta(minutes=idx * 25 + 5)).isoformat(),
            }
        )

    published_specs = [
        ("后悔没早知道：日本经营管理签证预算拆解", 18234, 663, 412, 156, 81),
        ("日本永住被拒后，我建议先补这3件事", 14652, 511, 298, 132, 49),
        ("高度人才积分不够，替代路线怎么走", 12680, 433, 255, 118, 41),
    ]
    for idx, (title, imp, likes, collects, comments, shares) in enumerate(published_specs):
        published_at = now - timedelta(days=1, hours=idx + 2)
        pipeline_rows.append(
            {
                "domain_id": domain_id,
                "account_id": account_id,
                "channel": "xiaohongshu",
                "content_type": "post",
                "status": "published",
                "stage": "published",
                "intent_jsonb": {"topic": title, "goal": "exposure", "demo_marker": SEED_MARKER},
                "payload_jsonb": {
                    "title": title,
                    "body": "这是用于闭环演示的已发布样稿，展示真实结构化写法。",
                    "topic": title,
                    "channel_account_id": account_id,
                    "analysis_jsonb": {"angle": "对比+复盘", "source": "KB+FB"},
                    "demo_tag": SEED_MARKER,
                },
                "review_jsonb": {
                    "approved_at": (published_at - timedelta(hours=1)).isoformat(),
                    "reviewer": "human_reviewer",
                },
                "publish_jsonb": {
                    "identity": {
                        "feed_id": f"demo_feed_{idx + 1}",
                        "published_url": f"https://www.xiaohongshu.com/discovery/item/demo_{idx + 1}",
                    },
                    "feedback_state": "collecting",
                    "next_feedback_at": (now + timedelta(hours=2 + idx)).isoformat(),
                    "last_feedback_at": (now - timedelta(hours=3 + idx)).isoformat(),
                    "feedback_schedule_hours": [1, 3, 24],
                    "feedback_completed_hours": [1],
                    "feedback_analysis": {
                        "metrics_mode": "manual_import",
                        "attribution": {"primary_bottleneck": "hook_strength"},
                        "ee": {"mode": "stable"},
                        "ces": {"score": round(0.62 + idx * 0.05, 3)},
                    },
                },
                "metrics_jsonb": {
                    "post_metrics": {
                        "impressions": imp,
                        "likes": likes,
                        "collects": collects,
                        "comments_count": comments,
                        "shares": shares,
                    }
                },
                "published_at": published_at.isoformat(),
                "created_by": SEED_MARKER,
                "created_at": (published_at - timedelta(hours=2)).isoformat(),
                "updated_at": (now - timedelta(hours=idx + 1)).isoformat(),
            }
        )

    rejected_at = now - timedelta(days=1, hours=5)
    pipeline_rows.append(
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "channel": "xiaohongshu",
            "content_type": "post",
            "status": "review_rejected",
            "stage": "pending_review",
            "intent_jsonb": {"topic": "失败样本：标题过长导致审核不通过", "goal": "exposure", "demo_marker": SEED_MARKER},
            "payload_jsonb": {
                "title": "失败样本：标题过长导致审核不通过",
                "body": "用于展示昨日失败原因统计。",
                "topic": "失败样本：标题过长导致审核不通过",
                "channel_account_id": account_id,
                "demo_tag": SEED_MARKER,
            },
            "review_jsonb": {
                "rejected_at": rejected_at.isoformat(),
                "rejection_reason": "标题过长，首句信息密度不足",
                "reviewer": "human_reviewer",
            },
            "publish_jsonb": {},
            "metrics_jsonb": {},
            "created_by": SEED_MARKER,
            "created_at": (rejected_at - timedelta(hours=2)).isoformat(),
            "updated_at": rejected_at.isoformat(),
        }
    )

    pipeline_inserted = safe_insert(client, "pipeline_tasks", pipeline_rows)
    published_task_ids = [str(row.get("id")) for row in pipeline_inserted if str(row.get("status") or "") == "published" and row.get("id")]
    first_topic_id = topic_id_map.get("demo:topic:3", "")

    review_rows = []
    for idx, task_id in enumerate(published_task_ids[:2]):
        review_rows.append(
            {
                "domain_id": domain_id,
                "account_id": account_id,
                "topic_id": first_topic_id or None,
                "pipeline_task_id": task_id,
                "content_item_ref": f"demo_feed_{idx + 1}",
                "platform": "xiaohongshu",
                "metrics": {"impressions": 13000 + idx * 2200, "engagement_rate": 0.058 + idx * 0.004},
                "success_points": "开头先结论，结构清晰，互动问题抛得早。",
                "failure_points": "中段案例细节略少，可信度可继续补强。",
                "improvement": "下轮增加真实时间线和预算分层。",
                "summary_text": "该内容达到曝光预期，可继续复用同结构。",
                "source_type": "demo_review",
                "source_ref": f"demo:review:{idx + 1}",
                "created_by": SEED_MARKER,
            }
        )
    safe_insert(client, "reviews", review_rows)

    pending_memory_rows = [
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "type": "strategy_rule",
            "title": "发布时间调整建议",
            "content": "晚间 20:30 的曝光稳定高于白天，建议固定为默认发布时间。",
            "tags": ["daily_ops", "rebuild_strategy"],
            "status": "pending",
            "confidence": 0.84,
            "created_by": SEED_MARKER,
        },
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "type": "asset",
            "title": "正文开头模板建议",
            "content": "开头 2 句必须给结论 + 成本边界，再进入步骤。",
            "tags": ["chief_evolution", "draft_writer"],
            "status": "pending",
            "confidence": 0.82,
            "created_by": SEED_MARKER,
        },
        {
            "domain_id": domain_id,
            "account_id": account_id,
            "type": "strategy_rule",
            "title": "审核驳回回写规则",
            "content": "被驳回内容必须写清驳回原因并进入次日优化约束。",
            "tags": ["daily_ops", "rebuild_copy"],
            "status": "pending",
            "confidence": 0.78,
            "created_by": SEED_MARKER,
        },
    ]
    safe_insert(client, "memory_items", pending_memory_rows)

    if table_ready(client, "prompt_versions"):
        safe_insert(
            client,
            "prompt_versions",
            [
                {
                    "domain_id": domain_id,
                    "account_id": account_id,
                    "agent_name": "draft_writer",
                    "version": "demo-v1",
                    "system_prompt": "你是小红书移民赛道文案专家，先结论后步骤，禁止空泛表达。",
                    "status": "active",
                    "source": "system_seed",
                    "reason": f"{SEED_MARKER}: baseline",
                    "created_by": SEED_MARKER,
                },
                {
                    "domain_id": domain_id,
                    "account_id": account_id,
                    "agent_name": "draft_writer",
                    "version": "demo-v2",
                    "system_prompt": "强化预算分层表达，结尾增加私信关键词 CTA。",
                    "status": "draft",
                    "source": "reflection",
                    "reason": f"{SEED_MARKER}: pending upgrade",
                    "created_by": SEED_MARKER,
                },
            ],
        )

    if table_ready(client, "daily_ops_reports"):
        safe_insert(
            client,
            "daily_ops_reports",
            [
                {
                    "domain_slug": DOMAIN_SLUG,
                    "run_key": f"{RUN_KEY_PREFIX}morning",
                    "attempt": 1,
                    "triggered_by": SEED_MARKER,
                    "status": "success",
                    "started_at": (now - timedelta(hours=10)).isoformat(),
                    "finished_at": (now - timedelta(hours=9, minutes=40)).isoformat(),
                    "result_jsonb": {"steps": ["collect", "draft", "review", "retro"], "demo_marker": SEED_MARKER},
                },
                {
                    "domain_slug": DOMAIN_SLUG,
                    "run_key": f"{RUN_KEY_PREFIX}evening",
                    "attempt": 1,
                    "triggered_by": SEED_MARKER,
                    "status": "running",
                    "started_at": (now - timedelta(minutes=35)).isoformat(),
                    "finished_at": None,
                    "result_jsonb": {"steps": ["collect", "topic"], "demo_marker": SEED_MARKER},
                },
            ],
        )

    if table_ready(client, "runs_daily"):
        run_rows = safe_insert(
            client,
            "runs_daily",
            [
                {
                    "run_key": f"{RUN_KEY_PREFIX}{now.strftime('%Y%m%d')}",
                    "domain_id": domain_id,
                    "account_id": account_id,
                    "flow": "full",
                    "run_date": str((now + timedelta(hours=8)).date()),
                    "status": "RETRO_DONE",
                    "target_posts_min": 1,
                    "evidence_pack": {"intel_count": len(intel_rows), "demo_marker": SEED_MARKER},
                    "llm_plan": {"goal": "exposure_first", "actions": 5},
                    "retro_report": "今日闭环完成：采集->选题->草稿->审核->复盘。",
                    "result_jsonb": {"published": len(published_task_ids), "pending_review": len(pending_titles)},
                    "started_at": (now - timedelta(hours=12)).isoformat(),
                    "finished_at": (now - timedelta(hours=11)).isoformat(),
                }
            ],
        )
        if run_rows and table_ready(client, "decisions"):
            run_id = run_rows[0].get("id")
            if run_id and published_task_ids:
                safe_insert(
                    client,
                    "decisions",
                    [
                        {
                            "run_id": run_id,
                            "pipeline_task_id": published_task_ids[0],
                            "evidence_pack": {"source": "demo", "demo_marker": SEED_MARKER},
                            "llm_output": {"decision": "keep_structure", "confidence": 0.81},
                            "summary_text": "保留三段式结构，强化预算分层表达。",
                        }
                    ],
                )

    if table_ready(client, "ingestion_logs"):
        safe_insert(
            client,
            "ingestion_logs",
            [
                {
                    "domain_id": domain_id,
                    "account_id": account_id,
                    "source": SEED_MARKER,
                    "source_run_id": f"{RUN_KEY_PREFIX}{now.strftime('%Y%m%d')}",
                    "entity_type": "case",
                    "status": "success",
                    "received_count": 30,
                    "success_count": 26,
                    "failed_count": 4,
                    "payload_sample": {"note": "demo ingestion log"},
                    "error_sample": {},
                    "created_by": SEED_MARKER,
                }
            ],
        )

    def count(table: str, domain_scoped: bool = True, marker_only: bool = False) -> int:
        if not table_ready(client, table):
            return 0
        query = client.table(table).select("id", count="exact")
        if domain_scoped:
            if table == "daily_ops_reports":
                query = query.eq("domain_slug", DOMAIN_SLUG)
            else:
                query = query.eq("domain_id", domain_id)
        if marker_only:
            if table == "intelligence_items":
                query = query.like("source_type", "demo_%")
            elif table == "daily_ops_reports":
                query = query.like("run_key", f"{RUN_KEY_PREFIX}%")
            else:
                query = query.eq("created_by", SEED_MARKER)
        res = query.execute()
        return int(getattr(res, "count", 0) or 0)

    summary = {
        "domain_slug": DOMAIN_SLUG,
        "domain_id": domain_id,
        "account_id": account_id,
        "seed_marker": SEED_MARKER,
        "counts": {
            "intelligence_items_demo": count("intelligence_items", marker_only=True),
            "pipeline_tasks_demo": count("pipeline_tasks", marker_only=True),
            "topics_demo": count("topics", marker_only=True),
            "reviews_demo": count("reviews", marker_only=True),
            "memory_items_pending_demo": (
                client.table("memory_items")
                .select("id", count="exact")
                .eq("domain_id", domain_id)
                .eq("status", "pending")
                .execute()
                .count
                or 0
            )
            if table_ready(client, "memory_items")
            else 0,
            "daily_ops_reports_demo": count("daily_ops_reports", domain_scoped=True, marker_only=True),
        },
        "dashboard_hint": {
            "url": "http://127.0.0.1:3000/",
            "expectation": [
                "左侧公共数据与账号数据有卡片",
                "右侧今日发布任务出现待审核队列",
                "SOP建议区出现待确认策略项",
                "底部教练对话显示昨日指标和今日5动作",
            ],
        },
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed/Reset closed-loop demo data for front-screen showcase.")
    parser.add_argument("--mode", choices=["seed", "reset"], default="seed")
    args = parser.parse_args()

    supabase_url = get_env("SUPABASE_URL")
    service_key = get_env("SUPABASE_SERVICE_ROLE_KEY")
    apply_network_overrides(supabase_url)
    client = create_client(supabase_url, service_key)

    try:
        domain = ensure_domain(client)
        account = ensure_account(client)
    except Exception as exc:  # noqa: BLE001
        hint = (
            "Failed to connect Supabase. "
            "Try toggling DEMO_SEED_DISABLE_PROXY=true/false and verify SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY."
        )
        raise RuntimeError(f"{hint} Original error: {exc}") from exc
    domain_id = str(domain["id"])
    account_id = str(account["id"])

    clear_demo_data(client, domain_id, account_id)
    if args.mode == "reset":
        print("DEMO_RESET_OK")
        print(json.dumps({"domain_slug": DOMAIN_SLUG, "domain_id": domain_id, "account_id": account_id}, ensure_ascii=False, indent=2))
        return

    summary = seed_demo_data(client, domain, account)
    print("DEMO_SEED_OK")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
