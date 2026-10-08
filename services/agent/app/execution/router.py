from __future__ import annotations

from typing import Any, Dict, List

from app.config import get_settings
from app.execution.rpa import publish_handoff


_SUPPORTED_PUBLISH_METHODS = {"playwright", "mcp", "rpa"}
_USER_LABELS = {
    "playwright": "标准自动执行",
    "mcp": "桥接执行",
    "rpa": "人工辅助执行",
}


def route_label(method: str) -> str:
    return _USER_LABELS.get(str(method or "").strip().lower(), "未配置方案")


def _normalize_method_list(raw: Any, *, limit: int = 3) -> List[str]:
    items: List[str] = []
    seen: set[str] = set()
    tokens = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    for token in tokens:
        method = str(token or "").strip().lower()
        if not method or method not in _SUPPORTED_PUBLISH_METHODS or method in seen:
            continue
        seen.add(method)
        items.append(method)
        if len(items) >= limit:
            break
    return items


def _resolve_readonly_bridge_label() -> str:
    settings = get_settings()
    if bool(settings.xhs_mcp_readonly_enabled):
        return "只读补数"
    if bool(settings.xhs_mcp_bridge_to_playwright):
        return "浏览器补数"
    return "暂未启用"


def resolve_publish_route(task: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    meta = task.get("meta_jsonb") if isinstance(task.get("meta_jsonb"), dict) else {}
    channel = str(task.get("channel") or "").strip().lower()
    explicit_raw = meta.get("publish_method_order") or ""
    env_raw = str(getattr(settings, "publish_method_order", "") or "").strip()
    order = _normalize_method_list(explicit_raw) or _normalize_method_list(env_raw)
    if not order:
        order = ["playwright", "mcp"]

    images = meta.get("images") or meta.get("image_paths") or meta.get("local_images")
    has_images = isinstance(images, list) and any(str(item or "").strip() for item in images)
    has_video = bool(str(meta.get("video") or meta.get("video_path") or "").strip())
    publish_tab = str(meta.get("xhs_publish_tab") or "").replace(" ", "").strip()

    if channel != "xiaohongshu":
        order = [item for item in order if item != "mcp"] or ["playwright"]
    elif publish_tab in {"写长文", "长文"} and not has_images and not has_video:
        order = [item for item in order if item != "mcp"] or ["playwright"]

    backup_methods = order[1:]
    primary_method = order[0] if order else "playwright"

    return {
        "order": order,
        "primary_method": primary_method,
        "backup_methods": backup_methods,
        "primary_label": route_label(primary_method),
        "backup_label": " / ".join(route_label(item) for item in backup_methods) if backup_methods else "人工介入",
        "readonly_bridge_label": _resolve_readonly_bridge_label(),
        "rpa_readiness": publish_handoff(task)["readiness"],
        "manual_handoff_available": True,
        "switch_rules": [
            "默认先走标准自动执行，保证主流程稳定。",
            "公开数据补数优先走低交互只读方案，减少不必要页面操作。",
            "主方案返回失败后按配置顺序尝试备用方案；备用发布契约缺失时转人工处理。",
            "用户侧只展示执行结果与是否需要人工介入，不展示底层技术细节。",
        ],
    }
