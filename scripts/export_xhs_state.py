from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open Xiaohongshu pages and export Playwright storage state after manual login."
    )
    parser.add_argument(
        "--url",
        default="https://creator.xiaohongshu.com",
        help="Target URL to open for manual login.",
    )
    parser.add_argument(
        "--verify-web",
        action="store_true",
        help="Also verify login on www.xiaohongshu.com before saving state.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=60,
        help="Seconds to wait for manual QR-code login before exporting storage state.",
    )
    parser.add_argument(
        "--output",
        default="xhs_storage_state.json",
        help="Output storage-state file path.",
    )
    parser.add_argument(
        "--user-data-dir",
        default="",
        help="Optional persistent user data dir for login bootstrap context.",
    )
    parser.add_argument(
        "--save-on-timeout",
        action="store_true",
        help="Save storage state even if login was not explicitly detected.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    user_data_dir = Path(args.user_data_dir).expanduser().resolve() if str(args.user_data_dir or "").strip() else None
    if user_data_dir is not None:
        user_data_dir.mkdir(parents=True, exist_ok=True)

    print(f"[export-xhs-state] Opening browser at: {args.url}")
    print("[export-xhs-state] Please complete login manually in the opened browser window.")
    print(f"[export-xhs-state] Waiting up to {args.wait_seconds} seconds for login success...")

    with sync_playwright() as p:
        browser = None
        if user_data_dir is not None:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                slow_mo=80,
                viewport={"width": 1440, "height": 960},
            )
        else:
            browser = p.chromium.launch(headless=False, slow_mo=80)
            context = browser.new_context(viewport={"width": 1440, "height": 960})
        creator_page = context.pages[0] if context.pages else context.new_page()
        creator_page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        web_page = context.new_page() if len(context.pages) < 2 else context.pages[1]
        web_page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=60_000)

        detected = False
        for remaining in range(args.wait_seconds, 0, -1):
            creator_ok = _looks_creator_logged_in(context, creator_page)
            web_ok = True
            if args.verify_web:
                web_ok = _looks_web_logged_in(context, web_page)
                if creator_ok and not web_ok and remaining % 6 == 0:
                    try:
                        web_page.reload(wait_until="domcontentloaded", timeout=20_000)
                    except Exception:  # noqa: BLE001
                        pass
            if creator_ok and web_ok:
                detected = True
                print("[export-xhs-state] Creator/Web login detected. Exporting storage state now...")
                creator_page.wait_for_timeout(1200)
                break
            if remaining in {60, 45, 30, 15, 10, 5, 4, 3, 2, 1}:
                suffix = " (creator+web)" if args.verify_web else " (creator)"
                print(f"[export-xhs-state] {remaining}s remaining...{suffix}")
            time.sleep(1)

        if detected or args.save_on_timeout:
            context.storage_state(path=str(output_path))
            print(f"[export-xhs-state] Storage state saved to: {output_path}")
        else:
            print("[export-xhs-state] Login not detected within timeout, keeping previous storage-state file unchanged.")
        context.close()
        if browser is not None:
            browser.close()

    if not detected:
        print("[export-xhs-state] Timeout reached before explicit login detection.")
        return 2
    print("[export-xhs-state] Next step: set PLAYWRIGHT_STORAGE_STATE_PATH to this file path in .env")
    return 0


def _looks_creator_logged_in(context, page) -> bool:
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        url = ""
    try:
        cookies = context.cookies()
    except Exception:  # noqa: BLE001
        cookies = []

    matched = [
        cookie
        for cookie in cookies
        if "xiaohongshu.com" in str(cookie.get("domain") or "")
        and str(cookie.get("name") or "").strip()
    ]
    if not matched:
        return False

    lowered_url = url.lower()
    if "website-login" in lowered_url or "login" in lowered_url:
        return False

    if _page_has_login_modal(page):
        return False

    try:
        logged_in_markers = page.locator(
            ".user-info .name-box, .d-topbar .name-box, .user_avatar, img.user_avatar, "
            ".creator-layout, .creator-content, .creator-tab, button:has-text('发布笔记'), button:has-text('新的创作')"
        )
        for idx in range(min(logged_in_markers.count(), 8)):
            try:
                if logged_in_markers.nth(idx).is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass

    try:
        creator_ready = page.evaluate(
            """() => {
              const hasShell = !!document.querySelector('.creator-layout, .creator-content, .creator-page');
              const hasTabs = document.querySelectorAll('.header-tabs .creator-tab').length > 0;
              return hasShell || hasTabs;
            }"""
        )
        if creator_ready:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _page_has_login_modal(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .login-container, .login-panel, .login-modal'));
                  const visibleDialog = dialogs.find((el) => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 200 && rect.height > 120 && rect.bottom > 0 && rect.top < window.innerHeight;
                  });
                  if (!visibleDialog) return false;
                  const text = (visibleDialog.textContent || '').replace(/\\s+/g, ' ');
                  if (/登录|扫码|手机号|验证码/.test(text)) return true;
                  const hasInput = !!visibleDialog.querySelector('input');
                  const hasCanvasOrQr = !!visibleDialog.querySelector('canvas, img[src*="qr"], img[src*="qrcode"]');
                  return hasInput || hasCanvasOrQr;
                }"""
            )
        )
    except Exception:  # noqa: BLE001
        return False


def _looks_web_logged_in(context, page) -> bool:
    try:
        cookies = context.cookies()
    except Exception:  # noqa: BLE001
        cookies = []
    matched = [
        cookie
        for cookie in cookies
        if "xiaohongshu.com" in str(cookie.get("domain") or "")
        and str(cookie.get("name") or "").strip()
    ]
    if not matched:
        return False
    if _page_has_login_modal(page):
        return False
    try:
        return bool(
            page.evaluate(
                """() => {
                  const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ');
                  if (/登录探索更多内容|扫码登录|手机号登录/.test(bodyText)) return false;
                  const leftLogin = Array.from(document.querySelectorAll('button, a, div')).find((el) => {
                    const text = (el.textContent || '').trim();
                    if (text !== '登录') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.left < 360 && rect.top < window.innerHeight * 0.85;
                  });
                  if (leftLogin) return false;
                  const topSearch = document.querySelector('input[placeholder*="搜索"]');
                  const creatorLink = Array.from(document.querySelectorAll('a, div, span')).find((el) => {
                    const text = (el.textContent || '').trim();
                    return text.includes('创作中心');
                  });
                  return !!topSearch || !!creatorLink;
                }"""
            )
        )
    except Exception:  # noqa: BLE001
        return not _page_has_login_modal(page)


if __name__ == "__main__":
    sys.exit(main())
