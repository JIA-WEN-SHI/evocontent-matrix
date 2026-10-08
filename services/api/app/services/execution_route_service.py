from __future__ import annotations

from typing import Any, Dict, List


_SUPPORTED_METHODS = {"playwright", "mcp", "rpa"}
_LABELS = {
    "playwright": "标准自动执行",
    "mcp": "桥接执行",
    "rpa": "人工辅助执行",
}


def _label(method: str) -> str:
    return _LABELS.get(str(method or "").strip().lower(), "未配置方案")


def _normalize_method_order(raw: Any, *, limit: int = 3) -> List[str]:
    items: List[str] = []
    seen: set[str] = set()
    for token in str(raw or "").split(","):
        method = str(token or "").strip().lower()
        if not method or method not in _SUPPORTED_METHODS or method in seen:
            continue
        seen.add(method)
        items.append(method)
        if len(items) >= limit:
            break
    return items


def resolve_execution_route_view(
    *,
    account_config: Dict[str, Any] | None,
    domain_config: Dict[str, Any] | None,
    collection_mode: str,
    account_id: str,
    domain_slug: str,
) -> Dict[str, Any]:
    if collection_mode == "xhs_cli":
        return {
            "status": "ok", "account_id": account_id, "domain_slug": domain_slug,
            "primary_route": "本地小红书采集", "backup_route": "失败后核对本地文件并手动重试",
            "readonly_bridge": "不回退到旧采集接口",
            "switch_rules": ["账号页面发起采集，每轮最多三篇。", "正文和图片同时保留本地副本。", "评论失败单独记录，不影响正文保存。", "发布仍需另行审核。"],
            "user_facing_status": "已启用本地采集，不需要保持小红书标签页在前台。登录失效时需要重新登录；本轮尚未接入每日自动采集。",
        }
    if collection_mode == "browser_ui":
        return {
            "status": "ok", "account_id": account_id, "domain_slug": domain_slug,
            "primary_route": "Chrome 页面协助采集", "backup_route": "等待人工登录或助手继续操作",
            "readonly_bridge": "不启用接口补数",
            "switch_rules": ["通过已登录的 Chrome 搜索并打开公开笔记。", "只导入可见页面摘要与来源，不读取登录凭据。", "登录失效或出现验证码时交由用户处理。", "内容只生成待审核草稿，发布需另行确认。"],
            "user_facing_status": "采集需要助手在当前会话中操作 Chrome；后台不会自动接管浏览器，也不会回退到旧采集接口。",
        }
    acc = account_config if isinstance(account_config, dict) else {}
    dom = domain_config if isinstance(domain_config, dict) else {}
    orchestrator = dom.get("orchestrator_control") if isinstance(dom.get("orchestrator_control"), dict) else {}
    data_input = orchestrator.get("data_input") if isinstance(orchestrator.get("data_input"), dict) else {}
    route_cfg = acc.get("execution_router") if isinstance(acc.get("execution_router"), dict) else {}

    publish_raw = (
        route_cfg.get("publish_method_order")
        or acc.get("publish_method_order")
        or data_input.get("publish_method_order")
        or "playwright,mcp"
    )
    publish_order = _normalize_method_order(publish_raw) or ["playwright", "mcp"]

    primary_method = publish_order[0]
    backup_methods = publish_order[1:]
    backup_route = " / ".join(_label(item) for item in backup_methods) if backup_methods else "人工介入"

    if collection_mode == "playwright_only":
        readonly_bridge = "暂不启用只读补数"
    else:
        readonly_bridge = "只读补数"

    if collection_mode == "mcp_only":
        user_facing_status = "当前以低交互补数为主，必要时切回标准自动执行。"
    elif backup_methods:
        user_facing_status = "当前以标准自动执行为主，必要时允许切到备用方案。"
    else:
        user_facing_status = "当前仅启用标准自动执行，失败后需要人工介入。"

    return {
        "status": "ok",
        "account_id": account_id,
        "domain_slug": domain_slug,
        "primary_route": _label(primary_method),
        "backup_route": backup_route,
        "readonly_bridge": readonly_bridge,
        "switch_rules": [
            "默认先走标准自动执行，保证主流程稳定。",
            "公开数据补数优先走低交互只读方案，减少页面操作。",
            "主方案连续失败或遇到高交互动作时，再切备用方案。",
            "用户侧只展示执行状态，不显示底层实现细节。",
        ],
        "user_facing_status": user_facing_status,
    }
