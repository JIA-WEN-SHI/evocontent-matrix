from __future__ import annotations

from datetime import datetime, timezone
import random
from typing import Any, Dict, List
from urllib.parse import quote_plus

from playwright.sync_api import Page
from supabase import Client

from app.runtime.accounts.account_runtime import account_execution_scope, mark_account_runtime, upsert_account_runtime
from app.runtime.browser.playwright_runtime import paced_wait, playwright_page_scope
from app.tools.publishing.publisher import _open_context


XHS_SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={keyword}&source=web_explore_feed"
XHS_CREATOR_URL = "https://creator.xiaohongshu.com"
XHS_CREATOR_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"
XHS_WEB_HOME_URL = "https://www.xiaohongshu.com/explore"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()



def _get_channel_account_runtime(client: Client, account_id: str) -> Dict[str, Any] | None:
    normalized = str(account_id or "").strip()
    if not normalized:
        return None
    try:
        rows = (
            client.table("channel_accounts")
            .select(
                "id,channel,account_name,account_handle,publish_selector,login_mode,storage_state_path,"
                "user_data_dir,cookies_json,login_username,login_password"
            )
            .eq("id", normalized)
            .limit(1)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        return None
    return rows[0] if rows else None



def _build_scoped_task(account: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"tool-{account.get('id')}",
        "channel_account_id": account.get("id"),
        "channel": str(account.get("channel") or "xiaohongshu").strip() or "xiaohongshu",
        "meta_jsonb": {
            "playwright_strict_account_scope": True,
            "playwright_login_mode": str(account.get("login_mode") or "").strip(),
            "playwright_storage_state_path": account.get("storage_state_path") or "",
            "playwright_user_data_dir": account.get("user_data_dir") or "",
            "playwright_session_cookies_json": account.get("cookies_json") or "",
            "playwright_login_username": account.get("login_username") or "",
            "playwright_login_password": account.get("login_password") or "",
            "expected_account_name": account.get("account_name") or "",
            "expected_account_handle": account.get("account_handle") or "",
            "verify_account_name": bool(str(account.get("account_handle") or "").strip()),
        },
    }



def _runtime_metadata(account: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "channel": account.get("channel"),
        "login_mode": account.get("login_mode"),
        "storage_state_path": account.get("storage_state_path"),
        "user_data_dir": account.get("user_data_dir"),
        "cookies_json": account.get("cookies_json"),
    }



def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()



def _build_result(
    *,
    status: str,
    tool_name: str,
    account_id: str,
    items: List[Dict[str, Any]] | None = None,
    diagnostics: List[str] | None = None,
    error: str | None = None,
    next_suggested_tool: str | None = None,
) -> Dict[str, Any]:
    rows = items or []
    return {
        "status": status,
        "tool_name": tool_name,
        "account_id": account_id,
        "scope": "account",
        "collected_count": len(rows),
        "items": rows,
        "diagnostics": diagnostics or [],
        "error": error,
        "next_suggested_tool": next_suggested_tool,
    }


def _safe_current_url(page: Page) -> str:
    try:
        return str(page.url or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _normalize_url_for_compare(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.rstrip("/").lower()


def _navigate_if_needed(
    page: Page,
    target: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 60_000,
    pause_min_ms: int = 1200,
    pause_max_ms: int = 2200,
) -> None:
    desired = str(target or "").strip()
    if not desired:
        return
    current = _safe_current_url(page)
    if _normalize_url_for_compare(current) == _normalize_url_for_compare(desired):
        paced_wait(page, 400, 900)
        return
    page.goto(desired, wait_until=wait_until, timeout=timeout)
    paced_wait(page, pause_min_ms, pause_max_ms)


def _open_url_in_current_page(page: Page, target_url: str) -> None:
    desired = str(target_url or "").strip()
    if not desired:
        return
    clicked = False
    try:
        clicked = bool(
            page.evaluate(
                """(target) => {
                  const normalize = (value) => String(value || '').trim();
                  const links = Array.from(document.querySelectorAll("a[href]"));
                  const wanted = normalize(target);
                  const candidate = links.find((node) => {
                    const href = normalize(node.getAttribute('href') || node.href || '');
                    if (!href) return false;
                    if (href === wanted) return true;
                    if (wanted.startsWith('http') && href.startsWith('/')) {
                      return (`https://www.xiaohongshu.com${href}` === wanted);
                    }
                    return false;
                  });
                  if (!candidate) return false;
                  candidate.scrollIntoView({ behavior: 'instant', block: 'center' });
                  candidate.click();
                  return true;
                }""",
                desired,
            )
        )
    except Exception:  # noqa: BLE001
        clicked = False
    if clicked:
        paced_wait(page, 1200, 2200)
        return
    _navigate_if_needed(page, desired)


def _restore_to_next_page(page: Page, origin_url: str) -> None:
    paced_wait(page, 2000, 3000)
    target = str(origin_url or "").strip()
    if not target or target.startswith("about:"):
        target = XHS_CREATOR_PUBLISH_URL
    try:
        _navigate_if_needed(page, target, pause_min_ms=1000, pause_max_ms=1800)
    except Exception:  # noqa: BLE001
        pass


def _finish_and_return(page: Page, origin_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _restore_to_next_page(page, origin_url)
    return payload


def _ensure_xhs_surface(page: Page) -> None:
    current = _safe_current_url(page)
    if "xiaohongshu.com" in current:
        return
    _navigate_if_needed(page, XHS_WEB_HOME_URL)


def _dismiss_xhs_login_modal(page: Page) -> bool:
    try:
        closed = bool(
            page.evaluate(
                """() => {
                  const dialog = Array.from(document.querySelectorAll('[role="dialog"], .login-modal, .login-container, .login-panel'))
                    .find((el) => {
                      const rect = el.getBoundingClientRect();
                      return rect.width > 200 && rect.height > 120 && rect.bottom > 0 && rect.top < window.innerHeight;
                    });
                  if (!dialog) return false;

                  const closeCandidates = Array.from(
                    dialog.querySelectorAll('button, [role="button"], .close, [class*="close"], [aria-label*="close"], [aria-label*="\\u5173\\u95ed"]')
                  );
                  const closeButton =
                    closeCandidates.find((el) => {
                      const text = (el.textContent || '').trim();
                      return /close|\\u5173\\u95ed|\\u7a0d\\u540e|\\u6682\\u4e0d/i.test(text) || String(el.className || '').toLowerCase().includes('close');
                    }) || closeCandidates[0];
                  if (!closeButton) return false;
                  closeButton.click();
                  return true;
                }"""
            )
        )
        if closed:
            paced_wait(page, 300, 700)
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        page.keyboard.press("Escape")
        paced_wait(page, 200, 500)
    except Exception:  # noqa: BLE001
        pass
    return False


def _wait_for_any_selector(page: Page, selectors: List[str], timeout: int = 7000) -> bool:
    for selector in selectors:
        try:
            page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _wait_for_results_loaded(page: Page) -> bool:
    return _wait_for_any_selector(
        page,
        [
            "a[href*='/explore/']",
            "section",
            "article",
            ".note-item",
            ".card",
        ],
        timeout=9000,
    )


def _human_like_scroll(page: Page, rounds: int = 2) -> None:
    for _ in range(max(1, rounds)):
        delta = random.randint(220, 620)
        page.mouse.wheel(0, delta)
        paced_wait(page, 420, 900)


def _search_keyword_via_page(page: Page, query: str) -> None:
    keyword = str(query or "").strip()
    if not keyword:
        return
    _ensure_xhs_surface(page)
    _dismiss_xhs_login_modal(page)
    selectors = [
        "input[placeholder*='搜索']",
        "input[placeholder*='搜']",
        "input[type='search']",
        "header input",
        "input[aria-label*='搜索']",
        "input[class*='search']",
    ]
    filled = False
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.click(timeout=2000)
            locator.fill(keyword, timeout=3000)
            locator.press("Enter", timeout=2000)
            filled = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not filled:
        _navigate_if_needed(page, XHS_SEARCH_URL.format(keyword=quote_plus(keyword)))
        _dismiss_xhs_login_modal(page)
    else:
        paced_wait(page, 1200, 2200)
    if not _wait_for_results_loaded(page):
        _navigate_if_needed(page, XHS_SEARCH_URL.format(keyword=quote_plus(keyword)))
        _dismiss_xhs_login_modal(page)
        _wait_for_results_loaded(page)

def _collect_note_cards(page: Page, limit: int, *, max_rounds: int = 6) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    stagnant_rounds = 0
    rounds = max(1, min(max_rounds, 10))
    target = max(1, min(limit, 40))
    for _ in range(rounds):
        raw = page.evaluate(
            """(maxItems) => {
              const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const rows = [];
              const seen = new Set();
              const links = Array.from(document.querySelectorAll("a[href*='/explore/']"));
              for (const link of links) {
                const hrefRaw = link.getAttribute('href') || link.href || '';
                if (!hrefRaw) continue;
                const href = hrefRaw.startsWith('http') ? hrefRaw : `https://www.xiaohongshu.com${hrefRaw}`;
                if (seen.has(href)) continue;
                const card = link.closest('section, article, li, .note-item, .card, .list-item') || link.parentElement;
                const titleNode = card?.querySelector('img[alt], h1, h2, h3, h4, h5, p, span');
                const text = normalize(card?.textContent || link.textContent || '');
                if (!text) continue;
                seen.add(href);
                rows.push({
                  title: normalize(titleNode?.getAttribute?.('alt') || titleNode?.textContent || link.textContent || '').slice(0, 140),
                  body: text.slice(0, 1000),
                  url: href,
                });
                if (rows.length >= maxItems) break;
              }
              return rows;
            }""",
            target,
        )
        if isinstance(raw, list):
            added = 0
            for item in raw:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                rows.append(
                    {
                        "title": _normalize_text(str(item.get("title") or "")) or "未命名结果",
                        "body": _normalize_text(str(item.get("body") or "")),
                        "url": url,
                        "captured_at": _now_iso(),
                    }
                )
                added += 1
                if len(rows) >= target:
                    break
            if len(rows) >= target:
                break
            if added == 0:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            if stagnant_rounds >= 2:
                break
        _human_like_scroll(page, rounds=random.randint(1, 2))
    return rows



def _has_xhs_login_gate(page: Page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                  const body = (document.body?.innerText || '').replace(/\\s+/g, ' ');
                  if (/\\u767b\\u5f55\\u63a2\\u7d22\\u66f4\\u591a\\u5185\\u5bb9|\\u626b\\u7801\\u767b\\u5f55|\\u624b\\u673a\\u53f7\\u767b\\u5f55|\\u8f93\\u5165\\u9a8c\\u8bc1\\u7801/.test(body)) return true;
                  const hasVisibleDialog = !!Array.from(document.querySelectorAll('[role="dialog"], .login-modal, .login-container')).find((el) => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 200 || rect.height < 100) return false;
                    if (rect.bottom <= 0 || rect.top >= window.innerHeight) return false;
                    const text = (el.textContent || '').replace(/\\s+/g, ' ');
                    return /\\u767b\\u5f55|\\u626b\\u7801|\\u624b\\u673a\\u53f7|\\u9a8c\\u8bc1\\u7801/.test(text);
                  });
                  const hasLeftLogin = !!Array.from(document.querySelectorAll('button, a, div')).find((el) => {
                    const text = (el.textContent || '').trim();
                    if (text !== '\\u767b\\u5f55') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.left < 360 && rect.top < window.innerHeight * 0.9;
                  });
                  return hasVisibleDialog || hasLeftLogin;
                }"""
            )
        )
    except Exception:  # noqa: BLE001
        return False


def _extract_profile_candidates(page: Page, limit: int = 5) -> List[Dict[str, str]]:
    raw = page.evaluate(
        """(maxItems) => {
          const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const rows = [];
          const seen = new Set();
          const links = Array.from(document.querySelectorAll("a[href*='/user/profile/'], a[href*='/user/']"));
          for (const link of links) {
            const hrefRaw = link.getAttribute('href') || link.href || '';
            if (!hrefRaw) continue;
            const href = hrefRaw.startsWith('http') ? hrefRaw : `https://www.xiaohongshu.com${hrefRaw}`;
            if (seen.has(href)) continue;
            const text = normalize(link.textContent || '');
            if (!text) continue;
            seen.add(href);
            rows.push({ title: text.slice(0, 140), url: href });
            if (rows.length >= maxItems) break;
          }
          return rows;
        }""",
        max(1, min(limit, 10)),
    )
    if not isinstance(raw, list):
        return []
    items: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        items.append({"title": _normalize_text(str(item.get("title") or "")) or "未命名账号", "url": url})
    return items



def _resolve_profile_url(page: Page, profile_hint: str) -> Dict[str, str] | None:
    normalized_hint = str(profile_hint or "").strip()
    if not normalized_hint:
        return None
    if normalized_hint.startswith("http://") or normalized_hint.startswith("https://"):
        return {"title": normalized_hint, "url": normalized_hint}
    _search_keyword_via_page(page, normalized_hint)
    candidates = _extract_profile_candidates(page, limit=3)
    return candidates[0] if candidates else None



def _collect_profile_note_cards(page: Page, limit: int) -> List[Dict[str, Any]]:
    return _collect_note_cards(page, limit, max_rounds=7)


def _expand_post_full_text(page: Page) -> None:
    try:
        page.evaluate(
            """() => {
              const targets = Array.from(document.querySelectorAll('button, a, span, div'))
                .filter((el) => /展开|全文|查看更多/.test((el.textContent || '').trim()));
              for (const el of targets.slice(0, 3)) {
                try { el.click(); } catch (e) {}
              }
            }"""
        )
    except Exception:  # noqa: BLE001
        pass



def _parse_url_list(payload: Any) -> List[str]:
    values = (
        payload
        if isinstance(payload, list)
        else str(payload or "").replace("\r", "\n").replace(",", "\n").split("\n")
    )
    urls: List[str] = []
    seen: set[str] = set()
    for raw in values:
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _collect_dashboard_snapshot(page: Page, limit: int = 20) -> Dict[str, Any]:
    return page.evaluate(
        """(maxMetrics) => {
          const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const text = normalize(document.body?.textContent || '').slice(0, 5000);
          const metrics = [];
          for (const node of Array.from(document.querySelectorAll('div, span, strong, td')).slice(0, 500)) {
            const t = normalize(node.textContent || '');
            if (!t || t.length > 48) continue;
            if (/[0-9]/.test(t)) metrics.push(t);
            if (metrics.length >= maxMetrics) break;
          }
          return {
            title: document.title || '创作者后台',
            body: text,
            metrics,
            url: window.location.href,
          };
        }""",
        max(1, min(limit, 40)),
    )



def _open_creator_note_manager(page: Page) -> None:
    _navigate_if_needed(page, XHS_CREATOR_URL, pause_min_ms=900, pause_max_ms=1800)
    try:
        note_link = page.get_by_text("笔记管理", exact=True).first
        note_link.click(timeout=5000)
        paced_wait(page)
        return
    except Exception:  # noqa: BLE001
        pass
    for candidate in [
        "https://creator.xiaohongshu.com/creator/note",
        "https://creator.xiaohongshu.com/note",
        "https://creator.xiaohongshu.com/creator/notes",
    ]:
        try:
            _navigate_if_needed(page, candidate, timeout=30_000, pause_min_ms=900, pause_max_ms=1600)
            return
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("note_manager_unavailable")



def search_keyword(client: Client, *, account_id: str, query: str, limit: int = 8, channel: str = "xiaohongshu") -> Dict[str, Any]:
    if not str(query or "").strip():
        return _build_result(status="error", tool_name="search_keyword", account_id=account_id, error="missing_query")
    account = _get_channel_account_runtime(client, account_id)
    if not account:
        return _build_result(status="error", tool_name="search_keyword", account_id=account_id, error="account_not_found")
    if str(channel or "").strip() != "xiaohongshu":
        return _build_result(status="error", tool_name="search_keyword", account_id=account_id, error="channel_not_supported")
    runtime_metadata = _runtime_metadata(account)
    upsert_account_runtime(account_id, runtime_metadata)
    with account_execution_scope(account_id, action="playwright.search_keyword", metadata=runtime_metadata):
        try:
            scoped_task = _build_scoped_task(account)
            with playwright_page_scope(scoped_task, context_factory=_open_context) as page:
                _search_keyword_via_page(page, str(query or "").strip())
                if _has_xhs_login_gate(page):
                    return _build_result(
                        status="error",
                        tool_name="search_keyword",
                        account_id=account_id,
                        error="login_required",
                        diagnostics=["web_search_login_required: 当前搜索页仍有登录门控，请先补齐 www.xiaohongshu.com 登录态。"],
                    )
                items = _collect_note_cards(page, limit, max_rounds=6)
                diagnostics: List[str] = []
                if not items:
                    diagnostics.append("search_empty: 未检测到搜索结果，可能需要刷新登录态或增加等待时间。")
                mark_account_runtime(account_id, current_action="playwright.search_keyword", status="idle")
                return _build_result(
                    status="ok",
                    tool_name="search_keyword",
                    account_id=account_id,
                    items=items,
                    diagnostics=diagnostics,
                    next_suggested_tool="collect_post_detail_batch" if items else "collect_profile_posts",
                )
        except Exception as exc:  # noqa: BLE001
            return _build_result(
                status="error",
                tool_name="search_keyword",
                account_id=account_id,
                error=str(exc),
                diagnostics=[str(exc)],
                next_suggested_tool="collect_profile_posts",
            )



def collect_profile_posts(client: Client, *, account_id: str, profile_hint: str, limit: int = 6) -> Dict[str, Any]:
    if not str(profile_hint or "").strip():
        return _build_result(status="error", tool_name="collect_profile_posts", account_id=account_id, error="missing_profile_hint")
    account = _get_channel_account_runtime(client, account_id)
    if not account:
        return _build_result(status="error", tool_name="collect_profile_posts", account_id=account_id, error="account_not_found")
    runtime_metadata = _runtime_metadata(account)
    upsert_account_runtime(account_id, runtime_metadata)
    with account_execution_scope(account_id, action="playwright.collect_profile_posts", metadata=runtime_metadata):
        try:
            scoped_task = _build_scoped_task(account)
            with playwright_page_scope(scoped_task, context_factory=_open_context) as page:
                origin_url = _safe_current_url(page)
                profile = _resolve_profile_url(page, profile_hint)
                if not profile:
                    fallback = search_keyword(client, account_id=account_id, query=profile_hint, limit=limit)
                    fallback["tool_name"] = "collect_profile_posts"
                    fallback["diagnostics"] = [
                        "未能定位账号主页，已尝试使用关键词搜索。",
                        *(fallback.get("diagnostics") or []),
                    ]
                    return _finish_and_return(page, origin_url, fallback)
                _open_url_in_current_page(page, profile["url"])
                _wait_for_results_loaded(page)
                if _has_xhs_login_gate(page):
                    return _finish_and_return(
                        page,
                        origin_url,
                        _build_result(
                            status="error",
                            tool_name="collect_profile_posts",
                            account_id=account_id,
                            error="login_required",
                            diagnostics=["profile_login_required: 账号主页仍有登录门控，请补齐 www.xiaohongshu.com 登录态。"],
                        ),
                    )
                items = _collect_profile_note_cards(page, limit)
                return _finish_and_return(
                    page,
                    origin_url,
                    _build_result(
                        status="ok",
                        tool_name="collect_profile_posts",
                        account_id=account_id,
                        items=items,
                        diagnostics=[f"已采集主页：{profile['url']}"],
                        next_suggested_tool="collect_post_detail_batch" if items else "search_keyword",
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            return _build_result(
                status="error",
                tool_name="collect_profile_posts",
                account_id=account_id,
                error=str(exc),
                diagnostics=[str(exc)],
                next_suggested_tool="search_keyword",
            )



def collect_post_detail(client: Client, *, account_id: str, url: str) -> Dict[str, Any]:
    if not str(url or "").strip():
        return _build_result(status="error", tool_name="collect_post_detail", account_id=account_id, error="missing_url")
    account = _get_channel_account_runtime(client, account_id)
    if not account:
        return _build_result(status="error", tool_name="collect_post_detail", account_id=account_id, error="account_not_found")
    runtime_metadata = _runtime_metadata(account)
    upsert_account_runtime(account_id, runtime_metadata)
    with account_execution_scope(account_id, action="playwright.collect_post_detail", metadata=runtime_metadata):
        try:
            scoped_task = _build_scoped_task(account)
            with playwright_page_scope(scoped_task, context_factory=_open_context) as page:
                origin_url = _safe_current_url(page)
                _open_url_in_current_page(page, str(url or "").strip())
                if _has_xhs_login_gate(page):
                    return _finish_and_return(page, origin_url, _build_result(
                        status="error", tool_name="collect_post_detail", account_id=account_id,
                        error="login_required", diagnostics=["笔记详情需要登录，请补齐登录态。"],
                    ))
                _expand_post_full_text(page)
                item = page.evaluate(
                    """() => {
                      const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
                      const titleNode =
                        document.querySelector('meta[property="og:title"]') ||
                        document.querySelector('title') ||
                        document.querySelector('h1');
                      const bodyNode =
                        document.querySelector('[class*="note-content"]') ||
                        document.querySelector('[class*="desc"]') ||
                        document.body;
                      return {
                        title: normalize(titleNode?.content || titleNode?.textContent || ''),
                        body: normalize(bodyNode?.textContent || '').slice(0, 3000),
                        url: window.location.href,
                      };
                    }"""
                )
                if not isinstance(item, dict) or not str(item.get("body") or "").strip():
                    return _finish_and_return(page, origin_url, _build_result(
                        status="error", tool_name="collect_post_detail", account_id=account_id,
                        error="empty_post_detail",
                    ))
                return _finish_and_return(
                    page,
                    origin_url,
                    _build_result(
                        status="ok",
                        tool_name="collect_post_detail",
                        account_id=account_id,
                        items=[
                            {
                                "title": _normalize_text(str(item.get("title") or "")) or "未命名笔记",
                                "body": _normalize_text(str(item.get("body") or "")),
                                "url": str(item.get("url") or url).strip(),
                                "captured_at": _now_iso(),
                            }
                        ],
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            return _build_result(
                status="error",
                tool_name="collect_post_detail",
                account_id=account_id,
                error=str(exc),
                diagnostics=[str(exc)],
            )



def collect_post_detail_batch(client: Client, *, account_id: str, urls: Any, limit: int = 5) -> Dict[str, Any]:
    account = _get_channel_account_runtime(client, account_id)
    if not account:
        return _build_result(
            status="error",
            tool_name="collect_post_detail_batch",
            account_id=account_id,
            error="account_not_found",
        )
    normalized_urls = _parse_url_list(urls)[: max(1, min(limit, 10))]
    if not normalized_urls:
        return _build_result(
            status="error",
            tool_name="collect_post_detail_batch",
            account_id=account_id,
            error="missing_urls",
        )
    runtime_metadata = _runtime_metadata(account)
    upsert_account_runtime(account_id, runtime_metadata)
    rows: List[Dict[str, Any]] = []
    diagnostics: List[str] = []
    scoped_task = _build_scoped_task(account)
    with account_execution_scope(account_id, action="playwright.collect_post_detail_batch", metadata=runtime_metadata):
        try:
            with playwright_page_scope(scoped_task, context_factory=_open_context) as page:
                origin_url = _safe_current_url(page)
                for idx, url in enumerate(normalized_urls, start=1):
                    try:
                        _open_url_in_current_page(page, str(url or "").strip())
                        if _has_xhs_login_gate(page):
                            diagnostics.append(f"第 {idx} 条详情需要登录。")
                            continue
                        _expand_post_full_text(page)
                        item = page.evaluate(
                            """() => {
                              const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
                              const titleNode =
                                document.querySelector('meta[property="og:title"]') ||
                                document.querySelector('title') ||
                                document.querySelector('h1');
                              const bodyNode =
                                document.querySelector('[class*="note-content"]') ||
                                document.querySelector('[class*="desc"]') ||
                                document.body;
                              return {
                                title: normalize(titleNode?.content || titleNode?.textContent || ''),
                                body: normalize(bodyNode?.textContent || '').slice(0, 3000),
                                url: window.location.href,
                              };
                            }"""
                        )
                        if not isinstance(item, dict) or not str(item.get("body") or "").strip():
                            diagnostics.append(f"第 {idx} 条详情无有效正文：invalid_detail_payload")
                            continue
                        rows.append(
                            {
                                "title": _normalize_text(str(item.get("title") or "")) or "未命名笔记",
                                "body": _normalize_text(str(item.get("body") or "")),
                                "url": str(item.get("url") or url).strip(),
                                "captured_at": _now_iso(),
                            }
                        )
                    except Exception as item_exc:  # noqa: BLE001
                        diagnostics.append(f"第 {idx} 条详情采集失败：{item_exc}")
                status = "ok" if rows else "error"
                return _finish_and_return(
                    page,
                    origin_url,
                    _build_result(
                        status=status,
                        tool_name="collect_post_detail_batch",
                        account_id=account_id,
                        items=rows,
                        diagnostics=diagnostics,
                        error=None if rows else "all_detail_requests_failed",
                        next_suggested_tool="collect_creator_dashboard" if rows else "collect_profile_posts",
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(str(exc))
    return _build_result(
        status="ok" if rows else "error",
        tool_name="collect_post_detail_batch",
        account_id=account_id,
        items=rows,
        diagnostics=diagnostics,
        error=None if rows else "all_detail_requests_failed",
        next_suggested_tool="collect_creator_dashboard" if rows else "collect_profile_posts",
    )



def collect_creator_dashboard(client: Client, *, account_id: str) -> Dict[str, Any]:
    account = _get_channel_account_runtime(client, account_id)
    if not account:
        return _build_result(status="error", tool_name="collect_creator_dashboard", account_id=account_id, error="account_not_found")
    runtime_metadata = _runtime_metadata(account)
    upsert_account_runtime(account_id, runtime_metadata)
    with account_execution_scope(account_id, action="playwright.collect_creator_dashboard", metadata=runtime_metadata):
        try:
            scoped_task = _build_scoped_task(account)
            with playwright_page_scope(scoped_task, context_factory=_open_context) as page:
                origin_url = _safe_current_url(page)
                _navigate_if_needed(page, XHS_CREATOR_URL, pause_min_ms=900, pause_max_ms=1800)
                if _has_xhs_login_gate(page):
                    return _finish_and_return(page, origin_url, _build_result(
                        status="error", tool_name="collect_creator_dashboard", account_id=account_id,
                        error="login_required", diagnostics=["创作者后台需要登录，请补齐登录态。"],
                    ))
                item = _collect_dashboard_snapshot(page)
                if not isinstance(item, dict) or not str(item.get("body") or "").strip():
                    return _finish_and_return(page, origin_url, _build_result(
                        status="error", tool_name="collect_creator_dashboard", account_id=account_id,
                        error="empty_dashboard_snapshot", diagnostics=["未读取到有效后台内容，请检查页面加载状态。"],
                    ))
                return _finish_and_return(
                    page,
                    origin_url,
                    _build_result(
                        status="ok",
                        tool_name="collect_creator_dashboard",
                        account_id=account_id,
                        items=[
                            {
                                "title": _normalize_text(str(item.get("title") or "")) or "创作者后台",
                                "body": _normalize_text(str(item.get("body") or "")),
                                "metrics": item.get("metrics") if isinstance(item.get("metrics"), list) else [],
                                "url": str(item.get("url") or "").strip(),
                                "captured_at": _now_iso(),
                            }
                        ],
                        next_suggested_tool="collect_recent_published_posts",
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            return _build_result(
                status="error",
                tool_name="collect_creator_dashboard",
                account_id=account_id,
                error=str(exc),
                diagnostics=[str(exc)],
            )



def collect_recent_published_posts(client: Client, *, account_id: str, limit: int = 6) -> Dict[str, Any]:
    account = _get_channel_account_runtime(client, account_id)
    if not account:
        return _build_result(status="error", tool_name="collect_recent_published_posts", account_id=account_id, error="account_not_found")
    runtime_metadata = _runtime_metadata(account)
    upsert_account_runtime(account_id, runtime_metadata)
    with account_execution_scope(account_id, action="playwright.collect_recent_published_posts", metadata=runtime_metadata):
        try:
            scoped_task = _build_scoped_task(account)
            with playwright_page_scope(scoped_task, context_factory=_open_context) as page:
                origin_url = _safe_current_url(page)
                _open_creator_note_manager(page)
                _wait_for_results_loaded(page)
                if _has_xhs_login_gate(page):
                    return _finish_and_return(page, origin_url, _build_result(
                        status="error", tool_name="collect_recent_published_posts", account_id=account_id,
                        error="login_required", diagnostics=["笔记管理页需要登录，请补齐登录态。"],
                    ))
                items = _collect_profile_note_cards(page, limit)
                diagnostics = ["已从笔记管理页读取最近发布的笔记。"]
                if not items:
                    diagnostics.append("未检测到已发布笔记，请检查登录态或笔记管理页。")
                return _finish_and_return(
                    page,
                    origin_url,
                    _build_result(
                        status="ok",
                        tool_name="collect_recent_published_posts",
                        account_id=account_id,
                        items=items,
                        diagnostics=diagnostics,
                        next_suggested_tool="collect_creator_dashboard",
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            return _build_result(
                status="error",
                tool_name="collect_recent_published_posts",
                account_id=account_id,
                error=str(exc),
                diagnostics=[str(exc)],
                next_suggested_tool="collect_creator_dashboard",
            )



def run_playwright_tool(client: Client, *, account_id: str, tool_name: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = params if isinstance(params, dict) else {}
    normalized_tool = str(tool_name or "").strip().lower()
    if normalized_tool == "search_keyword":
        return search_keyword(
            client,
            account_id=account_id,
            query=str(payload.get("query") or "").strip(),
            limit=int(payload.get("limit") or 8),
            channel=str(payload.get("channel") or "xiaohongshu").strip() or "xiaohongshu",
        )
    if normalized_tool == "collect_profile_posts":
        return collect_profile_posts(
            client,
            account_id=account_id,
            profile_hint=str(payload.get("profile_hint") or payload.get("query") or "").strip(),
            limit=int(payload.get("limit") or 6),
        )
    if normalized_tool == "collect_post_detail":
        return collect_post_detail(
            client,
            account_id=account_id,
            url=str(payload.get("url") or payload.get("detail_url") or "").strip(),
        )
    if normalized_tool == "collect_post_detail_batch":
        return collect_post_detail_batch(
            client,
            account_id=account_id,
            urls=payload.get("urls") or payload.get("detail_urls") or payload.get("url") or payload.get("detail_url") or "",
            limit=int(payload.get("limit") or 5),
        )
    if normalized_tool == "collect_creator_dashboard":
        return collect_creator_dashboard(client, account_id=account_id)
    if normalized_tool == "collect_recent_published_posts":
        return collect_recent_published_posts(client, account_id=account_id, limit=int(payload.get("limit") or 6))
    return _build_result(
        status="error",
        tool_name=normalized_tool or "unknown",
        account_id=account_id,
        error="unsupported_tool",
        diagnostics=[
            "supported tools: search_keyword, collect_profile_posts, collect_post_detail, collect_post_detail_batch, collect_creator_dashboard, collect_recent_published_posts"
        ],
    )

