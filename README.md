# arxiv-submitter

Schedule and click the final **Submit** button on an arXiv staged submission,
unattended — even with the display off, the lid closed, or the Mac asleep.

Assumes the heavy lifting is done: files uploaded, render checked, metadata
filled in, primary category chosen. All that remains is the final Submit click
on the preview page.

## Install

```bash
cd ~/arxiv-submitter
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
```

## One-time setup

```bash
# Tier 1 — persisted Playwright profile (recommended; survives without password).
arxiv-submit setup-profile
# A visible Chromium opens. Log in to arxiv.org. Press Enter in terminal to save.

# Tier 2 — store email/password in macOS Keychain (optional; fallback).
arxiv-submit setup-keychain --email me@example.com
```

Tier 3 (Chrome cookie reuse) needs no setup — it reads your live Chrome profile
when the first two tiers fail.

## Schedule the submit

```bash
arxiv-submit click 7643826 \
  --at "2026-05-28 14:00:00" --tz America/New_York
```

Defaults: `--tz America/New_York`, `--wake pmset` (AC fallback if no sudo
NOPASSWD), `--lead-time 120` (seconds of warmup before the click).

The submission ID is the only proof of intent. arXiv staged submissions are
user-scoped, so a wrong ID either 404s or surfaces a different one of *your*
in-flight papers. Title is logged for the audit trail but does NOT gate the
click.

## Click precision

`--at` accepts second-level granularity (`HH:MM:SS`). The actual click lands
within **±~100 ms of `--at`** in the typical case.

How: launchd is fundamentally minute-granular (no `Seconds` field in
`StartCalendarInterval`). To get sub-second precision, the tool schedules
launchd to fire `--lead-time` seconds before `--at`, uses that lead time to
finish all the slow work (auth, navigate, reload, locate the Submit Article
button), then *busy-waits on the wall clock* with converging `time.sleep` until
exactly `--at` before issuing `btn.click()`.

Worst-case lateness: if warmup runs over (slow auth, network hiccup), the click
fires as soon as warmup completes — log records the lateness in milliseconds.

Tune `--lead-time`:

- `120s` (default) — comfortably absorbs Playwright launch + pmset wake +
  network latency.
- `60s` — tighter; fine on a warm machine on stable network.
- `300s` — paranoia mode for important fires; longer wake window for pmset.

## Rehearse first

```bash
# Schedule a dry-run 30 minutes before the real fire.
arxiv-submit click 7643826 --at "2026-05-28 13:30" --rehearse
```

Rehearsal navigates, reloads (per arXiv's "you may need to refresh" caveat),
locates the Submit Article button, screenshots the preview page — and *stops
short of clicking*. Use this to catch expired cookies, stale selectors, or a
wrong submission ID before it's too late to fix.

## Other commands

```bash
arxiv-submit list                 # list scheduled jobs
arxiv-submit cancel <label>       # remove a scheduled job
arxiv-submit run-now <id>         # run immediately, no scheduling
```

Per-run logs and screenshots: `~/.arxiv-submitter/runs/<UTC-timestamp>/`.

## What happens at fire time

1. launchd wakes the system (if `pmset` was scheduled).
2. launchd invokes `/usr/bin/caffeinate -i -s python -m arxiv_submitter run-now ...`.
3. `caffeinate -i -s` keeps the system awake for the duration of the click.
4. The Playwright browser runs headless — display state is irrelevant.
5. Auth chain tries persisted profile → Keychain login → Chrome cookies.
6. Clicker navigates, verifies title, screenshots, clicks (or rehearses).
7. macOS notification banner reports success or failure.
8. Full log + screenshots saved to `~/.arxiv-submitter/runs/`.

## Caveats

- `pmset schedule` needs sudo NOPASSWD or you'll fall back to "machine must be
  on AC, lid open" mode. Set up via `visudo`:
  `<your-user> ALL=(root) NOPASSWD: /usr/bin/pmset`.
- If arXiv enables 2FA on your account, tier 2 (Keychain fresh login) will
  break — currently builds the form fill assuming password-only. Tiers 1 and 3
  carry the 2FA cookie through and still work.
- The Submit-button selector and URL pattern are best-effort defaults. Run
  `--rehearse` once against a real staged submission to confirm both before
  the real fire.
