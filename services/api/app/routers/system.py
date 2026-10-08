from __future__ import annotations

from typing import Any, Dict, List
from time import monotonic
from asyncio import TimeoutError, wait_for

import httpx
from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool
from supabase import Client

from app.config import Settings, get_settings
from app.db import get_supabase, probe_database_table
from app.models import Actor
from app.security import require_roles

router = APIRouter(prefix="/api/system", tags=["system"])

CORE_TABLES = ["domains", "strategy_versions", "pipeline_tasks", "intelligence_items", "audit_logs"]
OPTIONAL_TABLES = [
    "task_metrics",
    "evolution_insights",
    "tool_profiles",
    "daily_ops_reports",
    "channel_accounts",
]
READINESS_DATABASE_BUDGET_SEC = 10.0
AGENT_HEALTH_TIMEOUT_SEC = 5.0


def _probe_tables(client: Client) -> Dict[str, dict]:
    results = {}
    unavailable = False
    deadline = monotonic() + READINESS_DATABASE_BUDGET_SEC
    # API dependencies own their client, so this cannot change another request's timeout.
    session = client.postgrest.session if isinstance(client, Client) else None
    original_timeout = session.timeout if session is not None else None
    try:
        for table in CORE_TABLES + OPTIONAL_TABLES:
            if unavailable:
                results[table] = {"status": "not_checked"}
                continue
            remaining = deadline - monotonic()
            if remaining <= 0:
                results[table] = {
                    "status": "unavailable", "code": "database_readiness_timeout",
                    "message": "Database readiness checks exceeded the time budget.",
                }
            else:
                if session is not None:
                    # Share the remaining budget across pool/connect/write/read phases.
                    session.timeout = httpx.Timeout(**{
                        phase: min(value, remaining / 4) if value is not None else remaining / 4
                        for phase, value in original_timeout.as_dict().items()
                    })
                results[table] = probe_database_table(client, table)
            # One failed connection is enough; don't multiply the timeout by table count.
            unavailable = results[table]["status"] == "unavailable"
    finally:
        if session is not None:
            session.timeout = original_timeout
    return results


def _feature_item(
    key: str,
    name: str,
    status: str,
    reason: str,
    action: str,
) -> Dict[str, str]:
    return {
        "key": key,
        "name": name,
        "status": status,
        "reason": reason,
        "action": action,
    }


