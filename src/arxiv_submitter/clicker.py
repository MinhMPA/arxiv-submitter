"""Navigate to a staged arXiv submission and click the final "Submit Article" button.

The only proof-of-intent gate is the submission ID itself — arXiv staged
submissions are user-scoped, so a wrong ID either 404s or surfaces a different
one of your own in-flight papers. Title is extracted only for the audit log
and the success banner; it does NOT gate the click.

We reload the page before locating the button because arXiv's own instructions
warn: "Depending on your browser settings, you may need to refresh the page to
see the Submit Article button."
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout

from . import notify
from .auth import AuthedSession

DEFAULT_URL_TEMPLATE = "https://arxiv.org/submit/{id}/preview"


def _wait_until(target_unix: float) -> float:
    """Sleep until target_unix with sub-100ms precision. Returns actual lateness in s."""
    while True:
        remaining = target_unix - time.time()
        if remaining <= 0:
            return -remaining  # already past target → lateness
        # Converge on target without busy-looping the CPU.
        time.sleep(min(remaining * 0.5, 0.5))


@dataclass
class SubmitResult:
    success: bool
    tier: str
    observed_title: str
    final_url: str
    screenshot_path: Path
    reason: str = ""


def _find_submit_button(page):
    # Confirmed via rehearsal diagnostic on 2026-05-27: arXiv renders the final
    # submission control as <input type="submit" value="Submit">, despite its
    # help text calling it "Submit Article". Use the exact-value match first;
    # the rest are fallbacks against future arXiv UI churn.
    candidates = [
        'input[type="submit"][value="Submit"]',
        'input[type="submit"][value*="Submit Article"]',
        'button:has-text("Submit Article")',
        'a:has-text("Submit Article")',
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        if loc.count() and loc.is_visible():
            return loc, sel
    return None, None


def _diagnose_buttons(page) -> str:
    """Enumerate visible button/input/anchor elements with their text."""
    js = """() => {
      const out = [];
      const elements = document.querySelectorAll(
        'button, input[type=submit], input[type=button], a[href]'
      );
      for (const el of elements) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 100);
        if (!text) continue;
        out.push(`<${el.tagName.toLowerCase()}> "${text}"`);
      }
      return out.join('\\n');
    }"""
    try:
        return page.evaluate(js) or "(no candidate elements found)"
    except Exception as e:
        return f"(diagnostic JS failed: {e})"


def _extract_title(page) -> str:
    """Best-effort title scrape for the audit log / banner. Not a safety gate."""
    candidates = [
        'h1.title',
        '.preview-abstract h1',
        'h1',
        'th:has-text("Title:") + td',
        'dt:has-text("Title:") + dd',
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        if loc.count():
            txt = loc.inner_text().strip()
            if txt:
                return txt
    return ""


def submit(
    session: AuthedSession,
    submission_id: str,
    rehearse: bool,
    run_dir: Path,
    url_template: str = DEFAULT_URL_TEMPLATE,
    target_unix: float | None = None,
) -> SubmitResult:
    url = url_template.format(id=submission_id)
    page = session.context.new_page()
    notify.log(run_dir, f"navigating to {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except PWTimeout:
        return SubmitResult(False, session.tier, "", page.url,
                            run_dir / "screenshots" / "nav_fail.png",
                            reason="navigation timeout")

    pre_shot = run_dir / "screenshots" / "pre_click.png"
    page.screenshot(path=str(pre_shot), full_page=True)

    observed = _extract_title(page)
    notify.log(run_dir, f"observed title: {observed!r}")

    # arXiv: "you may need to refresh the page to see the Submit Article button"
    notify.log(run_dir, "reloading to surface Submit Article button")
    page.reload(wait_until="domcontentloaded", timeout=20_000)

    btn, sel = _find_submit_button(page)
    if btn is None:
        notify.log(run_dir, "Submit Article button not found", level="error")
        diag = _diagnose_buttons(page)
        notify.log(run_dir, f"visible button-like elements on page:\n{diag}",
                   level="warn")
        return SubmitResult(False, session.tier, observed, page.url, pre_shot,
                            reason="Submit Article button not found on page")
    notify.log(run_dir, f"submit button located via selector: {sel}")

    if rehearse:
        notify.log(run_dir, "REHEARSAL: skipping click")
        return SubmitResult(True, session.tier, observed, page.url, pre_shot,
                            reason="rehearsal — no click performed")

    if target_unix is not None:
        remaining = target_unix - time.time()
        if remaining > 0:
            notify.log(run_dir, f"holding button; waiting {remaining:.3f}s for target")
            lateness = _wait_until(target_unix)
            notify.log(run_dir, f"target reached; lateness={lateness*1000:.0f}ms")
        else:
            notify.log(run_dir,
                       f"already past target by {-remaining:.3f}s — clicking immediately",
                       level="warn")

    notify.log(run_dir, "CLICKING Submit Article")
    click_t0 = time.time()
    btn.click()
    notify.log(run_dir, f"click dispatched at unix={click_t0:.3f}")
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PWTimeout:
        notify.log(run_dir, "post-click networkidle timed out (proceeding)", level="warn")

    post_shot = run_dir / "screenshots" / "post_click.png"
    page.screenshot(path=str(post_shot), full_page=True)
    notify.log(run_dir, f"final URL: {page.url}")

    return SubmitResult(True, session.tier, observed, page.url, post_shot,
                        reason="submitted")
