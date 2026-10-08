from __future__ import annotations

import random
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock, get_ident
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from app.config import get_settings


ContextFactory = Callable[[Playwright, Dict[str, Any]], Tuple[BrowserContext, Optional[Browser]]]


@dataclass
class _AccountSession:
    account_id: str
    signature: str
    driver: Playwright
    context: BrowserContext
    browser: Optional[Browser]
    page: Page
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    owner_thread_id: int = field(default_factory=get_ident)

    def touch(self) -> None:
        self.last_used_at = datetime.now(timezone.utc)

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.driver is not None:
                self.driver.stop()
        except Exception:  # noqa: BLE001
            pass


_SESSION_LOCK = Lock()
_ACCOUNT_SESSIONS: Dict[str, _AccountSession] = {}


def _task_account_id(task: Dict[str, Any]) -> str:
    meta = task.get("meta_jsonb") if isinstance(task.get("meta_jsonb"), dict) else {}
    for key in ("channel_account_id", "account_id"):
        value = str(task.get(key) or "").strip()
        if value:
            return value
    for key in ("channel_account_id", "account_id"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    task_id = str(task.get("id") or "").strip()
    if task_id.startswith("tool-"):
        return task_id.removeprefix("tool-").strip()
    session_key = str(meta.get("playwright_session_key") or "").strip()
    if session_key:
        return f"session:{session_key}"
    return ""


def _session_signature(task: Dict[str, Any]) -> str:
    meta = task.get("meta_jsonb") if isinstance(task.get("meta_jsonb"), dict) else {}
    keys = [
        str(task.get("channel") or "").strip(),
        str(meta.get("playwright_strict_account_scope") or False),
        str(meta.get("playwright_login_mode") or "").strip().lower(),
        str(meta.get("playwright_storage_state_path") or "").strip(),
        str(meta.get("playwright_user_data_dir") or "").strip(),
        str(meta.get("playwright_login_username") or "").strip(),
        str(meta.get("playwright_proxy_server") or "").strip(),
    ]
    return "|".join(keys)


def is_reuse_enabled_for_task(task: Dict[str, Any]) -> bool:
    settings = get_settings()
    if not settings.playwright_session_reuse_enabled:
        return False
    meta = task.get("meta_jsonb") if isinstance(task.get("meta_jsonb"), dict) else {}
    if bool(meta.get("playwright_force_one_shot")):
        return False
    return bool(_task_account_id(task))


def _session_idle_ttl() -> Optional[timedelta]:
    settings = get_settings()
    minutes = int(settings.playwright_session_idle_ttl_minutes)
    if minutes <= 0:
        return None
    return timedelta(minutes=minutes)


def cleanup_idle_playwright_sessions() -> None:
    ttl = _session_idle_ttl()
    if ttl is None:
        return
    cutoff = datetime.now(timezone.utc) - ttl
    stale_keys: list[str] = []
    with _SESSION_LOCK:
        for account_id, session in _ACCOUNT_SESSIONS.items():
            if session.last_used_at < cutoff:
                stale_keys.append(account_id)
        for key in stale_keys:
            session = _ACCOUNT_SESSIONS.pop(key, None)
            if session is not None:
                session.close()


def close_account_playwright_session(account_id: str) -> None:
    normalized = str(account_id or "").strip()
    if not normalized:
        return
    with _SESSION_LOCK:
        session = _ACCOUNT_SESSIONS.pop(normalized, None)
    if session is not None:
        session.close()


def _create_session(account_id: str, signature: str, task: Dict[str, Any], context_factory: ContextFactory) -> _AccountSession:
    driver = sync_playwright().start()
    context, browser = context_factory(driver, task)
    try:
        page = context.pages[0] if context.pages else context.new_page()
    except Exception:
        context.close()
        if browser is not None:
            browser.close()
        driver.stop()
        raise
    return _AccountSession(
        account_id=account_id,
        signature=signature,
        driver=driver,
        context=context,
        browser=browser,
        page=page,
    )


def _get_or_create_session(task: Dict[str, Any], context_factory: ContextFactory) -> _AccountSession:
    cleanup_idle_playwright_sessions()
    account_id = _task_account_id(task)
    if not account_id:
        raise RuntimeError("playwright_reuse_missing_account_id")
    signature = _session_signature(task)
    current_thread_id = get_ident()
    with _SESSION_LOCK:
        existing = _ACCOUNT_SESSIONS.get(account_id)
        if existing is not None:
            same_signature = existing.signature == signature
            same_thread = existing.owner_thread_id == current_thread_id
            page_ok = False
            try:
                page_ok = existing.page is not None and not existing.page.is_closed()
            except Exception:  # noqa: BLE001
                page_ok = False
            if same_signature and same_thread and page_ok:
                existing.touch()
                return existing
            _ACCOUNT_SESSIONS.pop(account_id, None)
            existing.close()
        created = _create_session(account_id, signature, task, context_factory)
        _ACCOUNT_SESSIONS[account_id] = created
        return created


def _close_one_shot(context: BrowserContext, browser: Optional[Browser]) -> None:
    try:
        context.close()
    except Exception:  # noqa: BLE001
        pass
    if browser is not None:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def playwright_page_scope(
    task: Dict[str, Any],
    *,
    context_factory: ContextFactory,
    allow_reuse: bool = True,
) -> Iterator[Page]:
    reusable = allow_reuse and is_reuse_enabled_for_task(task)
    if reusable:
        session = _get_or_create_session(task, context_factory)
        try:
            session.touch()
            yield session.page
        except Exception:
            # Keep session warm on most failures. The next call will verify page state and
            # rebuild only if the page/context is truly closed.
            raise
        finally:
            session.touch()
        return

    with sync_playwright() as playwright:
        context, browser = context_factory(playwright, task)
        page = context.new_page()
        try:
            yield page
        finally:
            _close_one_shot(context, browser)


def _pause_bounds(min_ms: Optional[int], max_ms: Optional[int]) -> Tuple[int, int]:
    settings = get_settings()
    base_min = max(0, int(settings.playwright_action_pause_min_ms))
    base_max = max(base_min, int(settings.playwright_action_pause_max_ms))

    low = base_min if min_ms is None else max(0, int(min_ms))
    high = base_max if max_ms is None else max(0, int(max_ms))
    if high < low:
        high = low
    return low, high


def paced_wait(page: Page, min_ms: Optional[int] = None, max_ms: Optional[int] = None) -> None:
    low, high = _pause_bounds(min_ms, max_ms)
    page.wait_for_timeout(random.randint(low, high))