@router.get("/readiness")
async def get_system_readiness(
    settings: Settings = Depends(get_settings),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    table_checks = await run_in_threadpool(_probe_tables, client)
    core_status = {table: table_checks[table]["status"] == "ready" for table in CORE_TABLES}
    optional_status = {table: table_checks[table]["status"] == "ready" for table in OPTIONAL_TABLES}
    database = next(
        (result for result in table_checks.values() if result["status"] == "unavailable"),
        {"status": "ready"},
    )
    core_ready = all(core_status.values())
    pipeline_status = table_checks["pipeline_tasks"]["status"]
    pipeline_mode = {"ready": "native", "missing": "schema_missing"}.get(pipeline_status, "unavailable")

    agent_health: Dict[str, Any] = {"reachable": False, "status": "down"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            res = await wait_for(
                http_client.get(f"{settings.agent_service_url}/health"),
                timeout=AGENT_HEALTH_TIMEOUT_SEC,
            )
            if res.status_code == 200:
                agent_health = {"reachable": True, "status": "ok"}
            else:
                agent_health = {"reachable": False, "status": f"http_{res.status_code}"}
    except (httpx.HTTPError, TimeoutError):
        agent_health = {"reachable": False, "status": "down"}

    suggestions: List[str] = []
    if database["status"] == "unavailable":
        suggestions.append("数据库连接或查询不可用，请先检查 Supabase 项目状态、地址、凭据、DNS 和代理配置；当前不能判断表是否缺失。")
    if pipeline_status == "missing":
        suggestions.append("检测到未创建 pipeline 表。建议执行 003_matrix_pipeline.sql。")
    if table_checks["daily_ops_reports"]["status"] == "missing":
        suggestions.append("检测到未创建每日运营报表表。建议执行 004_daily_ops_reports.sql。")
    if not agent_health["reachable"]:
        suggestions.append("Agent 服务不可达，请检查 AGENT_SERVICE_URL 与 agent 进程状态。")
    if any(table_checks[table]["status"] == "missing" for table in CORE_TABLES):
        suggestions.append("核心表缺失，请先执行 001_init.sql 与 002_seed.sql。")

    return {
        "status": "ok" if core_ready and database["status"] == "ready" and agent_health["reachable"] else "degraded",
        "pipeline_mode": pipeline_mode,
        "database": database,
        "table_checks": table_checks,
        "core_ready": core_ready,
        "core_tables": core_status,
        "optional_tables": optional_status,
        "agent_health": agent_health,
        "suggestions": suggestions,
    }


@router.get("/feature-checklist")
async def get_feature_checklist(
    settings: Settings = Depends(get_settings),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> Dict[str, Any]:
    readiness = await get_system_readiness(settings=settings, client=client, _=_)
    core_tables = readiness["core_tables"]
    optional_tables = readiness["optional_tables"]
    pipeline_mode = readiness["pipeline_mode"]
    agent_reachable = bool((readiness.get("agent_health") or {}).get("reachable"))

    pipeline_tasks_ready = bool(core_tables.get("pipeline_tasks"))
    domains_ready = bool(core_tables.get("domains"))
    intel_ready = bool(core_tables.get("intelligence_items"))
    strategy_ready = bool(core_tables.get("strategy_versions"))
    audit_ready = bool(core_tables.get("audit_logs"))
    report_table_ready = bool(optional_tables.get("daily_ops_reports"))
    account_table_ready = bool(optional_tables.get("channel_accounts"))

    features: List[Dict[str, str]] = []

    if pipeline_tasks_ready and domains_ready:
        features.append(
            _feature_item(
                "studio",
                "任务工作室（创建/运行）",
                "ok" if agent_reachable else "degraded",
                "可直接使用 pipeline 原生模式。" if agent_reachable else "可创建任务，但运行依赖 Agent。",
                "若运行失败，检查 Agent 服务与 AGENT_SERVICE_URL。",
            )
        )
    else:
        features.append(
            _feature_item(
                "studio",
                "任务工作室（创建/运行）",
                "blocked",
                "缺少 domains/pipeline_tasks 核心表。",
                "先执行 001_init.sql 与 002_seed.sql。",
            )
        )

    features.append(
        _feature_item(
            "approval",
            "待审中心",
            "ok" if pipeline_tasks_ready else "blocked",
            "依赖 pipeline_tasks 表进行审核流转。" if pipeline_tasks_ready else "pipeline_tasks 表不可用。",
            "若 blocked，先修复数据库基础迁移。",
        )
    )

    daily_status = "ok"
    daily_reason = "热点采集 + 生成 + 发布闭环可运行。"
    daily_action = "可在数字员工输入“执行今日运营”触发。"
    if not (pipeline_tasks_ready and domains_ready and intel_ready):
        daily_status = "blocked"
        daily_reason = "缺少 daily ops 所需核心表（domains/pipeline_tasks/intelligence_items）。"
        daily_action = "先执行 001_init.sql。"
    elif not agent_reachable:
        daily_status = "degraded"
        daily_reason = "Agent 不可达，无法稳定执行生成/发布。"
        daily_action = "检查 Agent 进程和 8100 端口。"
    features.append(_feature_item("daily_ops", "每日运营闭环", daily_status, daily_reason, daily_action))

    viewpoint_status = "ok" if daily_status == "ok" else ("degraded" if daily_status == "degraded" else "blocked")
    viewpoint_reason = (
        "观点采集 -> 观点扩展 -> 生成发布可运行。"
        if viewpoint_status == "ok"
        else ("依赖 Agent 执行观点扩展链路。" if viewpoint_status == "degraded" else "基础数据层未就绪。")
    )
    viewpoint_action = (
        "在数字员工输入“执行观点运营”。"
        if viewpoint_status == "ok"
        else ("先恢复 Agent。" if viewpoint_status == "degraded" else "先修复核心表。")
    )
    features.append(_feature_item("viewpoint_ops", "观点扩展闭环", viewpoint_status, viewpoint_reason, viewpoint_action))

    report_status = "ok" if report_table_ready else "degraded"
    report_reason = "可查询每日运营历史报表。" if report_table_ready else "报表表缺失，仅能看到实时状态。"
    report_action = "执行 004_daily_ops_reports.sql 后可查看完整历史。"
    features.append(_feature_item("daily_reports", "每日运营报表", report_status, report_reason, report_action))

    strategy_status = "ok" if (domains_ready and strategy_ready and audit_ready) else "degraded"
    strategy_reason = (
        "策略版本与反思链路可记录。"
        if strategy_status == "ok"
        else "策略或审计基础表状态异常，可能影响反思沉淀。"
    )
    strategy_action = "核对 strategy_versions/audit_logs 表，必要时重跑 001+002。"
    features.append(_feature_item("strategy", "策略与反思", strategy_status, strategy_reason, strategy_action))

    account_status = "ok" if account_table_ready else "degraded"
    account_reason = "可管理多账号登录态与发布配置。" if account_table_ready else "账号管理表缺失，页面将无法保存数据。"
    account_action = "执行 006_channel_accounts.sql 后启用账号管理。"
    features.append(_feature_item("accounts", "账号管理", account_status, account_reason, account_action))

    if readiness["database"]["status"] == "unavailable":
        for feature in features:
            feature.update(
                status="blocked",
                reason="数据库不可用，功能状态尚无法验证。",
                action="先检查 Supabase 项目状态、地址、凭据、DNS 和代理连接。",
            )

    blocked_count = len([f for f in features if f["status"] == "blocked"])
    degraded_count = len([f for f in features if f["status"] == "degraded"])

    return {
        "status": "ok" if blocked_count == 0 and degraded_count == 0 else "degraded",
        "pipeline_mode": pipeline_mode,
        "summary": {
            "total": len(features),
            "ok": len([f for f in features if f["status"] == "ok"]),
            "degraded": degraded_count,
            "blocked": blocked_count,
        },
        "features": features,
        "suggestions": readiness["suggestions"],
    }
