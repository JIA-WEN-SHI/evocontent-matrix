from fastapi import APIRouter, Depends, Query
from supabase import Client

from app.db import get_supabase
from app.models import Actor, ExecutionRouteStatusView
from app.security import require_roles
from app.services.account_service import get_execution_route_status_view

router = APIRouter(prefix="/api/execution", tags=["execution"])


@router.get("/route-status", response_model=ExecutionRouteStatusView)
def get_execution_route_status(
    account_id: str = Query(default=""),
    domain_slug: str = Query(default="japan_immigration"),
    client: Client = Depends(get_supabase),
    _: Actor = Depends(require_roles("admin", "operator", "reviewer")),
) -> ExecutionRouteStatusView:
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        return ExecutionRouteStatusView(
            status="ok",
            account_id="",
            domain_slug=domain_slug,
            primary_route="标准自动执行",
            backup_route="备用自动执行",
            readonly_bridge="只读补数",
            switch_rules=[
                "默认先走标准自动执行，保证主流程稳定。",
                "公开数据补数优先走低交互只读方案，减少页面操作。",
                "主方案连续失败或遇到高交互动作时，再切备用方案。",
                "用户侧只展示执行状态，不显示底层实现细节。",
            ],
            user_facing_status="当前系统默认走标准自动执行，必要时允许切到备用方案。",
        )
    try:
        payload = get_execution_route_status_view(client, account_id=normalized_account_id, domain_slug=domain_slug)
        return ExecutionRouteStatusView(**payload)
    except Exception:  # noqa: BLE001
        return ExecutionRouteStatusView(
            status="degraded",
            account_id=normalized_account_id,
            domain_slug=domain_slug,
            primary_route="标准自动执行",
            backup_route="备用自动执行",
            readonly_bridge="只读补数",
            switch_rules=["当前无法读取执行策略，先按默认主方案理解。"],
            user_facing_status="当前无法读取执行策略状态。",
        )
