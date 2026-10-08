from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import quote_plus
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def read_env(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def build_proxy(env_map: Dict[str, str]) -> Dict[str, str] | None:
    server = env_map.get("PLAYWRIGHT_PROXY_SERVER", "").strip()
    if not server:
        return None
    proxy: Dict[str, str] = {"server": server}
    username = env_map.get("PLAYWRIGHT_PROXY_USERNAME", "").strip()
    password = env_map.get("PLAYWRIGHT_PROXY_PASSWORD", "").strip()
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy


def probe_proxy_server(proxy_server: str) -> Tuple[bool, str]:
    raw = (proxy_server or "").strip()
    if not raw:
        return True, "no_proxy"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return False, "invalid_proxy_url_format"
    host = parsed.hostname
    if parsed.port:
        port = parsed.port
    elif parsed.scheme.startswith("socks"):
        port = 1080
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80
    try:
        with socket.create_connection((host, port), timeout=5):
            return True, f"tcp_ok:{host}:{port}"
    except Exception as exc:  # noqa: BLE001
        return False, f"tcp_failed:{host}:{port}:{exc}"


def scan_page(query: str, env_map: Dict[str, str]) -> Tuple[bool, Dict[str, object]]:
    search_url = f"https://www.xiaohongshu.com/search_result?keyword={quote_plus(query)}&source=web_explore_feed"
    storage_state = env_map.get("PLAYWRIGHT_STORAGE_STATE_PATH", "").strip()
    headless = env_map.get("PLAYWRIGHT_HEADLESS", "true").strip().lower() not in {"false", "0"}
    proxy = build_proxy(env_map)
    proxy_probe_ok, proxy_probe_msg = probe_proxy_server((proxy or {}).get("server", ""))

    if proxy and not proxy_probe_ok:
        return True, {
            "query": query,
            "title": "代理连接失败",
            "url": "",
            "explore_links": 0,
            "blocked": True,
            "body_preview": "",
            "proxy_enabled": True,
            "proxy_probe": proxy_probe_msg,
            "error_type": "proxy_unreachable",
            "error": "Proxy TCP probe failed before browser launch.",
            "storage_state": storage_state or "",
        }

    browser = None
    context = None
    try:
        with sync_playwright() as p:
            launch_kwargs: Dict[str, object] = {"headless": headless}
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = p.chromium.launch(**launch_kwargs)
            context_kwargs: Dict[str, object] = {"viewport": {"width": 1366, "height": 900}}
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(5000)

            title = page.title()
            url = page.url
            body = page.locator("body").inner_text(timeout=5000)
            explore_links = page.locator("a[href*='/explore/']").count()
            risk_markers = [
                "安全限制",
                "IP存在风险",
                "验证",
                "异常",
                "风控",
                "461",
                "登录错误",
                "请先登录",
                "验证码",
            ]
            blocked = any(marker in title or marker in body for marker in risk_markers)
            payload = {
                "query": query,
                "title": title,
                "url": url,
                "explore_links": explore_links,
                "blocked": blocked,
                "body_preview": body[:300].replace("\n", " | "),
                "proxy_enabled": bool(proxy),
                "proxy_probe": proxy_probe_msg,
                "storage_state": storage_state or "",
            }
            return blocked, payload
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        err_type = "runtime_error"
        if "ERR_PROXY_CONNECTION_FAILED" in msg:
            err_type = "proxy_connection_failed"
        elif "ERR_TUNNEL_CONNECTION_FAILED" in msg:
            err_type = "proxy_tunnel_failed"
        elif "ERR_PROXY_AUTH_UNSUPPORTED" in msg:
            err_type = "proxy_auth_unsupported"
        elif "ERR_HTTP_RESPONSE_CODE_FAILURE" in msg:
            err_type = "target_http_error"
        payload = {
            "query": query,
            "title": "访问失败",
            "url": search_url,
            "explore_links": 0,
            "blocked": True,
            "body_preview": "",
            "proxy_enabled": bool(proxy),
            "proxy_probe": proxy_probe_msg,
            "storage_state": storage_state or "",
            "error_type": err_type,
            "error": msg[:500],
        }
        return True, payload
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Xiaohongshu search accessibility in current env.")
    parser.add_argument("--query", default="日本移民", help="Search keyword")
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    args = parser.parse_args()

    env_map = read_env(Path(args.env_file))
    blocked, payload = scan_page(args.query, env_map)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
