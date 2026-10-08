from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from supabase import Client

from app.config import get_settings

_MCP_SESSION_BY_BASE_URL: Dict[str, str] = {}
_MCP_TOOL_SCHEMA_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}
_WRITE_TOOL_TOKENS = ("publish", "create", "delete", "update", "write", "post_comment", "reply_comment", "like_feed", "favorite_feed")
_COLLECTION_TOOLS = {
    "list_feeds", "search_feeds", "get_feed_detail", "user_profile",
    "check_login_status", "xhs.search", "xhs.post.metrics",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _contains_cjk(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            return True
    return False


def _repair_mojibake_text(text: str) -> str:
    raw = str(text or "")
    if not raw or _contains_cjk(raw):
        return raw
    try:
        repaired = raw.encode("latin1").decode("utf-8")
    except UnicodeError:
        return raw
    return repaired if repaired and _contains_cjk(repaired) else raw


def _base_headers(session_id: str = "") -> Dict[str, str]:
    settings = get_settings()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    api_key = str(settings.xhs_mcp_api_key or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _parse_csv(value: str) -> List[str]:
    tokens: List[str] = []
    for raw in str(value or "").split(","):
        item = raw.strip()
        if item:
            tokens.append(item)
    return tokens


def _is_write_tool(tool_name: str) -> bool:
    lowered_tool = str(tool_name or "").strip().lower()
    return any(token in lowered_tool for token in _WRITE_TOOL_TOKENS)


def get_mcp_write_status() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "enabled": False,
        "bridge_to_playwright": bool(settings.xhs_mcp_bridge_to_playwright),
        "base_url": str(settings.xhs_mcp_base_url or "").strip(),
        "timeout_sec": settings.xhs_mcp_timeout_sec,
        "mode": "disabled",
        "allowed_tools": [],
        "reason": "delegated_publishing_disabled",
    }


def get_mcp_readonly_status() -> Dict[str, Any]:
    settings = get_settings()
    enabled = bool(settings.xhs_mcp_readonly_enabled and str(settings.xhs_mcp_base_url or "").strip())
    return {
        "status": "ok",
        "enabled": enabled,
        "bridge_to_playwright": bool(settings.xhs_mcp_bridge_to_playwright),
        "base_url": str(settings.xhs_mcp_base_url or "").strip(),
        "search_tool": settings.xhs_mcp_search_tool,
        "metrics_tool": settings.xhs_mcp_metrics_tool,
        "timeout_sec": settings.xhs_mcp_timeout_sec,
        "mode": "read_only",
    }


def _post_json(
    url: str,
    payload: Dict[str, Any],
    timeout_sec: int,
    *,
    session_id: str = "",
    return_headers: bool = False,
) -> Dict[str, Any] | Tuple[Dict[str, Any], Dict[str, str]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url=url, method="POST", headers=_base_headers(session_id=session_id), data=body)
    with urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8", errors="ignore")
        response_headers = {str(k): str(v) for k, v in resp.headers.items()}
    parsed = json.loads(raw) if raw.strip() else {}
    if return_headers:
        return parsed, response_headers
    return parsed


def _resolve_playwright_bridge_account_id(client: Client, preferred_account_id: str = "") -> str:
    preferred = str(preferred_account_id or "").strip()
    if preferred:
        try:
            row = (
                client.table("channel_accounts")
                .select("id")
                .eq("id", preferred)
                .eq("channel", "xiaohongshu")
                .eq("is_active", True)
                .limit(1)
                .execute()
            ).data or []
            if row:
                return preferred
        except Exception:  # noqa: BLE001
            pass
    try:
        rows = (
            client.table("channel_accounts")
            .select("id")
            .eq("channel", "xiaohongshu")
            .eq("is_active", True)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        return str(rows[0].get("id") or "").strip() if rows else ""
    except Exception:  # noqa: BLE001
        return ""


def _normalize_playwright_items(
    rows: Any,
    *,
    query: str,
    source_type: str,
    source_kind: str,
    tool_name: str,
    limit: int,
) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        title = _repair_mojibake_text(str(raw.get("title") or "").strip())
        body = _repair_mojibake_text(str(raw.get("body") or "").strip())
        captured_at = str(raw.get("captured_at") or "").strip() or _now_iso()
        raw_text = (title or body or "").strip()
        if not raw_text:
            continue
        dedupe = f"{url}|{raw_text[:120]}".lower()
        if dedupe in seen:
            continue
        seen.add(dedupe)
        normalized.append(
            {
                "source_type": source_type,
                "source_url": url,
                "captured_at": captured_at,
                "raw_text": raw_text[:4000],
                "meta_jsonb": {
                    "query": query,
                    "provider": "xhs_playwright_bridge",
                    "tool_name": tool_name,
                    "kind": source_kind,
                    "title": title[:500],
                    "body_preview": body[:1000],
                },
            }
        )
        if len(normalized) >= max(1, min(limit, 100)):
            break
    return normalized


def _collect_via_playwright_bridge(
    client: Client,
    *,
    account_id: str,
    query: str,
    limit: int,
    source_type: str,
    source_kind: str,
    diagnostics: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    from app.tools.collection.playwright_tools import run_playwright_tool  # local import to avoid circular import

    bounded_limit = max(1, min(limit, 100))
    search_result = run_playwright_tool(
        client,
        account_id=account_id,
        tool_name="search_keyword",
        params={"query": query, "limit": bounded_limit, "channel": "xiaohongshu"},
    )
    if str(search_result.get("status") or "") == "ok":
        return _normalize_playwright_items(
            search_result.get("items"),
            query=query,
            source_type=source_type,
            source_kind=source_kind,
            tool_name="playwright.search_keyword",
            limit=bounded_limit,
        )

    if diagnostics is not None:
        diagnostics.append(
            {
                "step": "playwright_search",
                "query": query,
                "error": search_result.get("error") or "playwright_search_failed",
            }
        )

    profile_result = run_playwright_tool(
        client,
        account_id=account_id,
        tool_name="collect_profile_posts",
        params={"profile_hint": query, "limit": bounded_limit},
    )
    if str(profile_result.get("status") or "") == "ok":
        if diagnostics is not None:
            diagnostics.append({"step": "playwright_profile_fallback", "query": query, "count": len(profile_result.get("items") or [])})
        return _normalize_playwright_items(
            profile_result.get("items"),
            query=query,
            source_type=source_type,
            source_kind=source_kind,
            tool_name="playwright.collect_profile_posts",
            limit=bounded_limit,
        )

    if diagnostics is not None:
        diagnostics.append(
            {
                "step": "playwright_profile",
                "query": query,
                "error": profile_result.get("error") or "playwright_profile_failed",
            }
        )
    return []


def _ensure_mcp_session(base_url: str, timeout_sec: int, force_refresh: bool = False) -> Tuple[str, str]:
    normalized_base = str(base_url or "").strip()
    if not normalized_base:
        return "", "missing_base_url"
    if not force_refresh:
        cached = _MCP_SESSION_BY_BASE_URL.get(normalized_base)
        if cached:
            return cached, ""

    try:
        init_payload = {
            "jsonrpc": "2.0",
            "id": f"init-{int(datetime.now(timezone.utc).timestamp())}",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "evocontent-agent", "version": "0.1.0"},
            },
        }
        init_response, headers = _post_json(
            normalized_base,
            init_payload,
            timeout_sec,
            return_headers=True,
        )
        if isinstance(init_response, dict) and isinstance(init_response.get("error"), dict):
            return "", str(init_response.get("error", {}).get("message") or "initialize_failed")
        session_id = (
            headers.get("Mcp-Session-Id")
            or headers.get("mcp-session-id")
            or headers.get("MCP-Session-Id")
            or ""
        ).strip()
        if not session_id:
            return "", "missing_mcp_session_id"

        notify_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        # Best effort only.
        try:
            _post_json(
                normalized_base,
                notify_payload,
                timeout_sec,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            pass
        _MCP_SESSION_BY_BASE_URL[normalized_base] = session_id
        return session_id, ""
    except Exception as exc:  # noqa: BLE001
        return "", str(exc)


def _try_parse_json_text(text: str) -> Any:
    if not isinstance(text, str):
        return text
    raw = text.strip()
    if not raw:
        return raw
    if (raw.startswith("{") and raw.endswith("}")) or (raw.startswith("[") and raw.endswith("]")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return text
    return text


def _unwrap_tool_result(response_json: Dict[str, Any]) -> Any:
    result = response_json.get("result", response_json)
    if isinstance(result, dict):
        if "structuredContent" in result:
            return result.get("structuredContent")
        if "content" in result and isinstance(result.get("content"), list):
            texts: List[str] = []
            for node in result.get("content") or []:
                if isinstance(node, dict) and node.get("type") == "text":
                    texts.append(str(node.get("text") or ""))
            if not texts:
                return result.get("content")
            merged = "\n".join([t for t in texts if t.strip()])
            parsed = _try_parse_json_text(merged)
            return parsed if parsed else merged
    return result


def _response_is_error(response_json: Dict[str, Any]) -> Tuple[bool, str]:
    result = response_json.get("result")
    if not isinstance(result, dict):
        return False, ""
    if result.get("isError") is True:
        content = result.get("content")
        if isinstance(content, list):
            text_nodes: List[str] = []
            for node in content:
                if isinstance(node, dict) and node.get("type") == "text":
                    text_nodes.append(str(node.get("text") or ""))
            return True, "\n".join([t for t in text_nodes if t.strip()])[:500]
        return True, str(content or "tool_call_error")
    return False, ""


def _extract_error_text(result: Dict[str, Any]) -> str:
    text = str(result.get("error") or "").strip()
    if text:
        return text
    attempts = result.get("attempts")
    if isinstance(attempts, list):
        for row in attempts:
            if not isinstance(row, dict):
                continue
            err = str(row.get("error") or "").strip()
            if err:
                return err
            detail = row.get("detail")
            if isinstance(detail, dict):
                detail_err = detail.get("error")
                if isinstance(detail_err, dict):
                    msg = str(detail_err.get("message") or "").strip()
                    if msg:
                        return msg
                msg = str(detail.get("message") or "").strip()
                if msg:
                    return msg
            elif isinstance(detail, list):
                for node in detail:
                    if not isinstance(node, dict):
                        continue
                    msg = str(node.get("error") or node.get("message") or "").strip()
                    if msg:
                        return msg
    raw = result.get("raw_response")
    if isinstance(raw, dict):
        err = raw.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or "").strip()
        result_obj = raw.get("result")
        if isinstance(result_obj, dict):
            content = result_obj.get("content")
            if isinstance(content, list):
                text_nodes: List[str] = []
                for node in content:
                    if isinstance(node, dict) and node.get("type") == "text":
                        value = str(node.get("text") or "").strip()
                        if value:
                            text_nodes.append(value)
                if text_nodes:
                    return "\n".join(text_nodes)[:800]
    return ""


def _parse_quoted_tokens(text: str) -> List[str]:
    if not text:
        return []
    return [token.strip() for token in re.findall(r'["\\\']([A-Za-z0-9_\\-]+)["\\\']', text) if token.strip()]


def _parse_unexpected_properties(error_text: str) -> List[str]:
    lowered = str(error_text or "").lower()
    if "unexpected additional properties" not in lowered:
        return []
    tokens = _parse_quoted_tokens(error_text)
    return list(dict.fromkeys(tokens))


def _parse_missing_properties(error_text: str) -> List[str]:
    lowered = str(error_text or "").lower()
    if "required" not in lowered or "propert" not in lowered:
        return []
    tokens = _parse_quoted_tokens(error_text)
    return list(dict.fromkeys(tokens))


def _args_signature(arguments: Dict[str, Any]) -> str:
    try:
        return json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return str(arguments)


def _query_value_from_args(arguments: Dict[str, Any]) -> str:
    for key in ("query", "keyword", "q", "search_query", "kw", "text"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return value
    return ""


def _limit_value_from_args(arguments: Dict[str, Any], default_limit: int = 8) -> int:
    for key in ("limit", "count", "size", "page_size", "top_k", "n"):
        if key in arguments:
            return max(1, min(100, _to_int(arguments.get(key), default_limit)))
    return max(1, min(100, default_limit))


def _sanitize_args_from_error(arguments: Dict[str, Any], error_text: str) -> Dict[str, Any] | None:
    unexpected = _parse_unexpected_properties(error_text)
    if not unexpected:
        return None
    patched = dict(arguments or {})
    removed = False
    for key in unexpected:
        if key in patched:
            patched.pop(key, None)
            removed = True
    if not removed:
        return None
    return patched


def _augment_args_from_error(arguments: Dict[str, Any], error_text: str) -> Dict[str, Any] | None:
    missing = _parse_missing_properties(error_text)
    if not missing:
        return None
    patched = dict(arguments or {})
    query_value = _query_value_from_args(patched)
    limit_value = _limit_value_from_args(patched)
    changed = False
    for key in missing:
        if key in patched:
            continue
        lowered = key.lower()
        if lowered in {"query", "keyword", "q", "search_query", "kw", "text"}:
            patched[key] = query_value
            changed = True
        elif lowered in {"limit", "count", "size", "page_size", "top_k", "n"}:
            patched[key] = limit_value
            changed = True
        elif lowered in {"read_only", "readonly", "is_readonly"}:
            patched[key] = True
            changed = True
    if not changed:
        return None
    return patched


def _dedupe_arg_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        args = row if isinstance(row, dict) else {}
        sig = _args_signature(args)
        if sig in seen:
            continue
        seen.add(sig)
        output.append(args)
    return output


def _extract_tools_from_response(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict):
        tools = result.get("tools")
        if isinstance(tools, list):
            return [item for item in tools if isinstance(item, dict)]
    tools = payload.get("tools")
    if isinstance(tools, list):
        return [item for item in tools if isinstance(item, dict)]
    return []


def _fetch_tool_input_schema(tool_name: str) -> Dict[str, Any]:
    settings = get_settings()
    base_url = str(settings.xhs_mcp_base_url or "").strip()
    timeout_sec = max(3, int(settings.xhs_mcp_timeout_sec))
    if not base_url:
        return {}

    cached_by_base = _MCP_TOOL_SCHEMA_CACHE.get(base_url) or {}
    cached_schema = cached_by_base.get(tool_name)
    if isinstance(cached_schema, dict) and cached_schema:
        return cached_schema

    def _cache(schema: Dict[str, Any]) -> Dict[str, Any]:
        by_base = _MCP_TOOL_SCHEMA_CACHE.setdefault(base_url, {})
        by_base[tool_name] = schema
        return schema

    session_id, _ = _ensure_mcp_session(base_url, timeout_sec)
    if session_id:
        try:
            rpc_payload = {
                "jsonrpc": "2.0",
                "id": f"tools-{int(datetime.now(timezone.utc).timestamp())}",
                "method": "tools/list",
                "params": {},
            }
            rpc_response = _post_json(base_url, rpc_payload, timeout_sec=timeout_sec, session_id=session_id)
            for row in _extract_tools_from_response(rpc_response):
                if str(row.get("name") or "").strip() == tool_name:
                    schema = row.get("inputSchema")
                    if isinstance(schema, dict):
                        return _cache(schema)
                    break
        except Exception:  # noqa: BLE001
            pass

    try:
        bridge_url = urljoin(base_url.rstrip("/") + "/", "tools/list")
        bridge_response = _post_json(bridge_url, {}, timeout_sec=timeout_sec)
        for row in _extract_tools_from_response(bridge_response):
            if str(row.get("name") or "").strip() == tool_name:
                schema = row.get("inputSchema")
                if isinstance(schema, dict):
                    return _cache(schema)
                break
    except Exception:  # noqa: BLE001
        pass
    return {}


def _build_search_arg_candidates(tool_name: str, query: str, limit: int) -> List[Dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    base_defaults: List[Dict[str, Any]] = [
        {"query": query},
        {"keyword": query},
        {"q": query},
        {"search_query": query},
        {"query": query, "limit": bounded_limit},
        {"keyword": query, "limit": bounded_limit},
        {"query": query, "count": bounded_limit},
        {"keyword": query, "count": bounded_limit},
        {"query": query, "read_only": True},
        {"keyword": query, "read_only": True},
    ]

    schema = _fetch_tool_input_schema(tool_name)
    schema_candidates: List[Dict[str, Any]] = []
    if isinstance(schema, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        property_keys = [str(key) for key in properties.keys()]
        required_keys = [str(key) for key in required if isinstance(key, str)]
        candidate: Dict[str, Any] = {}
        query_keys = {"query", "keyword", "q", "search_query", "kw", "text"}
        limit_keys = {"limit", "count", "size", "page_size", "top_k", "n"}
        readonly_keys = {"read_only", "readonly", "is_readonly"}

        for key in property_keys:
            lowered = key.lower()
            if lowered in query_keys:
                candidate[key] = query
            elif lowered in limit_keys:
                candidate[key] = bounded_limit
            elif lowered in readonly_keys:
                candidate[key] = True

        for key in required_keys:
            if key in candidate:
                continue
            lowered = key.lower()
            if lowered in query_keys:
                candidate[key] = query
            elif lowered in limit_keys:
                candidate[key] = bounded_limit
            elif lowered in readonly_keys:
                candidate[key] = True
            else:
                prop = properties.get(key) if isinstance(properties, dict) else {}
                prop_type = str(prop.get("type") or "").lower() if isinstance(prop, dict) else ""
                if prop_type in {"integer", "number"}:
                    candidate[key] = bounded_limit
                elif prop_type == "boolean":
                    candidate[key] = False
                else:
                    candidate[key] = query

        if candidate:
            schema_candidates.append(candidate)
            required_only = {k: candidate[k] for k in required_keys if k in candidate}
            if required_only:
                schema_candidates.append(required_only)

    return _dedupe_arg_candidates([*schema_candidates, *base_defaults])


def _build_home_arg_candidates(tool_name: str, limit: int) -> List[Dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    base_defaults: List[Dict[str, Any]] = [
        {"limit": bounded_limit, "read_only": True},
        {"count": bounded_limit, "read_only": True},
        {"limit": bounded_limit},
        {"count": bounded_limit},
        {"read_only": True},
        {},
    ]
    schema = _fetch_tool_input_schema(tool_name)
    schema_candidates: List[Dict[str, Any]] = []
    if isinstance(schema, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        property_keys = [str(key) for key in properties.keys()]
        required_keys = [str(key) for key in required if isinstance(key, str)]
        candidate: Dict[str, Any] = {}
        limit_keys = {"limit", "count", "size", "page_size", "top_k", "n"}
        readonly_keys = {"read_only", "readonly", "is_readonly"}
        for key in property_keys:
            lowered = key.lower()
            if lowered in limit_keys:
                candidate[key] = bounded_limit
            elif lowered in readonly_keys:
                candidate[key] = True
        for key in required_keys:
            if key in candidate:
                continue
            lowered = key.lower()
            if lowered in limit_keys:
                candidate[key] = bounded_limit
            elif lowered in readonly_keys:
                candidate[key] = True
            else:
                prop = properties.get(key) if isinstance(properties, dict) else {}
                prop_type = str(prop.get("type") or "").lower() if isinstance(prop, dict) else ""
                if prop_type in {"integer", "number"}:
                    candidate[key] = bounded_limit
                elif prop_type == "boolean":
                    candidate[key] = False
                else:
                    candidate[key] = ""
        if candidate:
            schema_candidates.append(candidate)
            required_only = {k: candidate[k] for k in required_keys if k in candidate}
            if required_only:
                schema_candidates.append(required_only)
    return _dedupe_arg_candidates([*schema_candidates, *base_defaults])


def _call_mcp_tool(tool_name: str, arguments: Dict[str, Any], *, allow_write: bool = False) -> Dict[str, Any]:
    lowered_tool = str(tool_name or "").strip().lower()
    if allow_write or any(token in lowered_tool for token in ("publish", "post_comment", "reply_comment")):
        return {"status": "blocked", "error": "delegated_publishing_disabled", "tool_name": tool_name}
    if lowered_tool not in _COLLECTION_TOOLS:
        return {"status": "blocked", "error": "collection_tool_not_allowed", "tool_name": tool_name}
    settings = get_settings()
    status = get_mcp_write_status() if allow_write else get_mcp_readonly_status()
    if not status["enabled"]:
        return {
            "status": "disabled",
            "error": "xhs_mcp_write_not_enabled" if allow_write else "xhs_mcp_readonly_not_enabled",
            "tool_name": tool_name,
        }

    base_url = str(settings.xhs_mcp_base_url or "").strip()
    timeout_sec = max(3, int(settings.xhs_mcp_timeout_sec))
    lowered_tool = str(tool_name or "").strip().lower()
    if not allow_write and _is_write_tool(lowered_tool):
        return {
            "status": "blocked",
            "tool_name": tool_name,
            "error": "readonly_tool_blocked",
        }
    if allow_write:
        allowed_tools = [str(x).strip().lower() for x in status.get("allowed_tools", []) if str(x).strip()]
        if allowed_tools and lowered_tool not in allowed_tools:
            return {
                "status": "blocked",
                "tool_name": tool_name,
                "error": "write_tool_not_allowed",
                "allowed_tools": allowed_tools,
            }
    attempts: List[Dict[str, Any]] = []

    # Pattern 1: JSON-RPC MCP style with session.
    session_id, session_err = _ensure_mcp_session(base_url, timeout_sec)
    if not session_id:
        attempts.append({"transport": "jsonrpc", "stage": "initialize", "error": session_err or "initialize_failed"})
    else:
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": f"{'write' if allow_write else 'readonly'}-{int(datetime.now(timezone.utc).timestamp())}",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
            response = _post_json(base_url, payload, timeout_sec=timeout_sec, session_id=session_id)
            if isinstance(response, dict) and isinstance(response.get("error"), dict):
                error_text = str(response.get("error", {}).get("message") or "tool_call_error")
                # Try once with a refreshed session.
                if "session" in error_text.lower():
                    session_id_refreshed, refresh_err = _ensure_mcp_session(base_url, timeout_sec, force_refresh=True)
                    if not session_id_refreshed:
                        attempts.append(
                            {
                                "transport": "jsonrpc",
                                "stage": "reinitialize",
                                "error": refresh_err or "reinitialize_failed",
                            }
                        )
                    else:
                        response = _post_json(base_url, payload, timeout_sec=timeout_sec, session_id=session_id_refreshed)
                        if isinstance(response, dict) and isinstance(response.get("error"), dict):
                            attempts.append(
                                {
                                    "transport": "jsonrpc",
                                    "stage": "call_after_reinitialize",
                                    "error": str(response.get("error", {}).get("message") or "tool_call_error"),
                                    "raw_response": response,
                                }
                            )
                            response = {}
                else:
                    attempts.append(
                        {
                            "transport": "jsonrpc",
                            "stage": "call",
                            "error": error_text,
                            "raw_response": response,
                        }
                    )
                    response = {}
            if not response:
                raise RuntimeError("jsonrpc_tool_call_failed")
            is_error, error_text = _response_is_error(response)
            if is_error:
                attempts.append(
                    {
                        "transport": "jsonrpc",
                        "stage": "tool_result_error",
                        "error": error_text or "tool_call_error",
                        "raw_response": response,
                    }
                )
                raise RuntimeError(error_text or "jsonrpc_tool_result_error")
            return {
                "status": "ok",
                "tool_name": tool_name,
                "transport": "jsonrpc",
                "raw_response": response,
                "result": _unwrap_tool_result(response),
            }
        except Exception as exc:  # noqa: BLE001
            attempts.append({"transport": "jsonrpc", "stage": "call", "error": str(exc)})

    # Pattern 2: MCP bridge style
    try:
        call_url = urljoin(base_url.rstrip("/") + "/", "tools/call")
        payload = {"name": tool_name, "arguments": arguments}
        response = _post_json(call_url, payload, timeout_sec=timeout_sec)
        if isinstance(response, dict) and isinstance(response.get("error"), dict):
            return {
                "status": "error",
                "tool_name": tool_name,
                "transport": "bridge",
                "error": str(response.get("error", {}).get("message") or "tool_call_error"),
                "raw_response": response,
            }
        is_error, error_text = _response_is_error(response)
        if is_error:
            return {
                "status": "error",
                "tool_name": tool_name,
                "transport": "bridge",
                "error": error_text or "tool_call_error",
                "raw_response": response,
            }
        return {
            "status": "ok",
            "tool_name": tool_name,
            "transport": "bridge",
            "raw_response": response,
            "result": _unwrap_tool_result(response),
        }
    except Exception as exc:  # noqa: BLE001
        attempts.append({"transport": "bridge", "stage": "call", "error": str(exc)})

    return {
        "status": "error",
        "tool_name": tool_name,
        "error": "mcp_call_failed",
        "attempts": attempts,
    }


def _call_mcp_tool_with_arg_fallback(
    tool_name: str,
    arg_candidates: List[Dict[str, Any]],
    *,
    allow_write: bool = False,
) -> Dict[str, Any]:
    attempts: List[Dict[str, Any]] = []
    queue: List[Dict[str, Any]] = _dedupe_arg_candidates([row for row in arg_candidates if isinstance(row, dict)])
    seen: set[str] = set()
    while queue:
        args = queue.pop(0)
        signature = _args_signature(args)
        if signature in seen:
            continue
        seen.add(signature)
        result = _call_mcp_tool(tool_name=tool_name, arguments=args, allow_write=allow_write)
        if result.get("status") == "ok":
            return result
        error_text = _extract_error_text(result)
        attempts.append(
            {
                "arguments": args,
                "status": result.get("status"),
                "error": result.get("error"),
                "error_text": error_text,
                "detail": result.get("attempts") or result.get("raw_response"),
            }
        )
        # Auto-adapt when MCP schema rejects additional/missing keys.
        sanitized = _sanitize_args_from_error(args, error_text)
        if sanitized is not None:
            queue.append(sanitized)
        augmented = _augment_args_from_error(args, error_text)
        if augmented is not None:
            queue.append(augmented)
        # Session glitches: retry same args once through the queue.
        lowered_error = str(error_text or "").lower()
        if "session with given id not found" in lowered_error or "session not found" in lowered_error:
            queue.append(args)
    return {
        "status": "error",
        "tool_name": tool_name,
        "error": "all_argument_shapes_failed",
        "attempts": attempts,
    }


def _extract_candidate_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ["items", "results", "data", "list", "notes", "feeds"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(k in payload for k in ["title", "name", "text", "content"]):
            return [payload]
    return []


def _normalize_search_items(
    payload: Any,
    *,
    query: str,
    source_type: str,
    source_kind: str,
    limit: int,
    tool_name: str,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in _extract_candidate_items(payload):
        if len(normalized) >= limit:
            break
        note_card = item.get("noteCard") if isinstance(item.get("noteCard"), dict) else {}
        user = note_card.get("user") if isinstance(note_card.get("user"), dict) else {}
        interact = note_card.get("interactInfo") if isinstance(note_card.get("interactInfo"), dict) else {}

        title = _repair_mojibake_text(
            str(
            item.get("title")
            or item.get("name")
            or note_card.get("displayTitle")
            or note_card.get("title")
            or ""
            ).strip()
        )
        snippet = _repair_mojibake_text(
            str(
            item.get("snippet")
            or item.get("summary")
            or item.get("description")
            or note_card.get("desc")
            or ""
            ).strip()
        )
        text = _repair_mojibake_text(str(item.get("text") or item.get("content") or "").strip())
        url = str(item.get("url") or item.get("link") or item.get("note_url") or "").strip()
        identity = _extract_feed_identity_from_url(url)
        feed_id = str(
            item.get("feed_id")
            or item.get("note_id")
            or item.get("id")
            or identity.get("feed_id")
            or ""
        ).strip()
        xsec_token = str(
            item.get("xsec_token")
            or item.get("xsecToken")
            or identity.get("xsec_token")
            or ""
        ).strip()
        if not url and feed_id:
            if xsec_token:
                url = (
                    f"https://www.xiaohongshu.com/explore/{feed_id}"
                    f"?xsec_token={xsec_token}&xsec_source=pc_feed"
                )
            else:
                url = f"https://www.xiaohongshu.com/explore/{feed_id}"
            identity = _extract_feed_identity_from_url(url)
            if not xsec_token:
                xsec_token = str(identity.get("xsec_token") or "").strip()

        social_hint = _repair_mojibake_text(
            " ".join(
                [
                    str(user.get("nickname") or ""),
                    str(interact.get("likedCount") or ""),
                    str(interact.get("commentCount") or ""),
                ]
            ).strip()
        )
        raw_text = " ".join([x for x in [title, snippet, text] if x]).strip()
        if not raw_text:
            raw_text = " ".join([x for x in [title, social_hint] if x]).strip()
        if not raw_text:
            continue
        raw_text = _repair_mojibake_text(raw_text)
        dedupe_key = f"{url}|{raw_text[:120]}".strip().lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(
            {
                "source_type": source_type,
                "source_url": url,
                "captured_at": _now_iso(),
                "raw_text": raw_text[:4000],
                "meta_jsonb": {
                    "query": query,
                    "provider": "xhs_mcp_readonly",
                    "tool_name": tool_name,
                    "kind": source_kind,
                    "feed_id": feed_id,
                    "xsec_token": xsec_token,
                },
            }
        )
    return normalized


def collect_xhs_search_intel_via_mcp(
    *,
    queries: List[str],
    max_per_query: int,
    collect_limit: int,
    source_type: str,
    source_kind: str,
    tool_name_override: str | None = None,
    arg_candidates_override: List[Dict[str, Any]] | None = None,
    diagnostics: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    settings = get_settings()
    tool_name = str(tool_name_override or settings.xhs_mcp_search_tool or "").strip() or settings.xhs_mcp_search_tool
    if collect_limit <= 0 or max_per_query <= 0:
        return []
    all_items: List[Dict[str, Any]] = []
    for query in queries:
        if len(all_items) >= collect_limit:
            break
        remain = collect_limit - len(all_items)
        if isinstance(arg_candidates_override, list) and arg_candidates_override:
            arg_candidates: List[Dict[str, Any]] = []
            for row in arg_candidates_override:
                base = row if isinstance(row, dict) else {}
                patched = dict(base)
                if "query" in patched:
                    patched["query"] = query
                if "keyword" in patched:
                    patched["keyword"] = query
                if "query" not in patched and "keyword" not in patched:
                    patched["query"] = query
                arg_candidates.append(patched)
            if not arg_candidates:
                arg_candidates = [{"query": query}]
        else:
            arg_candidates = _build_search_arg_candidates(tool_name, query, min(max_per_query, remain))
        response = _call_mcp_tool_with_arg_fallback(tool_name=tool_name, arg_candidates=arg_candidates)
        if response.get("status") != "ok":
            if diagnostics is not None:
                diagnostics.append(
                    {
                        "step": "search",
                        "query": query,
                        "tool_name": tool_name,
                        "error": response.get("error"),
                        "attempts": response.get("attempts", []),
                    }
                )
            continue
        items = _normalize_search_items(
            response.get("result"),
            query=query,
            source_type=source_type,
            source_kind=source_kind,
            limit=min(max_per_query, remain),
            tool_name=tool_name,
        )
        all_items.extend(items)
    return all_items[:collect_limit]
def collect_xhs_home_intel_via_mcp(
    *,
    collect_limit: int,
    source_type: str,
    source_kind: str,
    tool_name_override: str | None = None,
    arg_candidates_override: List[Dict[str, Any]] | None = None,
    diagnostics: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    settings = get_settings()
    tool_name = str(tool_name_override or settings.xhs_mcp_feed_tool or "").strip() or settings.xhs_mcp_feed_tool
    if collect_limit <= 0:
        return []
    response = _call_mcp_tool_with_arg_fallback(
        tool_name=tool_name,
        arg_candidates=(
            arg_candidates_override
            if isinstance(arg_candidates_override, list) and arg_candidates_override
            else _build_home_arg_candidates(tool_name, collect_limit)
        ),
    )
    if response.get("status") != "ok":
        if diagnostics is not None:
            diagnostics.append(
                {
                    "step": "home_feed",
                    "tool_name": tool_name,
                    "error": response.get("error"),
                    "attempts": response.get("attempts", []),
                }
            )
        return []
    return _normalize_search_items(
        response.get("result"),
        query="home_feed",
        source_type=source_type,
        source_kind=source_kind,
        limit=collect_limit,
        tool_name=tool_name,
    )


def _supports_intelligence_account_scope(client: Client) -> bool:
    try:
        client.table("intelligence_items").select("account_id").limit(1).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def _persist_intel_items(
    client: Client,
    domain_id: str,
    items: List[Dict[str, Any]],
    *,
    account_id: str = "",
) -> int:
    inserted = 0
    normalized_account_id = str(account_id or "").strip()
    scoped = _supports_intelligence_account_scope(client)
    for item in items:
        source_url = str(item.get("source_url") or "").strip()
        if source_url:
            dedupe_query = (
                client.table("intelligence_items")
                .select("id")
                .eq("domain_id", domain_id)
                .eq("source_url", source_url)
                .limit(1)
            )
            if scoped:
                if normalized_account_id:
                    dedupe_query = dedupe_query.eq("account_id", normalized_account_id)
                else:
                    dedupe_query = dedupe_query.is_("account_id", "null")
            dedupe = dedupe_query.execute()
            if dedupe.data:
                continue
        payload = {**item, "domain_id": domain_id}
        if scoped:
            payload["account_id"] = normalized_account_id or None
        created = client.table("intelligence_items").insert(payload).execute()
        if created.data:
            inserted += 1
    return inserted


def _enrich_items_with_metrics(items: List[Dict[str, Any]], *, detail_limit: int) -> Dict[str, int]:
    if detail_limit <= 0:
        return {"attempted": 0, "success": 0, "failed": 0}
    attempted = 0
    success = 0
    failed = 0
    for item in items[:detail_limit]:
        if not isinstance(item, dict):
            continue
        meta = item.get("meta_jsonb") if isinstance(item.get("meta_jsonb"), dict) else {}
        source_url = str(item.get("source_url") or "").strip()
        feed_id = str(meta.get("feed_id") or "").strip()
        xsec_token = str(meta.get("xsec_token") or "").strip()
        if not source_url and not feed_id:
            continue
        attempted += 1
        result = fetch_post_metrics_via_mcp(
            published_url=source_url,
            remote_post_id=feed_id,
            feed_id=feed_id,
            xsec_token=xsec_token,
            channel="xiaohongshu",
        )
        if result.get("status") == "ok":
            success += 1
            metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
            detail_text = _extract_detail_text(result.get("raw_result"))
            merged_meta = {**meta, "post_metrics": metrics}
            if detail_text:
                merged_meta["post_detail_text"] = detail_text
            item["meta_jsonb"] = merged_meta
            if detail_text:
                base_text = _repair_mojibake_text(str(item.get("raw_text") or "").strip())
                if detail_text not in base_text:
                    item["raw_text"] = f"{base_text} {detail_text}".strip()[:4000]
        else:
            failed += 1
            item["meta_jsonb"] = {
                **meta,
                "post_metrics_error": result.get("reason") or result.get("error") or "metrics_fetch_failed",
            }
    return {"attempted": attempted, "success": success, "failed": failed}


def _render_profile_args(
    raw: Any,
    *,
    query: str,
    limit: int,
    id_key: str,
) -> List[Dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None

    def _replace_value(value: Any) -> Any:
        if isinstance(value, str):
            out = value.replace("{{query}}", query).replace("${query}", query)
            out = out.replace("{{limit}}", str(limit)).replace("${limit}", str(limit))
            return out
        if isinstance(value, list):
            return [_replace_value(item) for item in value]
        if isinstance(value, dict):
            return {str(k): _replace_value(v) for k, v in value.items()}
        return value

    candidates: List[Dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        patched = _replace_value(row)
        if not isinstance(patched, dict):
            continue
        if id_key not in patched and "query" not in patched and "keyword" not in patched:
            patched[id_key] = query
        if "limit" not in patched and "count" not in patched:
            patched["limit"] = limit
        candidates.append({str(k): v for k, v in patched.items()})
        if len(candidates) >= 8:
            break
    return candidates or None


def collect_intel_bundle_via_mcp(
    client: Client,
    *,
    domain_slug: str,
    query: str,
    limit: int,
    include_home: bool,
    include_search: bool,
    include_detail_metrics: bool,
    persist: bool,
    tool_profile: Dict[str, Any] | None = None,
    account_id: str = "",
) -> Dict[str, Any]:
    settings = get_settings()
    profile = tool_profile if isinstance(tool_profile, dict) else {}
    feed_tool = str(profile.get("feed_tool") or settings.xhs_mcp_feed_tool or "").strip() or settings.xhs_mcp_feed_tool
    search_tool = str(profile.get("search_tool") or settings.xhs_mcp_search_tool or "").strip() or settings.xhs_mcp_search_tool
    metrics_tool = str(profile.get("metrics_tool") or settings.xhs_mcp_metrics_tool or "").strip() or settings.xhs_mcp_metrics_tool
    profile_tool = str(profile.get("profile_tool") or "").strip()
    profile_id_key = str(profile.get("profile_id_key") or "user_id").strip() or "user_id"
    home_args = profile.get("home_args") if isinstance(profile.get("home_args"), list) else None
    search_args = profile.get("search_args") if isinstance(profile.get("search_args"), list) else None
    profile_args = profile.get("profile_args") if isinstance(profile.get("profile_args"), dict) else {}
    profile_arg_candidates = _render_profile_args(
        profile_args.get("arg_candidates"),
        query=_repair_mojibake_text(query or "").strip(),
        limit=max(1, min(limit, 100)),
        id_key=profile_id_key,
    )
    include_profile = bool(profile.get("include_profile", False)) or bool(profile_tool)
    status = get_mcp_readonly_status()
    if not status.get("enabled"):
        if not bool(settings.xhs_mcp_bridge_to_playwright):
            return {
                "status": "disabled",
                "domain_slug": domain_slug,
                "reason": "xhs_mcp_readonly_not_enabled",
                "items": [],
            }

        domain_rows = client.table("domains").select("id,slug").eq("slug", domain_slug).limit(1).execute().data or []
        if not domain_rows:
            return {"status": "error", "domain_slug": domain_slug, "reason": "domain_not_found", "items": []}
        domain_id = str(domain_rows[0].get("id"))
        bounded_limit = max(1, min(limit, 100))
        diagnostics: List[Dict[str, Any]] = []
        resolved_account_id = _resolve_playwright_bridge_account_id(client, account_id)
        if not resolved_account_id:
            return {
                "status": "error",
                "domain_slug": domain_slug,
                "reason": "playwright_bridge_account_not_found",
                "items": [],
            }
        normalized_query = _repair_mojibake_text(query or "").strip() or "日本移民"
        deduped = _collect_via_playwright_bridge(
            client,
            account_id=resolved_account_id,
            query=normalized_query,
            limit=bounded_limit,
            source_type="hotspot_xiaohongshu_playwright_bridge",
            source_kind="hotspot",
            diagnostics=diagnostics,
        )
        inserted = 0
        if persist and deduped:
            inserted = _persist_intel_items(client, domain_id, deduped, account_id=resolved_account_id)
        return {
            "status": "ok",
            "domain_slug": domain_slug,
            "query": normalized_query,
            "requested_limit": bounded_limit,
            "collected": len(deduped),
            "inserted": inserted,
            "account_id": resolved_account_id,
            "include_home": include_home,
            "include_search": include_search,
            "include_profile": include_profile,
            "include_detail_metrics": False,
            "tool_names": {
                "feed": "playwright.collect_profile_posts",
                "search": "playwright.search_keyword",
                "metrics": "",
                "profile": "playwright.collect_profile_posts",
            },
            "metrics_sync": {"attempted": 0, "success": 0, "failed": 0},
            "diagnostics": diagnostics[:20],
            "bridge": "playwright",
            "items": deduped,
        }

    domain_rows = client.table("domains").select("id,slug").eq("slug", domain_slug).limit(1).execute().data or []
    if not domain_rows:
        return {"status": "error", "domain_slug": domain_slug, "reason": "domain_not_found", "items": []}
    domain_id = str(domain_rows[0].get("id"))

    bounded_limit = max(1, min(limit, 100))
    all_items: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    remaining = bounded_limit

    if include_home and remaining > 0:
        home_cap = max(1, min(int(settings.daily_xhs_home_limit), remaining))
        home_items = collect_xhs_home_intel_via_mcp(
            collect_limit=home_cap,
            source_type="hotspot_xiaohongshu_mcp_feed",
            source_kind="hotspot",
            tool_name_override=feed_tool,
            arg_candidates_override=home_args,
            diagnostics=diagnostics,
        )
        all_items.extend(home_items)
        remaining = max(0, bounded_limit - len(all_items))

    if include_search and remaining > 0:
        normalized_query = _repair_mojibake_text(query or "").strip() or "日本移民"
        before_diag = len(diagnostics)
        search_items = collect_xhs_search_intel_via_mcp(
            queries=[normalized_query],
            max_per_query=remaining,
            collect_limit=remaining,
            source_type="hotspot_xiaohongshu_mcp_search",
            source_kind="hotspot",
            tool_name_override=search_tool,
            arg_candidates_override=search_args,
            diagnostics=diagnostics,
        )
        all_items.extend(search_items)
        remaining = max(0, bounded_limit - len(all_items))
        search_failed = len(search_items) == 0 and any(
            isinstance(row, dict)
            and row.get("step") == "search"
            and str(row.get("query") or "").strip() == normalized_query
            for row in diagnostics[before_diag:]
        )
        if search_failed and remaining > 0:
            fallback_items = collect_xhs_home_intel_via_mcp(
                collect_limit=min(remaining, max(1, int(settings.daily_xhs_home_limit))),
                source_type="hotspot_xiaohongshu_mcp_search_fallback",
                source_kind="hotspot",
                tool_name_override=feed_tool,
                arg_candidates_override=home_args,
                diagnostics=diagnostics,
            )
            if fallback_items:
                diagnostics.append(
                    {
                        "step": "search_fallback_home_feed",
                        "query": normalized_query,
                        "from_tool": search_tool,
                        "to_tool": feed_tool,
                        "reason": "search_failed_or_schema_mismatch",
                        "count": len(fallback_items),
                    }
                )
                all_items.extend(fallback_items)
                remaining = max(0, bounded_limit - len(all_items))

    if include_profile and remaining > 0:
        normalized_query = _repair_mojibake_text(query or "").strip() or "日本移民"
        resolved_profile_args = profile_arg_candidates or [
            {profile_id_key: normalized_query, "limit": remaining},
            {"query": normalized_query, "limit": remaining},
            {"keyword": normalized_query, "limit": remaining},
        ]
        profile_items = collect_xhs_search_intel_via_mcp(
            queries=[normalized_query],
            max_per_query=remaining,
            collect_limit=remaining,
            source_type="hotspot_xiaohongshu_mcp_profile",
            source_kind="hotspot",
            tool_name_override=profile_tool or search_tool,
            arg_candidates_override=resolved_profile_args,
            diagnostics=diagnostics,
        )
        all_items.extend(profile_items)

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in all_items:
        source_url = str(item.get("source_url") or "").strip()
        raw_text = _repair_mojibake_text(str(item.get("raw_text") or "").strip())
        key = f"{source_url}|{raw_text[:120]}".lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= bounded_limit:
            break

    metrics_sync = {"attempted": 0, "success": 0, "failed": 0}
    if include_detail_metrics:
        metrics_sync = _enrich_items_with_metrics(deduped, detail_limit=min(8, len(deduped)))

    inserted = 0
    if persist and deduped:
        inserted = _persist_intel_items(client, domain_id, deduped, account_id=account_id)

    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "query": _repair_mojibake_text(query or ""),
        "requested_limit": bounded_limit,
        "collected": len(deduped),
        "inserted": inserted,
        "account_id": account_id or None,
        "include_home": include_home,
        "include_search": include_search,
        "include_profile": include_profile,
        "include_detail_metrics": include_detail_metrics,
        "tool_names": {
            "feed": feed_tool,
            "search": search_tool,
            "metrics": metrics_tool,
            "profile": profile_tool or search_tool,
        },
        "metrics_sync": metrics_sync,
        "diagnostics": diagnostics[:20],
        "items": deduped,
    }


def custom_call_readonly_via_mcp(
    *,
    tool_name: str,
    arguments: Dict[str, Any] | None = None,
    source_kind: str = "hotspot",
    limit: int = 20,
) -> Dict[str, Any]:
    safe_tool = str(tool_name or "").strip()
    if not safe_tool:
        return {"status": "error", "reason": "tool_name_required", "items": []}
    safe_source_kind = str(source_kind or "hotspot").strip() or "hotspot"
    safe_limit = max(1, min(int(limit or 20), 100))
    args = arguments if isinstance(arguments, dict) else {}

    def _unique_arg_candidates(base: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen: set[str] = set()

        def _push(candidate: Dict[str, Any]) -> None:
            key = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            if key in seen:
                return
            seen.add(key)
            candidates.append(candidate)

        _push(base)
        if "keyword" in base and "query" not in base:
            _push({**base, "query": base.get("keyword")})
        if "query" in base and "keyword" not in base:
            _push({**base, "keyword": base.get("query")})

        noisy_keys = ("read_only", "limit", "count", "page", "page_size", "offset")
        for noisy in noisy_keys:
            if noisy in base:
                relaxed = {k: v for k, v in base.items() if k != noisy}
                _push(relaxed)
        heavily_relaxed = {k: v for k, v in base.items() if k not in noisy_keys}
        _push(heavily_relaxed)
        _push({})
        return candidates

    result = _call_mcp_tool_with_arg_fallback(
        safe_tool,
        _unique_arg_candidates(args),
    )
    if result.get("status") != "ok":
        return {
            "status": "error",
            "tool_name": safe_tool,
            "reason": result.get("error", "mcp_call_failed"),
            "attempts": result.get("attempts", []),
            "items": [],
        }

    normalized = _normalize_search_items(
        result.get("result"),
        query=str(args.get("query") or args.get("keyword") or "custom"),
        source_type=f"{safe_source_kind}_xiaohongshu_mcp_custom",
        source_kind=safe_source_kind,
        limit=safe_limit,
        tool_name=safe_tool,
    )
    return {
        "status": "ok",
        "tool_name": safe_tool,
        "count": len(normalized),
        "items": normalized,
        "raw_result": result.get("result"),
    }


def custom_call_write_via_mcp(
    *,
    tool_name: str,
    arguments: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    safe_tool = str(tool_name or "").strip()
    if not safe_tool:
        return {"status": "error", "reason": "tool_name_required"}
    args = arguments if isinstance(arguments, dict) else {}

    result = _call_mcp_tool_with_arg_fallback(
        safe_tool,
        [args],
        allow_write=True,
    )
    if result.get("status") != "ok":
        return {
            "status": "error",
            "tool_name": safe_tool,
            "reason": result.get("error", "mcp_call_failed"),
            "attempts": result.get("attempts", []),
            "allowed_tools": get_mcp_write_status().get("allowed_tools", []),
        }

    return {
        "status": "ok",
        "tool_name": safe_tool,
        "transport": result.get("transport"),
        "raw_result": result.get("result"),
    }


def publish_content_via_mcp(
    *,
    title: str,
    content: str,
    images: List[str] | None = None,
    tags: List[str] | None = None,
    schedule_at: str = "",
    video: str = "",
) -> Dict[str, Any]:
    safe_title = str(title or "").strip()
    safe_content = str(content or "").strip()
    safe_images = [str(x).strip() for x in (images or []) if str(x).strip()]
    safe_tags = [str(x).strip() for x in (tags or []) if str(x).strip()]
    safe_schedule = str(schedule_at or "").strip()
    safe_video = str(video or "").strip()

    if not safe_title:
        return {"status": "error", "reason": "title_required"}
    if not safe_content:
        return {"status": "error", "reason": "content_required"}

    if safe_video:
        args: Dict[str, Any] = {
            "title": safe_title,
            "content": safe_content,
            "video": safe_video,
        }
        if safe_tags:
            args["tags"] = safe_tags
        if safe_schedule:
            args["schedule_at"] = safe_schedule
        return custom_call_write_via_mcp(tool_name="publish_with_video", arguments=args)

    if not safe_images:
        return {"status": "error", "reason": "images_required_for_publish_content"}

    args = {
        "title": safe_title,
        "content": safe_content,
        "images": safe_images,
    }
    if safe_tags:
        args["tags"] = safe_tags
    if safe_schedule:
        args["schedule_at"] = safe_schedule
    return custom_call_write_via_mcp(tool_name="publish_content", arguments=args)


def _normalize_metrics(payload: Any) -> Dict[str, Any]:
    raw = payload
    if isinstance(payload, list) and payload:
        raw = payload[0]
    if not isinstance(raw, dict):
        return {"raw": payload}
    # Common nested variants: data, note, feed, interact_info/stat.
    for key in ["data", "note", "feed", "item"]:
        nested = raw.get(key)
        if isinstance(nested, dict):
            raw = nested
            break

    stat_candidate = None
    for key in ["stat", "stats", "interact_info", "interaction", "engagement"]:
        value = raw.get(key)
        if isinstance(value, dict):
            stat_candidate = value
            break
    if isinstance(stat_candidate, dict):
        raw = {**raw, **stat_candidate}

    aliases = {
        "views": ("views", "view_count", "impressions", "play_count"),
        "likes": ("likes", "like_count", "liked_count"),
        "collects": ("collects", "favorite_count", "collect_count", "collected_count", "bookmarks"),
        "comments_count": ("comments_count", "comment_count", "comments"),
        "shares": ("shares", "share_count", "shared_count"),
        "follows": ("followers_delta", "follows"),
    }
    metrics: Dict[str, Any] = {"raw": raw}
    for name, keys in aliases.items():
        for key in keys:
            value = raw.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number) and number.is_integer() and (number >= 0 or name == "follows"):
                metrics[name] = int(number)
                break
    return metrics


def _extract_detail_text(payload: Any) -> str:
    seen: set[str] = set()
    chunks: List[str] = []

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_l = str(key).lower()
                if key_l in {"desc", "content", "note_desc", "title", "text", "display_title"} and isinstance(value, str):
                    text = _repair_mojibake_text(value).strip()
                    if len(text) >= 4 and text not in seen:
                        seen.add(text)
                        chunks.append(text)
                elif isinstance(value, (dict, list)):
                    _walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:12]:
                _walk(item, depth + 1)

    _walk(payload, 0)
    if not chunks:
        return ""
    merged = " ".join(chunks).strip()
    return re.sub(r"\s+", " ", merged)[:4000]


def _extract_feed_identity_from_url(url_text: str) -> Dict[str, str]:
    parsed = urlparse(url_text or "")
    host = str(parsed.netloc or "").lower()
    is_xhs_host = "xiaohongshu.com" in host
    path = parsed.path or ""
    parts = [p for p in path.split("/") if p]
    feed_id = ""
    if is_xhs_host:
        for idx, part in enumerate(parts):
            if part in {"explore", "note"} and idx + 1 < len(parts):
                feed_id = parts[idx + 1]
                break
            if part == "discovery" and idx + 1 < len(parts):
                next_seg = parts[idx + 1]
                if next_seg in {"item", "note", "explore"} and idx + 2 < len(parts):
                    feed_id = parts[idx + 2]
                else:
                    feed_id = next_seg
                break
        if not feed_id and parts:
            tail = parts[-1]
            if len(tail) >= 8 and tail not in {"search_result", "website-login", "publish"}:
                feed_id = tail
    query = parse_qs(parsed.query or "")
    xsec_token = (query.get("xsec_token") or [""])[0]
    if not xsec_token:
        frag = parse_qs((parsed.fragment or "").replace("?", "&"))
        xsec_token = (frag.get("xsec_token") or [""])[0]
    return {"feed_id": feed_id.strip(), "xsec_token": xsec_token.strip()}


def fetch_post_metrics_via_mcp(
    *,
    published_url: str,
    remote_post_id: str,
    feed_id: str = "",
    xsec_token: str = "",
    channel: str = "xiaohongshu",
) -> Dict[str, Any]:
    settings = get_settings()
    tool_name = settings.xhs_mcp_metrics_tool
    identity = _extract_feed_identity_from_url(published_url)
    feed_id = str(feed_id or identity.get("feed_id") or remote_post_id or "").strip()
    xsec_token = str(xsec_token or identity.get("xsec_token") or "").strip()
    lowered_tool = str(tool_name or "").strip().lower()
    if lowered_tool == "get_feed_detail":
        if not feed_id or not xsec_token:
            return {
                "status": "skipped",
                "tool_name": tool_name,
                "reason": "get_feed_detail_requires_feed_id_and_xsec_token",
                "detail": {
                    "feed_id_present": bool(feed_id),
                    "xsec_token_present": bool(xsec_token),
                },
            }
        arg_candidates: List[Dict[str, Any]] = [
            {
                "feed_id": feed_id,
                "xsec_token": xsec_token,
            }
        ]
    else:
        arg_candidates = [
            {
                "channel": channel,
                "published_url": published_url,
                "remote_post_id": remote_post_id,
                "read_only": True,
            },
            {
                "url": published_url,
                "read_only": True,
            },
            {
                "feed_id": feed_id,
                "xsec_token": xsec_token,
            },
            {
                "note_id": feed_id,
                "xsec_token": xsec_token,
            },
        ]
    response = _call_mcp_tool_with_arg_fallback(
        tool_name=tool_name,
        arg_candidates=arg_candidates,
    )
    if response.get("status") != "ok":
        return response
    metrics = _normalize_metrics(response.get("result"))
    return {
        "status": "ok",
        "tool_name": tool_name,
        "metrics": metrics,
        "raw_result": response.get("result"),
        "transport": response.get("transport"),
    }


def _score_from_metrics(metrics: Dict[str, Any]) -> float | None:
    normalized = _normalize_metrics(metrics)
    if not {"views", "likes", "collects", "comments_count", "shares"}.issubset(normalized) or normalized["views"] <= 0:
        return None
    views = normalized["views"]
    likes = normalized["likes"]
    collects = normalized["collects"]
    comments_count = normalized["comments_count"]
    shares = normalized["shares"]
    engagement = likes + collects * 1.6 + comments_count * 1.2 + shares * 1.3
    return round(float(engagement) / float(views), 4)


def sync_pipeline_task_metrics_via_mcp(client: Client, pipeline_task: Dict[str, Any]) -> Dict[str, Any]:
    payload = pipeline_task.get("payload_jsonb") if isinstance(pipeline_task.get("payload_jsonb"), dict) else {}
    publish_jsonb = pipeline_task.get("publish_jsonb") if isinstance(pipeline_task.get("publish_jsonb"), dict) else {}
    last_result = publish_jsonb.get("last_result") if isinstance(publish_jsonb.get("last_result"), dict) else {}
    identity_jsonb = publish_jsonb.get("identity") if isinstance(publish_jsonb.get("identity"), dict) else {}
    published_url = str(identity_jsonb.get("published_url") or last_result.get("published_url") or payload.get("published_url") or "").strip()
    remote_post_id = str(identity_jsonb.get("remote_post_id") or last_result.get("remote_post_id") or payload.get("remote_post_id") or "").strip()
    feed_id = str(
        last_result.get("feed_id")
        or identity_jsonb.get("feed_id")
        or payload.get("feed_id")
        or ""
    ).strip()
    xsec_token = str(
        last_result.get("xsec_token")
        or identity_jsonb.get("xsec_token")
        or payload.get("xsec_token")
        or ""
    ).strip()
    if not published_url and not remote_post_id and not feed_id:
        return {"status": "skipped", "reason": "missing_publish_identity", "pipeline_task_id": pipeline_task.get("id")}

    result = fetch_post_metrics_via_mcp(
        published_url=published_url,
        remote_post_id=remote_post_id,
        feed_id=feed_id,
        xsec_token=xsec_token,
        channel=str(pipeline_task.get("channel") or "xiaohongshu"),
    )
    if result.get("status") == "skipped":
        return {
            "status": "skipped",
            "reason": result.get("reason", "mcp_metrics_skipped"),
            "pipeline_task_id": pipeline_task.get("id"),
            "detail": result,
        }
    if result.get("status") != "ok":
        return {
            "status": "failed",
            "reason": result.get("error", "mcp_metrics_failed"),
            "pipeline_task_id": pipeline_task.get("id"),
            "detail": result,
        }

    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    score = _score_from_metrics(metrics)
    captured_at = _now_iso()
    pipeline_task_id = str(pipeline_task.get("id") or "")
    if not pipeline_task_id:
        return {"status": "failed", "reason": "missing_pipeline_task_id"}

    try:
        client.table("task_metrics").insert(
            {
                "pipeline_task_id": pipeline_task_id,
                "metric_window": "post_publish",
                "metrics_jsonb": metrics,
                "score": score,
                "captured_at": captured_at,
            }
        ).execute()
    except Exception:  # noqa: BLE001
        # task_metrics table may be unavailable in partially migrated envs.
        pass

    current_metrics_jsonb = (
        pipeline_task.get("metrics_jsonb") if isinstance(pipeline_task.get("metrics_jsonb"), dict) else {}
    )
    merged_metrics_jsonb = {
        **current_metrics_jsonb,
        "mcp_source": "xhs_mcp_readonly",
        "mcp_tool": result.get("tool_name"),
        "mcp_transport": result.get("transport"),
        "post_metrics": metrics,
        "post_metrics_score": score,
        "post_metrics_synced_at": captured_at,
        "metrics_mode": "real",
        "observation": {
            "values": {key: value for key, value in metrics.items() if key != "raw"},
            "observed_at": captured_at,
            "provider": "xhs_mcp_readonly",
            "source_ref": f"https://www.xiaohongshu.com/explore/{feed_id or remote_post_id}",
            "provenance": {"tool_name": result.get("tool_name")},
        },
    }
    try:
        client.table("pipeline_tasks").update(
            {"metrics_jsonb": merged_metrics_jsonb, "updated_at": captured_at}
        ).eq("id", pipeline_task_id).execute()
    except Exception:  # noqa: BLE001
        return {"status": "failed", "reason": "metrics_persistence_failed", "pipeline_task_id": pipeline_task_id}

    return {
        "status": "ok",
        "pipeline_task_id": pipeline_task_id,
        "metrics": metrics,
        "score": score,
    }


def sync_recent_published_metrics_via_mcp(client: Client, domain_slug: str, limit: int = 10) -> Dict[str, Any]:
    status = get_mcp_readonly_status()
    if not status.get("enabled"):
        return {
            "status": "disabled",
            "domain_slug": domain_slug,
            "reason": "xhs_mcp_readonly_not_enabled",
        }

    domain_rows = client.table("domains").select("id,slug").eq("slug", domain_slug).limit(1).execute().data or []
    if not domain_rows:
        return {"status": "error", "domain_slug": domain_slug, "reason": "domain_not_found"}
    domain_id = str(domain_rows[0].get("id"))

    rows = (
        client.table("pipeline_tasks")
        .select("id,channel,status,payload_jsonb,publish_jsonb,metrics_jsonb,published_at,updated_at")
        .eq("domain_id", domain_id)
        .in_("status", ["published", "metrics_ready", "reflecting", "done"])
        .order("published_at", desc=True)
        .limit(max(1, min(limit, 50)))
        .execute()
    ).data or []

    synced: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in rows:
        result = sync_pipeline_task_metrics_via_mcp(client, row)
        if result.get("status") == "ok":
            synced.append({"pipeline_task_id": result.get("pipeline_task_id"), "score": result.get("score")})
        elif result.get("status") == "skipped":
            skipped.append({"pipeline_task_id": result.get("pipeline_task_id"), "reason": result.get("reason")})
        else:
            failed.append({"pipeline_task_id": result.get("pipeline_task_id"), "reason": result.get("reason")})

    return {
        "status": "ok",
        "domain_slug": domain_slug,
        "checked": len(rows),
        "synced": len(synced),
        "failed": len(failed),
        "skipped": len(skipped),
        "items": {
            "synced": synced,
            "failed": failed,
            "skipped": skipped,
        },
    }


def search_readonly_via_mcp(
    *,
    query: str,
    limit: int = 10,
    source_kind: str = "hotspot",
    client: Client | None = None,
    account_id: str = "",
) -> Dict[str, Any]:
    settings = get_settings()
    query = _repair_mojibake_text(query)
    status = get_mcp_readonly_status()
    if not status.get("enabled"):
        if bool(settings.xhs_mcp_bridge_to_playwright) and client is not None:
            resolved_account_id = _resolve_playwright_bridge_account_id(client, account_id)
            if resolved_account_id:
                bounded_limit = max(1, min(limit, 50))
                diagnostics: List[Dict[str, Any]] = []
                bridge_items = _collect_via_playwright_bridge(
                    client,
                    account_id=resolved_account_id,
                    query=query.strip() or "日本移民",
                    limit=bounded_limit,
                    source_type=f"{source_kind}_xiaohongshu_playwright_bridge",
                    source_kind=source_kind,
                    diagnostics=diagnostics,
                )
                return {
                    "status": "ok",
                    "query": query,
                    "count": len(bridge_items),
                    "tool_name": "playwright.search_keyword",
                    "items": bridge_items,
                    "bridge": "playwright",
                    "account_id": resolved_account_id,
                    "diagnostics": diagnostics[:10],
                }
        return {
            "status": "disabled",
            "query": query,
            "count": 0,
            "tool_name": settings.xhs_mcp_search_tool,
            "items": [],
            "reason": "xhs_mcp_readonly_not_enabled",
        }
    bounded_limit = max(1, min(limit, 50))
    response = _call_mcp_tool_with_arg_fallback(
        tool_name=settings.xhs_mcp_search_tool,
        arg_candidates=_build_search_arg_candidates(settings.xhs_mcp_search_tool, query, bounded_limit),
    )
    if response.get("status") != "ok":
        fallback_tool = str(settings.xhs_mcp_feed_tool or "list_feeds").strip() or "list_feeds"
        fallback_response = _call_mcp_tool_with_arg_fallback(
            tool_name=fallback_tool,
            arg_candidates=_build_home_arg_candidates(fallback_tool, bounded_limit),
        )
        if fallback_response.get("status") == "ok":
            fallback_items = _normalize_search_items(
                fallback_response.get("result"),
                query=f"{query}:home_fallback",
                source_type=f"{source_kind}_xiaohongshu_mcp_fallback",
                source_kind=source_kind,
                limit=bounded_limit,
                tool_name=fallback_tool,
            )
            return {
                "status": "ok",
                "query": query,
                "count": len(fallback_items),
                "tool_name": settings.xhs_mcp_search_tool,
                "items": fallback_items,
                "fallback_used": True,
                "fallback_tool_name": fallback_tool,
                "fallback_reason": response.get("error", "mcp_search_failed"),
                "attempts": response.get("attempts", []),
            }
        return {
            "status": "error",
            "query": query,
            "count": 0,
            "tool_name": settings.xhs_mcp_search_tool,
            "items": [],
            "reason": response.get("error", "mcp_search_failed"),
            "attempts": response.get("attempts", []),
        }
    result_items = _normalize_search_items(
        response.get("result"),
        query=query,
        source_type=f"{source_kind}_xiaohongshu_mcp",
        source_kind=source_kind,
        limit=bounded_limit,
        tool_name=settings.xhs_mcp_search_tool,
    )
    return {
        "status": "ok",
        "query": query,
        "count": len(result_items),
        "tool_name": settings.xhs_mcp_search_tool,
        "items": result_items,
    }

