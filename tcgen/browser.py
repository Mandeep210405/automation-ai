"""
browser.py — launch Firefox and handle login sessions like a real browser.

If login is required:
  * a saved session (cookies + localStorage + IndexedDB) is reused if you want,
  * otherwise you log in yourself in the visible window (works with OTP / SSO /
    captcha), press ENTER, and the session is saved to sessions/<site>.json.
Passwords are never stored by this tool; only the session the site gives you.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout


def session_path(cfg, url: str) -> Path:
    host = urlsplit(url).netloc or "site"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    p = Path(cfg.SESSIONS_DIR)
    p.mkdir(exist_ok=True)
    return p / f"{safe}.json"


def save_session(context, path: Path):
    try:
        context.storage_state(path=str(path), indexed_db=True)   # Playwright >= 1.51
    except TypeError:
        context.storage_state(path=str(path))


def ask_yes_no(question: str, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        ans = input(f"{question} {hint}: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def wait_until_loaded(page, cfg):
    for state, timeout in (("domcontentloaded", cfg.NAVIGATION_TIMEOUT_MS), ("networkidle", 10_000)):
        try:
            page.wait_for_load_state(state, timeout=timeout)
        except (PWTimeout, PWError):
            pass
    page.wait_for_timeout(cfg.SETTLE_EXTRA_MS)


def open_session(pw, cfg, url: str, login_required: bool):
    """Return (browser, context, page, session_file or None)."""
    browser = pw.firefox.launch(headless=cfg.HEADLESS, slow_mo=cfg.SLOW_MO_MS)
    ctx_args = {"viewport": cfg.VIEWPORT}
    sess = None

    if login_required:
        sess = session_path(cfg, url)
        if sess.exists() and ask_yes_no(f"Saved session found ({sess}). Reuse it?", True):
            ctx_args["storage_state"] = str(sess)
        context = browser.new_context(**ctx_args)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=cfg.NAVIGATION_TIMEOUT_MS)
        wait_until_loaded(page, cfg)
        print("\n" + "=" * 64)
        print(" LOGIN: look at the Firefox window.")
        print(" - If you are NOT logged in, log in there now (OTP/SSO is fine).")
        print(" - If you are already logged in, do nothing.")
        print("=" * 64)
        input(" Press ENTER here when you are logged in... ")
        save_session(context, sess)
        print(f" Session saved -> {sess}")
        # Start the test from the requested URL (login may have redirected elsewhere).
        page.goto(url, wait_until="domcontentloaded", timeout=cfg.NAVIGATION_TIMEOUT_MS)
    else:
        context = browser.new_context(**ctx_args)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=cfg.NAVIGATION_TIMEOUT_MS)

    context.set_default_timeout(cfg.ACTION_TIMEOUT_MS)
    wait_until_loaded(page, cfg)
    return browser, context, page, sess
