"""Three-tier authentication chain for arxiv.org via Playwright.

Tier 1 — persisted Playwright profile (cleanest; cookies + storage survive across runs).
Tier 2 — fresh login with credentials from macOS Keychain (works without prior browser state).
Tier 3 — copy arxiv.org cookies from a live Chrome profile into a fresh headless context.

`authenticate()` tries them in order, returns the first tier that proves logged-in.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from playwright.sync_api import (
    BrowserContext,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from . import chrome_cookies, notify

DEFAULT_PROFILE_DIR = Path.home() / ".arxiv-submitter" / "profile"
ARXIV_LOGIN_URL = "https://arxiv.org/login"
ARXIV_USER_URL = "https://arxiv.org/user"  # logged-in landing page


@dataclass
class AuthedSession:
    context: BrowserContext
    tier: str
    cleanup: Callable[[], None]


def _keychain_get(email: str) -> str:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", "arxiv-submitter",
         "-a", email, "-w"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# Auth-success probe — decides whether a tier actually logged us in.
# Returning True means "context is authenticated, proceed to click."
# Returning False means "fall through to the next tier."
# ---------------------------------------------------------------------------
def is_authenticated(context: BrowserContext, run_dir: Path) -> bool:
    page = context.new_page()
    try:
        page.goto(ARXIV_USER_URL, wait_until="domcontentloaded", timeout=15_000)
        return _user_probe(page)
    except PWTimeout:
        notify.log(run_dir, "auth probe timed out", level="warn")
        return False
    finally:
        page.close()


def _user_probe(page) -> bool:  # noqa: ANN001
    # Logged-in pages on arxiv.org show "Logout" in the top-right header.
    # A login redirect renders an email/password form instead. Element-based is
    # robust to URL redirects and silent session expiry.
    return page.locator('a:has-text("Logout")').count() > 0


# ---------------------------------------------------------------------------
# Tier 1: persisted Playwright profile.
# ---------------------------------------------------------------------------
def _tier1_persisted_profile(pw: Playwright, profile_dir: Path, run_dir: Path):
    if not profile_dir.exists():
        notify.log(run_dir, f"tier1 skipped: no profile at {profile_dir}")
        return None
    notify.log(run_dir, "tier1: launching persistent_context")
    ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=True)
    if is_authenticated(ctx, run_dir):
        return AuthedSession(ctx, "persisted-profile", ctx.close)
    ctx.close()
    notify.log(run_dir, "tier1: probe failed", level="warn")
    return None


# ---------------------------------------------------------------------------
# Tier 2: fresh login using Keychain creds.
# ---------------------------------------------------------------------------
def _tier2_keychain_login(pw: Playwright, email: str | None, run_dir: Path):
    if not email:
        notify.log(run_dir, "tier2 skipped: no --email provided")
        return None
    try:
        password = _keychain_get(email)
    except subprocess.CalledProcessError:
        notify.log(run_dir, f"tier2 skipped: no Keychain entry for {email}", level="warn")
        return None

    notify.log(run_dir, "tier2: fresh login")
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(ARXIV_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
        page.fill('input[name="username"]', email)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
    except PWTimeout:
        notify.log(run_dir, "tier2: login form timed out", level="warn")
        ctx.close(); browser.close()
        return None
    page.close()

    if is_authenticated(ctx, run_dir):
        return AuthedSession(ctx, "keychain-login", lambda: (ctx.close(), browser.close()))
    ctx.close(); browser.close()
    notify.log(run_dir, "tier2: probe failed after login", level="warn")
    return None


# ---------------------------------------------------------------------------
# Tier 3: extract arxiv.org cookies from Chrome, inject into headless context.
# ---------------------------------------------------------------------------
def _tier3_chrome_cookies(pw: Playwright, run_dir: Path,
                          chrome_profiles: list[Path] | None = None):
    # Try each supplied Chrome profile in order; use the first whose arxiv.org
    # cookies prove logged-in. Falls back to Chrome's "Default" profile when the
    # caller passes nothing, preserving the original single-profile behavior.
    profiles = chrome_profiles or [chrome_cookies.DEFAULT_CHROME_PROFILE]
    for profile in profiles:
        tag = f"tier3 [{profile.name}]"
        try:
            cookies = chrome_cookies.extract_arxiv_cookies(profile)
        except Exception as e:
            notify.log(run_dir, f"{tag} skipped: {e}", level="warn")
            continue
        if not cookies:
            notify.log(run_dir, f"{tag}: no arxiv.org cookies in Chrome profile", level="warn")
            continue

        notify.log(run_dir, f"{tag}: injecting {len(cookies)} Chrome cookies")
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        ctx.add_cookies(cookies)
        if is_authenticated(ctx, run_dir):
            return AuthedSession(ctx, f"chrome-cookies[{profile.name}]",
                                 lambda c=ctx, b=browser: (c.close(), b.close()))
        ctx.close(); browser.close()
        notify.log(run_dir, f"{tag}: probe failed", level="warn")
    return None


# ---------------------------------------------------------------------------
# Orchestrator.
# ---------------------------------------------------------------------------
def authenticate(
    run_dir: Path,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    email: str | None = None,
    chrome_profiles: list[Path] | None = None,
) -> tuple[Playwright, AuthedSession]:
    """Try tiers 1→2→3. Returns (playwright, session) on success; raises on total failure."""
    pw = sync_playwright().start()
    for fn in (
        lambda: _tier1_persisted_profile(pw, profile_dir, run_dir),
        lambda: _tier2_keychain_login(pw, email, run_dir),
        lambda: _tier3_chrome_cookies(pw, run_dir, chrome_profiles),
    ):
        session = fn()
        if session is not None:
            notify.log(run_dir, f"auth OK via tier: {session.tier}")
            return pw, session
    pw.stop()
    raise RuntimeError("All auth tiers failed; see run log.")
