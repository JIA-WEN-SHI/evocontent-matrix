from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def sql_exec(url: str, headers: dict[str, str], query: str):
    last_error: Exception | None = None
    for i in range(4):
        try:
            response = requests.post(url, headers=headers, json={"query": query}, timeout=120)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if i < 3:
                continue
    raise RuntimeError(f"sql_exec failed after retries: {last_error}") from last_error


def main() -> None:
    token = require_env("SUPABASE_MANAGEMENT_TOKEN")
    project_ref = require_env("SUPABASE_PROJECT_REF")

    url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    domain_rows = sql_exec(
        url,
        headers,
        "select id from domains where slug='japan_immigration' limit 1;",
    )
    if not domain_rows:
        raise RuntimeError("Domain japan_immigration not found. Run migrations first.")
    domain_id = domain_rows[0]["id"]

    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%d%H%M%S")
    published_time = (now - timedelta(hours=30)).isoformat()
    scheduled_time = (now + timedelta(hours=2)).isoformat()
    now_iso = now.isoformat()

    insert_sql = f"""
    with intel as (
      insert into intelligence_items (domain_id, source_type, source_url, captured_at, raw_text, meta_jsonb)
      values
      ('{domain_id}', 'policy_watch', 'https://www.moj.go.jp/isa/', now(), '近期日本入管相关公开说明更新，审核材料一致性要求提高。', '{{"tag":"policy"}}'::jsonb),
      ('{domain_id}', 'competitor_watch', 'https://www.xiaohongshu.com/', now(), '高转化内容普遍采用家庭教育+资产安全双线叙事。', '{{"tag":"competitor"}}'::jsonb)
      returning id
    )
    insert into tasks (
      domain_id, channel, status, title, body, utm_code, form_id, scheduled_at, published_at, leads_count, meta_jsonb, created_at, updated_at
    ) values
      (
        '{domain_id}', 'xiaohongshu', 'queued',
        null, null, 'utm_demo_q_{run_id}', 'form_demo_{run_id}', '{scheduled_time}', null, 0,
        '{{"topic":"经营管理签预算路径","publish_selector":"button:has-text(''发布'')"}}'::jsonb,
        '{now_iso}', '{now_iso}'
      ),
      (
        '{domain_id}', 'xiaohongshu', 'pending_review',
        '后悔没早知道：35+家庭的日本规划窗口期',
        '你以为是移民，其实是家庭教育和资产风险的再配置。\\n\\n我们按PAS拆解：\\nP: 教育内卷与职场焦虑叠加。\\nA: 决策越晚，路径越窄，试错成本越高。\\nS: 用经营管理签建立中长期身份与资产配置通道。\\n\\n私信领取《最新日本经管签打分表》。',
        'utm_demo_r_{run_id}', 'form_demo_{run_id}', '{scheduled_time}', null, 0,
        '{{"topic":"PAS审批样例","publish_selector":"button:has-text(''发布'')"}}'::jsonb,
        '{now_iso}', '{now_iso}'
      ),
      (
        '{domain_id}', 'wechat_mp', 'published',
        '预算规划：日本经营管理签家庭路径',
        '这是一篇用于仪表盘验证的已发布样例内容。',
        'utm_demo_p_{run_id}', 'form_demo_{run_id}', '{scheduled_time}', '{published_time}', 3,
        '{{"topic":"dashboard样例"}}'::jsonb,
        '{now_iso}', '{now_iso}'
      );
    """

    sql_exec(url, headers, insert_sql)

    summary = sql_exec(
        url,
        headers,
        """
        select status, count(*) as c
        from tasks
        group by status
        order by status;
        """,
    )

    print("SEED_DEMO_RUNTIME=OK")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
