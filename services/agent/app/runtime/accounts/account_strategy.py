from __future__ import annotations

from typing import Any, Dict, List


def _to_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _to_list(value: Any, *, max_items: int = 20, max_len: int = 120) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        result.append(text[:max_len])
        if len(result) >= max_items:
            break
    return result


def normalize_account_strategy(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    mode = _to_text(src.get("mcp_mode"), "hotspot").lower()
    if mode not in {"home", "keyword", "hotspot", "profile"}:
        mode = "hotspot"
    quality_raw = src.get("quality_gate") if isinstance(src.get("quality_gate"), dict) else {}
    emphasize_raw = _to_list(quality_raw.get("emphasize_checks"), max_items=8, max_len=40)
    allowed_checks = {
        "title_length",
        "body_length",
        "paragraph_count",
        "keyword_coverage",
        "hashtag_count",
        "cta_presence",
        "forbidden_claims",
        "encoding_clean",
    }
    emphasize_checks = [item for item in emphasize_raw if item in allowed_checks]
    quality_gate = {
        "pass_score": _to_int(quality_raw.get("pass_score"), default=72, minimum=50, maximum=95),
        "min_keyword_hits": _to_int(quality_raw.get("min_keyword_hits"), default=2, minimum=1, maximum=5),
        "hashtag_target": _to_int(quality_raw.get("hashtag_target"), default=3, minimum=1, maximum=8),
        "paragraph_min": _to_int(quality_raw.get("paragraph_min"), default=4, minimum=2, maximum=10),
        "title_min": _to_int(quality_raw.get("title_min"), default=10, minimum=6, maximum=30),
        "title_max": _to_int(quality_raw.get("title_max"), default=28, minimum=12, maximum=40),
        "body_min": _to_int(quality_raw.get("body_min"), default=140, minimum=60, maximum=800),
        "body_max": _to_int(quality_raw.get("body_max"), default=1200, minimum=300, maximum=3000),
        "emphasize_checks": emphasize_checks,
    }

    strategy = {
        "persona_name": _to_text(src.get("persona_name")),
        "ip_positioning": _to_text(src.get("ip_positioning")),
        "tone_style": _to_text(src.get("tone_style"), "专业、直接、可执行"),
        "audience": _to_list(src.get("audience"), max_items=8),
        "pain_points": _to_list(src.get("pain_points"), max_items=8),
        "content_pillars": _to_list(src.get("content_pillars"), max_items=8),
        "forbidden_claims": _to_list(src.get("forbidden_claims"), max_items=12),
        "focus_keywords": _to_list(src.get("focus_keywords"), max_items=20),
        "hotspot_queries": _to_list(src.get("hotspot_queries"), max_items=20),
        "viewpoint_queries": _to_list(src.get("viewpoint_queries"), max_items=20),
        "mcp_mode": mode,
        "mcp_query_default": _to_text(src.get("mcp_query_default"), "日本移民"),
        "mcp_limit_default": _to_int(src.get("mcp_limit_default"), default=12, minimum=1, maximum=100),
        "include_detail_metrics": _to_bool(src.get("include_detail_metrics"), True),
        "mcp_feed_tool": _to_text(src.get("mcp_feed_tool")),
        "mcp_search_tool": _to_text(src.get("mcp_search_tool")),
        "mcp_metrics_tool": _to_text(src.get("mcp_metrics_tool")),
        "mcp_profile_tool": _to_text(src.get("mcp_profile_tool")),
        "mcp_profile_id_key": _to_text(src.get("mcp_profile_id_key"), "user_id"),
        "mcp_extra_args": src.get("mcp_extra_args") if isinstance(src.get("mcp_extra_args"), dict) else {},
        "prompt_overrides": src.get("prompt_overrides") if isinstance(src.get("prompt_overrides"), dict) else {},
        "primary_goal": _to_text(src.get("primary_goal"), "线索转化"),
        "cta_style": _to_text(src.get("cta_style")),
        "quality_gate": quality_gate,
    }
    return strategy


def strategy_from_account(account: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(account, dict):
        return normalize_account_strategy({})
    config = account.get("config_jsonb") if isinstance(account.get("config_jsonb"), dict) else {}
    raw = config.get("strategy_profile") if isinstance(config.get("strategy_profile"), dict) else {}
    normalized = normalize_account_strategy(raw)
    normalized["collection_plan"] = config.get("collection_plan") if isinstance(config.get("collection_plan"), dict) else {}
    normalized["account_id"] = str(account.get("id") or "")
    normalized["account_name"] = str(account.get("account_name") or "")
    normalized["account_handle"] = str(account.get("account_handle") or "")
    return normalized


def mcp_flags_from_mode(mode: str, *, include_home: bool = True, include_search: bool = True) -> Dict[str, bool]:
    normalized_mode = (mode or "hotspot").strip().lower()
    if normalized_mode == "home":
        return {"include_home": True, "include_search": False, "include_profile": False}
    if normalized_mode == "keyword":
        return {"include_home": False, "include_search": True, "include_profile": False}
    if normalized_mode == "profile":
        return {"include_home": False, "include_search": False, "include_profile": True}
    return {"include_home": include_home, "include_search": include_search, "include_profile": False}


def merge_keywords(primary: List[str], secondary: List[str], *, max_items: int = 20) -> List[str]:
    merged: List[str] = []
    seen: set[str] = set()
    for token in [*primary, *secondary]:
        text = str(token or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(text)
        if len(merged) >= max_items:
            break
    return merged
