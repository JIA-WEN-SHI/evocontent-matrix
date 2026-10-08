import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
import re

from playwright.sync_api import Browser, BrowserContext, Page, Playwright
from supabase import Client

from app.config import get_settings
from app.execution.router import resolve_publish_route
from app.execution.rpa import publish_handoff
from app.runtime.browser.playwright_runtime import paced_wait, playwright_page_scope
from app.tools.integrations.xhs_mcp_readonly import publish_content_via_mcp

RISK_KEYWORDS = [
    "风控",
    "安全验证",
    "验证码",
    "异常行为",
    "请先登录",
    "账号异常",
    "安全限制",
    "IP存在风险",
    "300012",
]
LOGIN_REQUIRED_KEYWORDS = ["请先登录", "登录后", "立即登录", "扫码登录"]
_INVALID_NOTE_ID_TOKENS = {
    "success",
    "publish",
    "note",
    "item",
    "explore",
    "showstatus",
    "status",
    "list",
    "detail",
}
_NOTE_ID_REGEX = re.compile(r"^[0-9A-Za-z_-]{12,128}$")


def _is_valid_xhs_note_id(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.lower() in _INVALID_NOTE_ID_TOKENS:
        return False
    if not _NOTE_ID_REGEX.match(text):
        return False
    if text.lower().startswith(("http", "www.")):
        return False
    return True


def _contains_cjk(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            return True
    return False


def _repair_mojibake_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    if _contains_cjk(raw):
        return raw
    try:
        repaired = raw.encode("latin1").decode("utf-8")
    except UnicodeError:
        return raw
    return repaired if repaired and _contains_cjk(repaired) else raw


def _normalize_xhs_publish_tab(meta: Dict[str, Any]) -> str:
    task_meta = meta if isinstance(meta, dict) else {}
    raw_tab = _repair_mojibake_text(str(task_meta.get("xhs_publish_tab") or "").strip())
    images = _normalize_file_list(task_meta.get("images") or task_meta.get("image_paths") or task_meta.get("local_images"))
    video = str(task_meta.get("video") or task_meta.get("video_path") or "").strip()

    normalized = raw_tab.replace(" ", "")
    if normalized in {"写长文", "长文", "图文长文"}:
        return "写长文"
    if normalized in {"上传图文", "图文", "发图文"}:
        return "上传图文"
    if normalized in {"上传视频", "视频", "发视频"}:
        return "上传视频"
    if video:
        return "上传视频"
    if images:
        return "上传图文"
    return "写长文"


def _normalize_publish_meta(task: Dict[str, Any]) -> Dict[str, Any]:
    meta = task.get("meta_jsonb") if isinstance(task.get("meta_jsonb"), dict) else {}
    normalized = dict(meta)
    normalized["title"] = _repair_mojibake_text(str(meta.get("title") or "").strip())
    normalized["body"] = _repair_mojibake_text(str(meta.get("body") or "").strip())
    normalized["xhs_publish_tab"] = _normalize_xhs_publish_tab(normalized)
    tags = meta.get("hashtags") if isinstance(meta.get("hashtags"), list) else meta.get("tags")
    if isinstance(tags, list):
        normalized["hashtags"] = [_repair_mojibake_text(str(x).strip()) for x in tags if str(x).strip()]
    return normalized


def extract_publish_identity(result: Dict[str, Any]) -> Dict[str, Any]:
    published_url = str(result.get("published_url") or "").strip()
    remote_post_id = str(result.get("remote_post_id") or "").strip()
    feed_id = str(result.get("feed_id") or "").strip()
    xsec_token = str(result.get("xsec_token") or "").strip()
    canonical_note_url = str(result.get("canonical_note_url") or "").strip()

    if published_url:
        parsed = urlparse(published_url)
        host = str(parsed.netloc or "").lower()
        is_xhs_host = "xiaohongshu.com" in host
        parts = [p for p in (parsed.path or "").split("/") if p]
        if is_xhs_host and not feed_id:
            for idx, part in enumerate(parts):
                if part in {"explore", "note"} and idx + 1 < len(parts):
                    candidate = parts[idx + 1].strip()
                    if _is_valid_xhs_note_id(candidate):
                        feed_id = candidate
                    break
                if part == "discovery" and idx + 1 < len(parts):
                    next_seg = parts[idx + 1].strip()
                    if next_seg in {"item", "note", "explore"} and idx + 2 < len(parts):
                        candidate = parts[idx + 2].strip()
                        if _is_valid_xhs_note_id(candidate):
                            feed_id = candidate
                    else:
                        if _is_valid_xhs_note_id(next_seg):
                            feed_id = next_seg
                    break
            if not feed_id and parts:
                tail = parts[-1].strip()
                if _is_valid_xhs_note_id(tail):
                    feed_id = tail
        if not xsec_token:
            qs = parse_qs(parsed.query or "")
            xsec_token = str((qs.get("xsec_token") or [""])[0] or "").strip()
            if not xsec_token:
                frag_qs = parse_qs((parsed.fragment or "").replace("?", "&"))
                xsec_token = str((frag_qs.get("xsec_token") or [""])[0] or "").strip()
        if feed_id and xsec_token and not canonical_note_url:
            canonical_note_url = f"https://www.xiaohongshu.com/explore/{feed_id}?xsec_token={xsec_token}"

    if feed_id and not remote_post_id:
        remote_post_id = feed_id

    identity: Dict[str, Any] = {
        "published_url": published_url,
        "remote_post_id": remote_post_id,
        "feed_id": feed_id,
        "xsec_token": xsec_token,
    }
    if canonical_note_url:
        identity["canonical_note_url"] = canonical_note_url
    return identity


def _dry_run_publish(task: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "status": "success",
        "remote_post_id": f"dryrun-{task['id']}",
        "published_url": f"https://example.com/posts/{task['id']}",
        "attempt": 1,
    }
    return {**result, **extract_publish_identity(result)}


def _walk_dict_nodes(value: Any) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            nodes.extend(_walk_dict_nodes(child))
    elif isinstance(value, list):
        for item in value:
            nodes.extend(_walk_dict_nodes(item))
    return nodes


def _extract_mcp_publish_fields(raw_result: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    url_candidates: List[str] = []
    feed_candidates: List[str] = []
    token_candidates: List[str] = []
    id_candidates: List[str] = []
    for node in _walk_dict_nodes(raw_result):
        for key in ["published_url", "note_url", "share_url", "url", "link", "jump_url"]:
            value = str(node.get(key) or "").strip()
            if value:
                url_candidates.append(value)
        for key in ["feed_id", "note_id", "post_id", "id", "remote_post_id"]:
            value = str(node.get(key) or "").strip()
            if _is_valid_xhs_note_id(value):
                if key in {"feed_id", "note_id"}:
                    feed_candidates.append(value)
                id_candidates.append(value)
        for key in ["xsec_token", "xsecToken"]:
            value = str(node.get(key) or "").strip()
            if value:
                token_candidates.append(value)

    published_url = url_candidates[0] if url_candidates else ""
    if published_url:
        fields.update(extract_publish_identity({"published_url": published_url}))
    if feed_candidates and not str(fields.get("feed_id") or "").strip():
        fields["feed_id"] = feed_candidates[0]
    if token_candidates and not str(fields.get("xsec_token") or "").strip():
        fields["xsec_token"] = token_candidates[0]
    if id_candidates and not str(fields.get("remote_post_id") or "").strip():
        fields["remote_post_id"] = id_candidates[0]
    if not str(fields.get("remote_post_id") or "").strip() and str(fields.get("feed_id") or "").strip():
        fields["remote_post_id"] = str(fields.get("feed_id") or "").strip()
    if not str(fields.get("published_url") or "").strip():
        feed_id = str(fields.get("feed_id") or "").strip()
        xsec_token = str(fields.get("xsec_token") or "").strip()
        if feed_id and xsec_token:
            fields["published_url"] = f"https://www.xiaohongshu.com/explore/{feed_id}?xsec_token={xsec_token}"
    return fields


def _normalize_tags(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    tags: List[str] = []
    for item in values:
        tag = str(item or "").strip().lstrip("#")
        if not tag or tag in tags:
            continue
        tags.append(tag[:80])
        if len(tags) >= 20:
            break
    return tags


def _normalize_file_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    files: List[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text or text in files:
            continue
        files.append(text)
        if len(files) >= 20:
            break
    return files


def publish_with_mcp(task: Dict[str, Any]) -> Dict[str, Any]:
    return _publishing_disabled_result()


def _random_sleep(page: Page, min_ms: int = 700, max_ms: int = 2200) -> None:
    paced_wait(page, min_ms=min_ms, max_ms=max_ms)


def _wait_for_any_selector(page: Page, selectors: List[str], timeout_ms: int = 8000) -> str:
    deadline = time.time() + (timeout_ms / 1000)
    candidates = [s for s in selectors if str(s).strip()]
    if not candidates:
        return ""
    while time.time() < deadline:
        for selector in candidates:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=500):
                    return selector
            except Exception:  # noqa: BLE001
                continue
        page.wait_for_timeout(300)
    return ""


def _normalize_url_for_compare(value: str) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _goto_if_needed(
    page: Page,
    target: str,
    *,
    timeout: int = 60_000,
    min_pause_ms: int = 900,
    max_pause_ms: int = 1800,
) -> None:
    desired = str(target or "").strip()
    if not desired:
        return
    current = str(page.url or "").strip()
    if _normalize_url_for_compare(current) == _normalize_url_for_compare(desired):
        _random_sleep(page, 450, 900)
        return
    page.goto(desired, timeout=timeout)
    _random_sleep(page, min_pause_ms, max_pause_ms)


def _human_like_mouse_move(page: Page, start: tuple[float, float], end: tuple[float, float]) -> None:
    steps = random.randint(16, 28)
    sx, sy = start
    ex, ey = end
    for i in range(1, steps + 1):
        t = i / steps
        jitter_x = random.uniform(-3, 3)
        jitter_y = random.uniform(-3, 3)
        x = sx + (ex - sx) * t + jitter_x
        y = sy + (ey - sy) * t + jitter_y
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(12, 30))


def _human_like_scroll(page: Page, rounds: int = 4) -> None:
    for _ in range(rounds):
        delta = random.randint(180, 520)
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(random.randint(250, 650))


def _cookies_from_json(raw_json: Any) -> List[Dict[str, Any]]:
    if not raw_json:
        return []
    if isinstance(raw_json, list):
        parsed = raw_json
    else:
        parsed = json.loads(str(raw_json))
    if not isinstance(parsed, list):
        raise ValueError("PLAYWRIGHT_SESSION_COOKIES_JSON must be a JSON array of cookie objects")
    return parsed


def _cookies_from_env(raw_json: str) -> List[Dict[str, Any]]:
    # Backward compatibility for modules still importing the old helper name.
    return _cookies_from_json(raw_json)


def _normalize_account_text(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    # Keep chinese/letters/digits only for fuzzy matching.
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return normalized


def _xhs_detect_topbar_account_candidates(page: Page) -> List[str]:
    try:
        values = page.evaluate(
            """() => {
              const pool = [];
              const selectors = [
                '[class*="user-name"]',
                '[class*="username"]',
                '[class*="nick"]',
                '[class*="account"]',
                'header span',
                'header div',
                'header a'
              ];
              for (const sel of selectors) {
                const nodes = Array.from(document.querySelectorAll(sel));
                for (const el of nodes) {
                  const rect = el.getBoundingClientRect();
                  if (rect.top < 0 || rect.top > 160) continue;
                  if (rect.left < window.innerWidth * 0.55) continue;
                  const text = (el.textContent || '').trim();
                  if (!text || text.length < 2 || text.length > 40) continue;
                  if (/草稿箱|发布笔记|首页|笔记管理|创作服务平台/.test(text)) continue;
                  pool.push(text);
                }
              }
              return Array.from(new Set(pool)).slice(0, 12);
            }"""
        )
        if isinstance(values, list):
            return [str(x).strip() for x in values if str(x).strip()]
    except Exception:  # noqa: BLE001
        return []
    return []


def _xhs_assert_expected_account(
    page: Page,
    *,
    expected_name: str = "",
    expected_handle: str = "",
    verify_name: bool = False,
) -> None:
    target_raw = ""
    if str(expected_handle or "").strip():
        target_raw = str(expected_handle or "").strip()
    elif verify_name and str(expected_name or "").strip():
        target_raw = str(expected_name or "").strip()

    target = _normalize_account_text(target_raw)
    if not target:
        return
    candidates = _xhs_detect_topbar_account_candidates(page)
    if not candidates:
        raise RuntimeError("xhs_account_identity_not_detected")

    norm_candidates = [_normalize_account_text(x) for x in candidates if _normalize_account_text(x)]
    matched = any((target in cand) or (cand in target) for cand in norm_candidates)
    if not matched:
        raise RuntimeError(f"xhs_account_mismatch:expected={expected_handle or expected_name};actual={','.join(candidates)}")


def _playwright_proxy(settings: Any) -> Dict[str, str] | None:
    server = str(getattr(settings, "playwright_proxy_server", "") or "").strip()
    if not server:
        return None
    proxy: Dict[str, str] = {"server": server}
    username = str(getattr(settings, "playwright_proxy_username", "") or "").strip()
    password = str(getattr(settings, "playwright_proxy_password", "") or "").strip()
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy


def _capture_failure_artifact(page: Page, task_id: str, attempt: int, reason: str) -> Dict[str, str]:
    settings = get_settings()
    artifact_dir = Path(settings.playwright_artifacts_dir) / task_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    screenshot_path = artifact_dir / f"attempt-{attempt}-{ts}.png"
    html_path = artifact_dir / f"attempt-{attempt}-{ts}.html"

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    return {"screenshot_path": str(screenshot_path), "html_path": str(html_path), "reason": reason}


def _parse_publish_error_payload(value: Any) -> Dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    if not raw.startswith("{"):
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_failed_publish_result(result: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(result or {})
    parsed_error = _parse_publish_error_payload(normalized.get("error"))
    parsed_artifact = normalized.get("artifact") if isinstance(normalized.get("artifact"), dict) else {}
    artifact = parsed_artifact or parsed_error
    if artifact:
        normalized["artifact"] = artifact
    reason = str(artifact.get("reason") or "").strip()
    if reason:
        normalized["error_code"] = reason
        normalized["error"] = reason
    elif str(normalized.get("error_code") or "").strip():
        normalized["error"] = str(normalized.get("error_code") or "").strip()
    elif not str(normalized.get("error") or "").strip():
        normalized["error"] = "publish_failed"
    return normalized


def _is_non_retryable_publish_error(result: Dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(result.get("error") or "").strip(),
            str(result.get("error_code") or "").strip(),
            str(((result.get("artifact") or {}) if isinstance(result.get("artifact"), dict) else {}).get("reason") or "").strip(),
        ]
    ).lower()
    hard_stop_tokens = [
        "publish_not_confirmed_after_click",
        "auth_required",
        "credential_login_failed",
        "xhs_account_mismatch",
        "xhs_account_identity_not_detected",
        "risk_or_auth_blocked",
    ]
    return any(token in text for token in hard_stop_tokens)


def _resolve_output_path(path_text: str) -> Path:
    candidate = Path(str(path_text or "").strip()).expanduser()
    if candidate.is_absolute():
        return candidate
    return (Path.cwd() / candidate).resolve()


def _resolve_runtime_path(path_text: str) -> str:
    text = str(path_text or "").strip()
    if not text:
        return ""
    return str(_resolve_output_path(text))


def _resolve_account_id_from_task(task: Dict[str, Any]) -> str:
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
    return ""


def _save_storage_state(context: BrowserContext, storage_state_path: str) -> None:
    resolved = _resolve_output_path(storage_state_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(resolved))


def _is_login_required(page: Page) -> bool:
    url_text = (page.url or "").lower()
    if "login" in url_text or "website-login" in url_text:
        return True
    try:
        content = page.content()
    except Exception:  # noqa: BLE001
        content = ""
    return any(token in content for token in LOGIN_REQUIRED_KEYWORDS)


def _try_click_first(page: Page, selectors: List[str], timeout: int = 2500) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _try_fill_first(page: Page, selectors: List[str], text: str, timeout: int = 2500) -> bool:
    if not text:
        return False
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.fill(text, timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _switch_xhs_publish_tab(page: Page, tab_name: str) -> bool:
    target = str(tab_name or "").strip() or "写长文"
    try:
        page.wait_for_selector(".header-tabs .creator-tab", timeout=12_000)
    except Exception:  # noqa: BLE001
        return False
    if _get_xhs_active_publish_tab(page) == target:
        return True

    selector_candidates = [
        f".header-tabs .creator-tab:has-text('{target}')",
        f".header-tabs .creator-tab .title:text-is('{target}')",
        f"text='{target}'",
    ]
    for _ in range(3):
        for selector in selector_candidates:
            try:
                locator = page.locator(selector).first
                if locator.count() <= 0:
                    continue
                locator.click(timeout=4_000, force=True)
                page.wait_for_timeout(500)
                if _get_xhs_active_publish_tab(page) == target:
                    return True
            except Exception:  # noqa: BLE001
                continue
        try:
            clicked = page.evaluate(
                """(tabName) => {
                  const tabs = Array.from(document.querySelectorAll('.header-tabs .creator-tab'));
                  const match = tabs.find((el) => (el.textContent || '').includes(tabName));
                  if (!match) return false;
                  const title = match.querySelector('.title');
                  (title || match).click();
                  return true;
                }""",
                target,
            )
            if clicked:
                page.wait_for_timeout(600)
                if _get_xhs_active_publish_tab(page) == target:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return _get_xhs_active_publish_tab(page) == target


def _get_xhs_active_publish_tab(page: Page) -> str:
    candidates = [
        ".header-tabs .creator-tab.active .title",
        ".header-tabs .creator-tab.active",
        ".header-tabs .creator-tab[aria-selected='true'] .title",
        ".header-tabs .creator-tab[aria-selected='true']",
    ]
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            text = str(locator.inner_text(timeout=1500) or "").strip()
            if text:
                return text
        except Exception:  # noqa: BLE001
            continue
    try:
        text = page.evaluate(
            """() => {
              const active = document.querySelector('.header-tabs .creator-tab.active .title, .header-tabs .creator-tab.active');
              return active ? (active.textContent || '').trim() : '';
            }"""
        )
        return str(text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _xhs_editor_ready(page: Page, preferred_tab: str) -> bool:
    # "新的创作/导入链接" 仅代表长文入口页，不代表可直接填写编辑器。
    base_editable_selectors = [
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
        "textarea[placeholder*='正文']",
        "textarea[placeholder*='内容']",
        "div[contenteditable='true'][data-placeholder*='正文']",
        "div[contenteditable='true'][placeholder*='正文']",
        ".rich-editor-content .ProseMirror[contenteditable='true']",
        ".ProseMirror[contenteditable='true']",
    ]
    if preferred_tab == "上传图文":
        selector_groups = [
            [
                "input[type='file'][accept*='image']",
                "input[type='file'][accept*='jpg']",
                "input[type='file'][accept*='jpeg']",
                "input[type='file'][accept*='png']",
                "button:has-text('上传图文')",
                "button:has-text('上传图片')",
            ],
            base_editable_selectors,
        ]
    else:
        selector_groups = [base_editable_selectors]
    for selectors in selector_groups:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=1200):
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _fill_xhs_fields(page: Page, title: str, body: str) -> Dict[str, bool]:
    safe_title = _repair_mojibake_text(str(title or "").strip())
    safe_body = _repair_mojibake_text(str(body or "").strip())
    if not (safe_title or safe_body):
        return {"title_ok": False, "body_ok": False}

    # First try regular locators.
    title_ok = _try_fill_first(
        page,
        [
            "input[placeholder*='标题']",
            "textarea[placeholder*='标题']",
            "input[maxlength]",
        ],
        safe_title[:60] if safe_title else "",
        timeout=3000,
    )
    body_ok = _try_fill_first(
        page,
        [
            "textarea[placeholder*='正文']",
            "textarea[placeholder*='内容']",
            "textarea[placeholder*='输入正文']",
            "div.editable-textarea",
            "div[contenteditable='true'][data-placeholder*='正文']",
            "div[contenteditable='true'][data-placeholder*='输入正文']",
            "div[contenteditable='true'][placeholder*='正文']",
            ".editor-content [contenteditable='true']",
            ".rich-editor-content .ProseMirror[contenteditable='true']",
            ".editor-content .ProseMirror",
        ],
        safe_body,
        timeout=3000,
    )

    # Fallback for dynamic Vue editors.
    try:
        fallback = page.evaluate(
            """({ title, body }) => {
              const setText = (el, value) => {
                if (!el || !value) return false;
                el.focus();
                if ('value' in el) {
                  el.value = value;
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                  return true;
                }
                if (el.getAttribute('contenteditable') === 'true') {
                  el.innerText = value;
                  el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
                  return true;
                }
                return false;
              };

                const allInputs = Array.from(document.querySelectorAll('input, textarea'));
                const titleInput = allInputs.find((el) => ((el.getAttribute('placeholder') || '').includes('标题')));
              const bodyInput = allInputs.find((el) => {
                const ph = (el.getAttribute('placeholder') || '');
                return ph.includes('正文') || ph.includes('内容') || ph.includes('输入正文');
              });

              let titleOk = false;
              let bodyOk = false;

              if (title && titleInput) titleOk = setText(titleInput, title);
              if (body && bodyInput) bodyOk = setText(bodyInput, body);

              if (!bodyOk && body) {
                const editables = Array.from(document.querySelectorAll("[contenteditable='true']"))
                  .filter((el) => (el.textContent || '').length < 5000);
                if (editables.length > 0) {
                  const editable = editables[editables.length - 1];
                  bodyOk = setText(editable, body);
                }
              }
              if (!bodyOk && body) {
                const rich = document.querySelector('.editable-textarea, .editor-content .ProseMirror, .ProseMirror');
                if (rich) bodyOk = setText(rich, body);
              }

              if (!titleOk && title) {
                const possibleTitle = allInputs.find((el) => {
                  const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                  const cls = (el.className || '').toString().toLowerCase();
                  return ph.includes('title') || cls.includes('title');
                });
                if (possibleTitle) titleOk = setText(possibleTitle, title);
              }

              return { titleOk, bodyOk };
            }""",
            {"title": safe_title[:60], "body": safe_body},
        )
        if isinstance(fallback, dict):
            title_ok = bool(title_ok or fallback.get("titleOk"))
            body_ok = bool(body_ok or fallback.get("bodyOk"))
    except Exception:  # noqa: BLE001
        pass

    # Final fallback: type directly into ProseMirror editor.
    if safe_body and not body_ok:
        for selector in [
            ".editable-textarea",
            ".editor-content .ProseMirror",
            ".editor-content [contenteditable='true']",
            ".rich-editor-content .ProseMirror[contenteditable='true']",
            ".ProseMirror[contenteditable='true']",
            "div[contenteditable='true'].ProseMirror",
        ]:
            try:
                locator = page.locator(selector).first
                if locator.count() <= 0:
                    continue
                locator.click(timeout=3000)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                for idx, line in enumerate(safe_body.splitlines() or [safe_body]):
                    segment = line.strip()
                    if segment:
                        page.keyboard.type(segment, delay=8)
                    if idx < len((safe_body.splitlines() or [safe_body])) - 1:
                        page.keyboard.press("Enter")
                page.wait_for_timeout(300)
                body_ok = True
                break
            except Exception:  # noqa: BLE001
                continue

    return {"title_ok": bool(title_ok), "body_ok": bool(body_ok)}


def _prepare_xhs_publish_content(page: Page, task_meta: Dict[str, Any]) -> None:
    title = str(task_meta.get("title") or "").strip()
    body = str(task_meta.get("body") or "").strip()
    if not (title or body):
        return

    preferred_tab = str(task_meta.get("xhs_publish_tab") or "").strip() or "写长文"
    _wait_for_any_selector(
        page,
        [
            ".header-tabs .creator-tab",
            "button:has-text('新建长文')",
            "button:has-text('新的创作')",
            "input[placeholder*='标题']",
            "textarea[placeholder*='正文']",
            "div[contenteditable='true']",
        ],
        timeout_ms=10_000,
    )
    switched = _switch_xhs_publish_tab(page, preferred_tab)
    active_tab = _get_xhs_active_publish_tab(page)
    if not switched or active_tab != preferred_tab:
        raise RuntimeError(f"xhs_publish_tab_not_ready:{preferred_tab}:active={active_tab or 'unknown'}")
    _random_sleep(page, 900, 1600)

    # For "写长文", the first screen often requires entering an editor first.
    if active_tab == "写长文" and not _xhs_editor_ready(page, active_tab):
        for _ in range(3):
            _try_click_first(
                page,
                [
                    "button:has-text('新的创作')",
                    "button:has-text('新建长文')",
                    "button:has-text('创建')",
                    "div:has-text('新的创作')",
                    "div:has-text('导入链接')",
                ],
                timeout=3000,
            )
            _random_sleep(page, 900, 1600)
            if _xhs_editor_ready(page, active_tab):
                break
        if not _xhs_editor_ready(page, active_tab):
            raise RuntimeError(f"xhs_longform_editor_not_ready:active={_get_xhs_active_publish_tab(page) or active_tab}")
    elif active_tab == "上传图文" and not _xhs_editor_ready(page, active_tab):
        raise RuntimeError("xhs_image_editor_not_ready")

    filled = _fill_xhs_fields(page, title=title, body=body)
    if not filled.get("title_ok") and not filled.get("body_ok"):
        raise RuntimeError(f"xhs_editor_fill_failed:tab={active_tab or preferred_tab}")


def _login_xiaohongshu_with_credentials(
    page: Page,
    *,
    username: str,
    password: str,
    storage_state_path: str = "",
) -> Dict[str, Any]:
    try:
        _goto_if_needed(page, "https://www.xiaohongshu.com", timeout=60_000, min_pause_ms=900, max_pause_ms=1800)
        _try_click_first(
            page,
            [
                "button:has-text('登录')",
                "a:has-text('登录')",
                "div:has-text('登录')",
            ],
            timeout=2500,
        )
        page.wait_for_timeout(1200)
        _try_click_first(
            page,
            [
                "button:has-text('密码登录')",
                "div:has-text('密码登录')",
                "span:has-text('密码登录')",
            ],
            timeout=2500,
        )
        page.wait_for_timeout(600)

        user_ok = _try_fill_first(
            page,
            [
                "input[placeholder*='手机号']",
                "input[placeholder*='邮箱']",
                "input[placeholder*='账号']",
                "input[placeholder*='小红书号']",
                "input[type='text']",
            ],
            username,
            timeout=3000,
        )
        pwd_ok = _try_fill_first(
            page,
            [
                "input[type='password']",
                "input[placeholder*='密码']",
            ],
            password,
            timeout=3000,
        )

        if not (user_ok and pwd_ok):
            blocker = _check_blockers(page)
            if blocker:
                return {"status": "failed", "reason": blocker}
            # Fallback: try script-based input filling for dynamic forms.
            script_ok = bool(
                page.evaluate(
                    """({ username, password }) => {
                      const inputs = Array.from(document.querySelectorAll('input'));
                      if (!inputs.length) return false;
                      let userInput = null;
                      let passInput = null;
                      for (const input of inputs) {
                        const ph = (input.getAttribute('placeholder') || '').toLowerCase();
                        const tp = (input.getAttribute('type') || '').toLowerCase();
                        if (!passInput && (tp === 'password' || ph.includes('密码'))) passInput = input;
                        if (!userInput && (ph.includes('手机号') || ph.includes('账号') || ph.includes('邮箱') || ph.includes('小红书'))) {
                          userInput = input;
                        }
                      }
                      if (!userInput) {
                        userInput = inputs.find((i) => (i.getAttribute('type') || '').toLowerCase() !== 'password') || null;
                      }
                      if (!passInput) {
                        passInput = inputs.find((i) => (i.getAttribute('type') || '').toLowerCase() === 'password') || null;
                      }
                      if (!userInput || !passInput) return false;
                      userInput.focus();
                      userInput.value = username;
                      userInput.dispatchEvent(new Event('input', { bubbles: true }));
                      userInput.dispatchEvent(new Event('change', { bubbles: true }));
                      passInput.focus();
                      passInput.value = password;
                      passInput.dispatchEvent(new Event('input', { bubbles: true }));
                      passInput.dispatchEvent(new Event('change', { bubbles: true }));
                      return true;
                    }""",
                    {"username": username, "password": password},
                )
            )
            if not script_ok:
                blocker = _check_blockers(page)
                if blocker:
                    return {"status": "failed", "reason": blocker}
                return {"status": "failed", "reason": "credential_inputs_not_found"}

        _try_click_first(
            page,
            [
                "button:has-text('登录')",
                "button:has-text('同意并登录')",
                "div.login-btn",
            ],
            timeout=3500,
        )
        page.wait_for_timeout(7000)
        if _is_login_required(page):
            return {"status": "failed", "reason": "login_not_completed_or_captcha_required"}
        if storage_state_path:
            _save_storage_state(page.context, storage_state_path)
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason": str(exc)}


def _open_context(playwright: Playwright, task: Dict[str, Any]) -> tuple[BrowserContext, Optional[Browser]]:
    settings = get_settings()
    proxy = _playwright_proxy(settings)
    task_meta = task.get("meta_jsonb") if isinstance(task.get("meta_jsonb"), dict) else {}
    account_id = _resolve_account_id_from_task(task)
    strict_scope = bool(task_meta.get("playwright_strict_account_scope"))
    login_mode = str(task_meta.get("playwright_login_mode") or "").strip().lower()
    login_username = str(task_meta.get("playwright_login_username") or "").strip()
    login_password = str(task_meta.get("playwright_login_password") or "")

    if strict_scope:
        storage_state_path = _resolve_runtime_path(str(task_meta.get("playwright_storage_state_path") or "").strip())
        user_data_dir = _resolve_runtime_path(str(task_meta.get("playwright_user_data_dir") or "").strip())
        cookies_raw = task_meta.get("playwright_session_cookies_json")
    else:
        storage_state_path = _resolve_runtime_path(str(task_meta.get("playwright_storage_state_path") or settings.playwright_storage_state_path))
        user_data_dir = _resolve_runtime_path(str(task_meta.get("playwright_user_data_dir") or settings.playwright_user_data_dir))
        cookies_raw = task_meta.get("playwright_session_cookies_json") or settings.playwright_session_cookies_json
    cookies = _cookies_from_json(cookies_raw)
    strict_scope = bool(task_meta.get("playwright_strict_account_scope"))

    credential_mode_ready = login_mode == "credential" and bool(login_username and login_password)
    if strict_scope and login_mode == "credential" and not user_data_dir:
        fallback_account_id = account_id or "default"
        user_data_dir = _resolve_runtime_path(str(Path(".run") / "account_user_data" / fallback_account_id))

    if strict_scope and not (storage_state_path or user_data_dir or cookies or credential_mode_ready):
        raise RuntimeError("account_auth_material_missing_under_strict_scope")

    if user_data_dir:
        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": user_data_dir,
            "headless": settings.playwright_headless,
            "viewport": {"width": 1366, "height": 900},
        }
        if proxy:
            launch_kwargs["proxy"] = proxy
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        if cookies:
            context.add_cookies(cookies)
        return context, None

    browser_kwargs: Dict[str, Any] = {"headless": settings.playwright_headless}
    if proxy:
        browser_kwargs["proxy"] = proxy
    browser = playwright.chromium.launch(**browser_kwargs)
    new_context_kwargs: Dict[str, Any] = {"viewport": {"width": 1366, "height": 900}}
    if storage_state_path:
        new_context_kwargs["storage_state"] = storage_state_path
    context = browser.new_context(**new_context_kwargs)
    if cookies:
        context.add_cookies(cookies)
    return context, browser


def _target_url(channel: str) -> str:
    if channel == "xiaohongshu":
        return "https://creator.xiaohongshu.com/publish/publish"
    if channel == "wechat_mp":
        return "https://mp.weixin.qq.com/cgi-bin/home"
    raise RuntimeError(f"Unsupported channel: {channel}")


def _check_blockers(page: Page) -> Optional[str]:
    try:
        content = page.content()
    except Exception:  # noqa: BLE001
        content = ""
    for keyword in RISK_KEYWORDS:
        if keyword in content:
            return f"risk_or_auth_blocked:{keyword}"
    for keyword in LOGIN_REQUIRED_KEYWORDS:
        if keyword in content:
            return "auth_required"
    return None


def _wait_for_xhs_publish_shell(page: Page) -> str:
    ready = _wait_for_any_selector(
        page,
        [
            ".header-tabs .creator-tab",
            "button:has-text('发布')",
            "button:has-text('发布笔记')",
            "button:has-text('新建长文')",
            "button:has-text('新的创作')",
            "input[placeholder*='标题']",
            "textarea[placeholder*='正文']",
            "div[contenteditable='true']",
            "button:has-text('登录')",
            "text=扫码登录",
        ],
        timeout_ms=12_000,
    )
    if ready and ("登录" in ready or "扫码" in ready):
        return "login_required"
    if _is_login_required(page):
        return "login_required"
    return ready


def _xhs_publish_confirmed(page: Page) -> bool:
    url_text = (page.url or "").lower()
    if "xiaohongshu.com/explore/" in url_text or "xiaohongshu.com/note/" in url_text:
        return True
    if "creator.xiaohongshu.com/publish/publish" not in url_text:
        return True
    try:
        content = page.content()
    except Exception:  # noqa: BLE001
        content = ""
    success_tokens = [
        "发布成功",
        "发布完成",
        "笔记发布成功",
        "已发布",
        "审核中",
    ]
    return any(token in content for token in success_tokens)


def _xhs_click_primary_publish(page: Page) -> bool:
    try:
        clicked = page.evaluate(
            """() => {
              const candidates = Array.from(document.querySelectorAll('button, [role="button"]'));
              const wanted = new Set(['发布', '去发布', '确认发布', '发布并同步', '完成']);
              const visible = candidates.filter((el) => {
                const text = (el.textContent || '').trim();
                if (!wanted.has(text)) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width < 44 || rect.height < 24) return false;
                if (rect.bottom <= 0 || rect.top >= window.innerHeight) return false;
                if (rect.right <= 0 || rect.left >= window.innerWidth) return false;
                const style = window.getComputedStyle(el);
                if (!style || style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity || '1') < 0.1) {
                  return false;
                }
                return true;
              });
              if (!visible.length) return false;
              const scored = visible.map((el) => {
                const text = (el.textContent || '').trim();
                const rect = el.getBoundingClientRect();
                let score = 0;
                if (text === '发布') score += 120;
                if (text === '确认发布' || text === '去发布') score += 90;
                if (rect.top > window.innerHeight * 0.55) score += 60;
                if (rect.left > 220) score += 25;
                if (rect.left < 220 && rect.top < 180) score -= 140; // avoid left-top "发布笔记" menu area
                const cls = (el.className || '').toString().toLowerCase();
                if (cls.includes('submit') || cls.includes('publish')) score += 30;
                return { el, score };
              }).sort((a, b) => b.score - a.score);
              const target = scored[0]?.el;
              if (!target) return false;
              target.click();
              return true;
            }"""
        )
        return bool(clicked)
    except Exception:  # noqa: BLE001
        return False


def _xhs_accept_publish_dialog_if_any(page: Page) -> bool:
    touched = False
    touched = _try_click_first(
        page,
        [
            "button:has-text('确认发布')",
            "button:has-text('继续发布')",
            "button:has-text('我知道了')",
            "button:has-text('确定')",
            "button:has-text('确认')",
            "button:has-text('同意并继续')",
        ],
        timeout=1500,
    ) or touched
    try:
        auto_checked = bool(
            page.evaluate(
                """() => {
                  let changed = false;
                  const nodes = Array.from(document.querySelectorAll('input[type="checkbox"], label'));
                  for (const el of nodes) {
                    const text = (el.textContent || '').trim();
                    const hint = /我已阅读|已阅读|同意|确认|原创|声明|协议/.test(text);
                    if (!hint) continue;
                    if (el.tagName.toLowerCase() === 'input') {
                      if (!(el).checked) { el.click(); changed = true; }
                    } else {
                      el.click(); changed = true;
                    }
                  }
                  return changed;
                }"""
            )
        )
        touched = touched or auto_checked
    except Exception:  # noqa: BLE001
        pass
    return touched


def _xhs_extract_identity_from_dom(page: Page, expected_title: str = "") -> Dict[str, Any]:
    try:
        items = page.evaluate(
            """() => {
              const rows = [];
              const anchors = Array.from(document.querySelectorAll('a[href]'));
              for (const a of anchors) {
                const href = String(a.getAttribute('href') || '');
                if (!href) continue;
                if (!(href.includes('/explore/') || href.includes('/note/') || href.includes('/discovery/item/') || href.includes('/discovery/note/'))) {
                  continue;
                }
                let abs = href;
                if (href.startsWith('/')) abs = `https://www.xiaohongshu.com${href}`;
                const card = a.closest('li, article, .note-item, .card, .list-item, .item') || a.parentElement;
                const text = (card?.textContent || a.textContent || '').slice(0, 400);
                rows.push({ href: abs, text });
              }
              return rows.slice(0, 40);
            }"""
        )
    except Exception:  # noqa: BLE001
        return {}

    if not isinstance(items, list):
        return {}
    expected = str(expected_title or "").strip()
    best_score = -1
    best_identity: Dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or "").strip()
        if not href:
            continue
        parsed = extract_publish_identity({"published_url": href})
        feed_id = str(parsed.get("feed_id") or "").strip()
        if not _is_valid_xhs_note_id(feed_id):
            continue
        text = str(item.get("text") or "")
        score = 10
        if expected and expected[:8] and expected[:8] in text:
            score += 60
        if "/explore/" in href:
            score += 20
        if score > best_score:
            best_score = score
            best_identity = parsed
    return best_identity


def _xhs_title_exists_on_page(page: Page, expected_title: str) -> bool:
    title = str(expected_title or "").strip()
    if not title:
        return False
    probe = title[:16]
    try:
        found = page.evaluate(
            """(needle) => {
              const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
              return needle && text.includes(needle);
            }""",
            probe,
        )
        return bool(found)
    except Exception:  # noqa: BLE001
        return False


def _xhs_backfill_identity(
    page: Page,
    network_identities: List[Dict[str, Any]],
    *,
    expected_title: str = "",
) -> Dict[str, Any]:
    best = _choose_best_xhs_identity(network_identities)
    if _is_valid_xhs_note_id(str(best.get("feed_id") or "").strip()):
        return best

    _try_click_first(
        page,
        [
            "button:has-text('立即返回')",
            "a:has-text('立即返回')",
            "button:has-text('返回')",
            "a:has-text('返回')",
        ],
        timeout=2000,
    )
    page.wait_for_timeout(1200)
    _try_click_first(
        page,
        [
            "a:has-text('笔记管理')",
            "div:has-text('笔记管理')",
            "span:has-text('笔记管理')",
        ],
        timeout=2500,
    )
    page.wait_for_timeout(3000)

    best = _choose_best_xhs_identity(network_identities)
    if _is_valid_xhs_note_id(str(best.get("feed_id") or "").strip()):
        return best

    dom_identity = _xhs_extract_identity_from_dom(page, expected_title=expected_title)
    if _is_valid_xhs_note_id(str(dom_identity.get("feed_id") or "").strip()):
        return dom_identity
    if _xhs_title_exists_on_page(page, expected_title):
        return {
            "published_url": page.url,
            "remote_post_id": f"title_match:{expected_title[:24]}",
            "title_matched": True,
            "verification_pending": True,
            "verification_method": "title_match",
        }
    return {}


def _walk_json_candidates(value: Any) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        candidates.append(value)
        for child in value.values():
            candidates.extend(_walk_json_candidates(child))
    elif isinstance(value, list):
        for item in value:
            candidates.extend(_walk_json_candidates(item))
    return candidates


def _extract_xhs_identity_from_response(response: Any) -> Dict[str, Any]:
    identity: Dict[str, Any] = {}
    try:
        url_text = str(response.url or "")
    except Exception:  # noqa: BLE001
        url_text = ""
    if not url_text:
        return identity

    # First pass: parse IDs from URL only when URL clearly points to note pages.
    lowered_url = url_text.lower()
    looks_like_note_url = (
        "/explore/" in lowered_url
        or "/note/" in lowered_url
        or "/discovery/item/" in lowered_url
        or "/discovery/note/" in lowered_url
    )
    if looks_like_note_url:
        url_identity = extract_publish_identity({"published_url": url_text})
        feed_id = str(url_identity.get("feed_id") or "").strip()
        xsec_token = str(url_identity.get("xsec_token") or "").strip()
        canonical_note_url = str(url_identity.get("canonical_note_url") or "").strip()
        if feed_id and _is_valid_xhs_note_id(feed_id):
            identity["feed_id"] = feed_id
            identity["remote_post_id"] = feed_id
        if xsec_token:
            identity["xsec_token"] = xsec_token
        if canonical_note_url:
            identity["canonical_note_url"] = canonical_note_url

    # Second pass: parse JSON body and probe common id fields.
    try:
        content_type = str((response.header_value("content-type") or "")).lower()
    except Exception:  # noqa: BLE001
        content_type = ""

    body_json: Any = None
    should_parse_json = ("json" in content_type) or ("api/" in url_text and "xiaohongshu.com" in url_text)
    if should_parse_json:
        try:
            body_json = response.json()
        except Exception:  # noqa: BLE001
            body_json = None

    if body_json is not None:
        for node in _walk_json_candidates(body_json):
            if not isinstance(node, dict):
                continue
            note_id = str(
                node.get("note_id")
                or node.get("noteId")
                or node.get("feed_id")
                or node.get("feedId")
                or node.get("post_id")
                or node.get("postId")
                or ""
            ).strip()
            token = str(node.get("xsec_token") or node.get("xsecToken") or "").strip()
            if _is_valid_xhs_note_id(note_id):
                identity.setdefault("feed_id", note_id)
                identity.setdefault("remote_post_id", note_id)
            if token:
                identity.setdefault("xsec_token", token)
            if identity.get("feed_id") and identity.get("xsec_token"):
                identity.setdefault(
                    "canonical_note_url",
                    f"https://www.xiaohongshu.com/explore/{identity['feed_id']}?xsec_token={identity['xsec_token']}",
                )
                break

    return identity


def _choose_best_xhs_identity(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return {}
    # Prefer the latest identity that has feed_id; if tie, prefer one with xsec_token.
    best: Dict[str, Any] = {}
    for item in items:
        item_feed_id = str(item.get("feed_id") or "").strip()
        best_feed_id = str(best.get("feed_id") or "").strip()
        if item_feed_id and not best_feed_id:
            best = item
            continue
        if item_feed_id and best_feed_id:
            if item.get("xsec_token") and not best.get("xsec_token"):
                best = item
            else:
                best = item
    return best or items[-1]


def _publish_once(task: Dict[str, Any], attempt: int) -> Dict[str, Any]:
    normalized_task = dict(task)
    normalized_task["meta_jsonb"] = _normalize_publish_meta(task)
    with playwright_page_scope(normalized_task, context_factory=_open_context) as page:
        response_handler = None
        try:
            network_identities: List[Dict[str, Any]] = []
            if normalized_task.get("channel") == "xiaohongshu":
                def _on_response(response: Any) -> None:
                    try:
                        identity = _extract_xhs_identity_from_response(response)
                        if identity:
                            network_identities.append(identity)
                            if len(network_identities) > 200:
                                del network_identities[:-120]
                    except Exception:  # noqa: BLE001
                        return

                response_handler = _on_response
                page.on("response", response_handler)

            task_meta = normalized_task.get("meta_jsonb") if isinstance(normalized_task.get("meta_jsonb"), dict) else {}
            login_mode = str(task_meta.get("playwright_login_mode") or "").strip().lower()
            login_username = str(task_meta.get("playwright_login_username") or "").strip()
            login_password = str(task_meta.get("playwright_login_password") or "")
            storage_state_path = str(task_meta.get("playwright_storage_state_path") or "")
            expected_account_name = str(task_meta.get("expected_account_name") or "").strip()
            expected_account_handle = str(task_meta.get("expected_account_handle") or "").strip()
            verify_account_name = bool(task_meta.get("verify_account_name"))
            _goto_if_needed(page, _target_url(normalized_task["channel"]), timeout=60_000)

            if normalized_task.get("channel") == "xiaohongshu":
                shell_state = _wait_for_xhs_publish_shell(page)
                if shell_state == "login_required":
                    blocker_reason = "auth_required_before_publish"
                    artifact = _capture_failure_artifact(page, normalized_task["id"], attempt, blocker_reason)
                    raise RuntimeError(json.dumps(artifact, ensure_ascii=False))

            if _is_login_required(page):
                if normalized_task.get("channel") == "xiaohongshu" and login_username and login_password:
                    login_result = _login_xiaohongshu_with_credentials(
                        page,
                        username=login_username,
                        password=login_password,
                        storage_state_path=storage_state_path,
                    )
                    if str(login_result.get("status")) != "ok":
                        blocker_reason = f"credential_login_failed:{login_result.get('reason')}"
                        artifact = _capture_failure_artifact(page, normalized_task["id"], attempt, blocker_reason)
                        raise RuntimeError(json.dumps(artifact, ensure_ascii=False))
                    _goto_if_needed(
                        page,
                        _target_url(normalized_task["channel"]),
                        timeout=60_000,
                        min_pause_ms=800,
                        max_pause_ms=1500,
                    )
                    if normalized_task.get("channel") == "xiaohongshu":
                        shell_state = _wait_for_xhs_publish_shell(page)
                        if shell_state == "login_required":
                            blocker_reason = "auth_required_after_login"
                            artifact = _capture_failure_artifact(page, normalized_task["id"], attempt, blocker_reason)
                            raise RuntimeError(json.dumps(artifact, ensure_ascii=False))
                else:
                    blocker_reason = "auth_required_and_no_credentials"
                    artifact = _capture_failure_artifact(page, normalized_task["id"], attempt, blocker_reason)
                    raise RuntimeError(json.dumps(artifact, ensure_ascii=False))

            blocker = _check_blockers(page)
            if blocker:
                artifact = _capture_failure_artifact(page, normalized_task["id"], attempt, blocker)
                raise RuntimeError(json.dumps(artifact, ensure_ascii=False))

            if normalized_task.get("channel") == "xiaohongshu":
                _xhs_assert_expected_account(
                    page,
                    expected_name=expected_account_name,
                    expected_handle=expected_account_handle,
                    verify_name=verify_account_name,
                )

            _human_like_scroll(page, rounds=random.randint(2, 5))
            _random_sleep(page, 500, 1200)

            safe_x = random.randint(220, 980)
            safe_y = random.randint(180, 720)
            _human_like_mouse_move(page, (20, 20), (safe_x, safe_y))
            page.mouse.click(safe_x, safe_y, delay=random.randint(40, 120))
            _random_sleep(page, 800, 1800)

            if normalized_task.get("channel") == "xiaohongshu":
                _prepare_xhs_publish_content(page, task_meta)
                _random_sleep(page, 600, 1200)

            # Channel default selectors can be overridden by task.meta_jsonb.publish_selector.
            default_publish_selector = {
                "xiaohongshu": "button:has-text('发布')",
                "wechat_mp": "button:has-text('发表')",
            }.get(normalized_task["channel"])
            publish_selector = str(task_meta.get("publish_selector") or "").strip()
            selector_candidates = [publish_selector] if publish_selector else []
            if default_publish_selector:
                selector_candidates.append(default_publish_selector)
            selector_candidates.extend(
                [
                    "button:has-text('发布笔记')",
                    "button:has-text('发布文章')",
                    "button:has-text('发表')",
                ]
            )
            selector_candidates = [x for x in selector_candidates if x]
            if not selector_candidates:
                raise RuntimeError("publish_selector_missing")
            final_publish_selectors = selector_candidates + [
                "button:has-text('去发布')",
                "button:has-text('确认发布')",
                "button:has-text('发布并同步')",
                "button:has-text('确认')",
                "button:has-text('完成')",
            ]
            next_step_selectors = [
                "button.submit:not([disabled]):not(.disabled)",
                "button:has-text('下一步'):not([disabled]):not(.disabled)",
                "button:has-text('一键排版'):not([disabled]):not(.disabled)",
                "button.next-btn:not([disabled]):not(.disabled)",
                "button:has-text('去发布'):not([disabled]):not(.disabled)",
                ".editor-item.active",
                ".editor-cards-wrapper .editor-item",
            ]

            clicked = False
            deadline = time.time() + (90 if normalized_task.get("channel") == "xiaohongshu" else 20)
            while time.time() < deadline:
                if normalized_task.get("channel") == "xiaohongshu":
                    _xhs_accept_publish_dialog_if_any(page)
                    if _xhs_click_primary_publish(page):
                        clicked = True
                        break
                if _try_click_first(page, final_publish_selectors, timeout=2500):
                    clicked = True
                    break
                advanced = _try_click_first(page, next_step_selectors, timeout=2200)
                if advanced:
                    _random_sleep(page, 900, 1800)
                    continue
                page.wait_for_timeout(1000)
            if not clicked:
                raise RuntimeError(f"publish_click_failed:{' | '.join(selector_candidates[:3])}")
            _random_sleep(page, 1200, 2400)

            if normalized_task.get("channel") == "xiaohongshu":
                confirmed = False
                for _ in range(15):
                    _xhs_accept_publish_dialog_if_any(page)
                    if _xhs_publish_confirmed(page):
                        confirmed = True
                        break
                    _xhs_click_primary_publish(page)
                    page.wait_for_timeout(1000)
                if not confirmed:
                    recovered = _xhs_backfill_identity(
                        page,
                        network_identities,
                        expected_title=str(task_meta.get("title") or ""),
                    )
                    if _is_valid_xhs_note_id(str(recovered.get("feed_id") or "").strip()) or bool(
                        recovered.get("title_matched")
                    ):
                        confirmed = True
                        network_identities.append(recovered)
                    else:
                        raise RuntimeError("publish_not_confirmed_after_click")

            result = {
                "status": "success",
                "remote_post_id": f"pw-{normalized_task['id']}-a{attempt}",
                "published_url": page.url,
                "attempt": attempt,
            }
            network_identity = _choose_best_xhs_identity(network_identities)
            merged = {**result, **network_identity, **extract_publish_identity({**result, **network_identity})}
            if normalized_task.get("channel") == "xiaohongshu":
                feed_id = str(merged.get("feed_id") or "").strip()
                if not _is_valid_xhs_note_id(feed_id):
                    recovered = _xhs_backfill_identity(
                        page,
                        network_identities,
                        expected_title=str(task_meta.get("title") or ""),
                    )
                    if recovered:
                        merged = {
                            **result,
                            **recovered,
                            **extract_publish_identity({**result, **recovered}),
                        }
                url_text = str(merged.get("published_url") or "")
                feed_id = str(merged.get("feed_id") or "").strip()
                title_matched = bool(merged.get("title_matched"))
                # Success-page fallback: creator center may return a success page before note id
                # is visible. Mark verification pending and let feedback reconcile backfill identity.
                if (not feed_id) and ("creator.xiaohongshu.com/publish/success" in url_text):
                    if "bind_status=not_bind" in url_text or "bind_status=" in url_text:
                        merged["verification_pending"] = True
                        merged.setdefault("verification_method", "success_page_bind_status")
                        merged.setdefault("identity_pending_reason", "bind_status_no_note_id")
                    else:
                        merged["verification_pending"] = True
                        merged.setdefault("verification_method", "success_page_pending_note_id")
                if not feed_id and title_matched and not merged.get("verification_method"):
                    merged["verification_pending"] = True
                    merged.setdefault("verification_method", "title_match")
            # Keep browser session warm for next operation and avoid rapid-fire transitions.
            _random_sleep(page, 2000, 3000)
            _goto_if_needed(page, _target_url(normalized_task["channel"]), timeout=60_000, min_pause_ms=1000, max_pause_ms=1800)
            return merged
        except Exception as exc:  # noqa: BLE001
            artifact = _capture_failure_artifact(page, normalized_task["id"], attempt, str(exc))
            return _normalize_failed_publish_result({
                "status": "failed",
                "error": str(exc),
                "attempt": attempt,
                "artifact": artifact,
            })
        finally:
            if response_handler is not None:
                try:
                    page.remove_listener("response", response_handler)
                except Exception:  # noqa: BLE001
                    pass


def publish_with_playwright(task: Dict[str, Any]) -> Dict[str, Any]:
    return _publishing_disabled_result()


def _publishing_disabled_result() -> Dict[str, Any]:
    return {
        "status": "blocked",
        "error": "delegated_publishing_disabled",
        "message": "AI 代发布已取消，草稿保留，请自行发布。",
        "attempt": 0,
        "publish_attempts": [],
    }


def publish_with_fallback(task: Dict[str, Any]) -> Dict[str, Any]:
    return _publishing_disabled_result()


def persist_publish_result(client: Client, task: Dict[str, Any], result: Dict[str, Any]) -> None:
    status = "published" if result.get("status") == "success" else "publish_failed"
    now = datetime.now(timezone.utc).isoformat()
    publish_source = str(result.get("publish_method") or "playwright").strip() or "playwright"

    payload = task.get("payload_jsonb") if isinstance(task.get("payload_jsonb"), dict) else {}
    publish = task.get("publish_jsonb") if isinstance(task.get("publish_jsonb"), dict) else {}
    client.table("pipeline_tasks").update(
        {
            "status": status,
            "stage": "feedback_pending" if status == "published" else "publishing",
            "published_at": now if status == "published" else None,
            "updated_at": now,
            "payload_jsonb": {**payload, "last_publish_result": result},
            "publish_jsonb": {**publish, "source": publish_source, "last_result": result},
        }
    ).eq("id", task["id"]).execute()
    client.table("audit_logs").insert(
        {
            "actor": "system:publisher",
            "action": "pipeline.task_published" if status == "published" else "pipeline.task_publish_failed",
            "target_type": "pipeline_task",
            "target_id": task["id"],
            "diff_jsonb": {
                "channel": task["channel"],
                "publish_result": result,
            },
        }
    ).execute()
