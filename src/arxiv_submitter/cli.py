"""CLI for arxiv-submitter."""
from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, clicker, notify, scheduler


# ---------- time parsing ----------------------------------------------------

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?[+-]\d{2}:\d{2}$")


def _parse_when(s: str, tz_name: str) -> _dt.datetime:
    """Accept ISO-with-offset OR 'YYYY-MM-DD HH:MM[:SS]' in tz_name."""
    if _ISO_RE.match(s):
        return _dt.datetime.fromisoformat(s)
    fmt = "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M"
    dt = _dt.datetime.strptime(s, fmt)
    return dt.replace(tzinfo=ZoneInfo(tz_name))


_CHROME_DIR = (Path.home() / "Library" / "Application Support"
               / "Google" / "Chrome")


def _resolve_chrome_profiles(vals: list[str] | None) -> list[Path]:
    """Turn --chrome-profile values into dirs. A bare name (no '/') is taken
    relative to the default macOS Chrome dir; anything with a '/' is a path."""
    out: list[Path] = []
    for v in vals or []:
        out.append(Path(v).expanduser() if "/" in v else _CHROME_DIR / v)
    return out


# ---------- subcommands -----------------------------------------------------

def cmd_setup_profile(args: argparse.Namespace) -> int:
    """One-time: open a visible browser, let user log in, persist cookies."""
    from playwright.sync_api import sync_playwright
    profile_dir = Path(args.profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"Launching Chromium with persistent profile at {profile_dir}")
    print("Log in to arxiv.org, then press Enter here to save and quit.")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=False)
        page = ctx.new_page()
        page.goto("https://arxiv.org/login")
        try:
            input()
        finally:
            ctx.close()
    print("Profile saved.")
    return 0


def cmd_setup_keychain(args: argparse.Namespace) -> int:
    password = getpass.getpass(f"arXiv password for {args.email}: ")
    subprocess.run(
        ["security", "add-generic-password",
         "-s", "arxiv-submitter", "-a", args.email,
         "-w", password, "-U"],
        check=True,
    )
    print(f"Stored password for {args.email} in Keychain (service=arxiv-submitter).")
    return 0


def cmd_click(args: argparse.Namespace) -> int:
    """Schedule a future click via launchd (+ optional pmset wake)."""
    target = _parse_when(args.at, args.tz)
    target_local = target.astimezone()
    fire_local = target_local - _dt.timedelta(seconds=args.lead_time)

    # launchd StartCalendarInterval is MINUTE-granular: it drops the seconds and
    # fires at the minute boundary (e.g. fire=20:18:58 -> launchd fires 20:18:00).
    # A one-shot whose minute boundary has already passed never runs. Validate
    # against that truncated boundary (not the fire second), with a small margin
    # so we don't lose a race loading the plist right at the boundary.
    fire_minute = fire_local.replace(second=0, microsecond=0)
    now = _dt.datetime.now(_dt.timezone.utc)
    margin = _dt.timedelta(seconds=15)
    if fire_minute <= now + margin:
        secs = (target_local - now).total_seconds()
        print(f"ERROR: launchd fires at the minute boundary {fire_minute.isoformat()}, "
              f"which is not safely in the future.")
        print(f"  Target is {secs:.0f}s away; fire = target - {args.lead_time}s lead-time, "
              f"then rounded DOWN to the minute.")
        print(f"  A launchd one-shot past its minute never runs. Choose one:")
        print(f"    - pick --at at least ~{args.lead_time + 90}s in the future "
              f"(so the fire minute is comfortably ahead), or")
        print(f"    - lower --lead-time, or")
        print(f"    - submit immediately (no scheduling):  arxiv-submit run-now {args.id}")
        return 1

    label = f"{args.id}-{target_local.strftime('%Y%m%d%H%M%S')}"
    log_dir = Path.home() / ".arxiv-submitter" / "launchd-logs"

    argv = ["run-now", args.id, "--at", target.isoformat()]
    if args.rehearse:
        argv.append("--rehearse")
    if args.email:
        argv += ["--email", args.email]
    if args.url_template:
        argv += ["--url-template", args.url_template]
    if args.profile_dir:
        argv += ["--profile-dir", args.profile_dir]
    for cp in args.chrome_profile or []:
        argv += ["--chrome-profile", cp]

    plist_path = scheduler.install_launchd(label, fire_local, argv, log_dir)
    print(f"Scheduled launchd job: {plist_path}")
    print(f"Target click:           {target_local.isoformat()} (local)")
    print(f"                        {target.isoformat()} ({args.tz})")
    print(f"Launchd fires:          {fire_local.isoformat()} ({args.lead_time}s lead time)")
    print(f"Expected click window:  target ± ~100ms (warmup absorbs jitter)")

    if args.wake == "pmset":
        ok, msg = scheduler.schedule_pmset_wake(fire_local)
        if ok:
            print(f"Wake:                   {msg}")
        else:
            print(f"Wake:                   pmset FAILED ({msg})")
            print("Falling back: keep Mac on AC power and lid open at fire time.")
    elif args.wake == "ac":
        print("Wake:                   user-managed — keep Mac on AC, lid open.")
    else:
        print("Wake:                   disabled (--wake=off).")

    print(f"Cancel with: arxiv-submit cancel {label}")
    return 0


def cmd_run_now(args: argparse.Namespace) -> int:
    """Actually perform the click — invoked by launchd at fire time, or by user."""
    run_dir = notify.new_run_dir()
    target_unix: float | None = None
    if args.at:
        target_unix = _parse_when(args.at, args.tz).timestamp()
    notify.log(run_dir,
               f"run-now id={args.id} rehearse={args.rehearse} "
               f"target={args.at or 'immediate'}")
    pw = None
    try:
        profile_dir = (Path(args.profile_dir).expanduser()
                       if args.profile_dir else auth.DEFAULT_PROFILE_DIR)
        chrome_profiles = _resolve_chrome_profiles(args.chrome_profile)
        pw, session = auth.authenticate(run_dir, profile_dir, args.email,
                                        chrome_profiles or None)
        result = clicker.submit(
            session, args.id, args.rehearse,
            run_dir, args.url_template or clicker.DEFAULT_URL_TEMPLATE,
            target_unix=target_unix,
        )
        session.cleanup()
    except Exception as e:
        notify.log(run_dir, f"FATAL: {e}", level="error")
        notify.banner("arXiv submit FAILED", str(e), sound="Basso")
        return 2
    finally:
        if pw is not None:
            pw.stop()

    if result.success and not args.rehearse:
        notify.banner("arXiv submitted",
                      f"id={args.id} tier={result.tier}\n{result.final_url}",
                      sound="Glass")
        return 0
    if result.success and args.rehearse:
        notify.banner("arXiv rehearsal OK",
                      f"id={args.id} would submit '{result.observed_title}'",
                      sound="Tink")
        return 0
    notify.banner("arXiv submit FAILED",
                  f"{result.reason}\nSee {result.screenshot_path}",
                  sound="Basso")
    return 1


def cmd_list(_: argparse.Namespace) -> int:
    jobs = scheduler.list_jobs()
    if not jobs:
        print("No scheduled arxiv-submit jobs.")
        return 0
    for j in jobs:
        print(j)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    scheduler.uninstall_launchd(args.label)
    print(f"Cancelled: {args.label}")
    return 0


# ---------- argparse --------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arxiv-submit",
                                description=__doc__ or "arxiv submitter")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup-profile",
                        help="One-time interactive login → persisted profile.")
    sp.add_argument("--profile-dir", default=str(auth.DEFAULT_PROFILE_DIR))
    sp.set_defaults(func=cmd_setup_profile)

    sk = sub.add_parser("setup-keychain", help="Store arXiv password in Keychain.")
    sk.add_argument("--email", required=True)
    sk.set_defaults(func=cmd_setup_keychain)

    def add_common(x: argparse.ArgumentParser) -> None:
        x.add_argument("id", help="7-digit arXiv submission ID")
        x.add_argument("--rehearse", action="store_true",
                       help="Do everything EXCEPT the final click.")
        x.add_argument("--email", help="For tier-2 (Keychain) auth fallback.")
        x.add_argument("--url-template", default=None,
                       help=f"Default: {clicker.DEFAULT_URL_TEMPLATE}")
        x.add_argument("--profile-dir", default=None)
        x.add_argument("--chrome-profile", action="append", default=None,
                       metavar="NAME_OR_PATH",
                       help="Chrome profile to pull arxiv.org cookies from for "
                            "tier-3 auth. Bare name (e.g. 'Profile 7') resolves "
                            "under the default Chrome dir. Repeatable; tried in "
                            "order until one is logged in.")

    cl = sub.add_parser("click", help="Schedule a future Submit click.")
    add_common(cl)
    cl.add_argument("--at", required=True,
                    help='Target click moment. Either "YYYY-MM-DD HH:MM[:SS]" '
                         '(interpreted in --tz) or ISO-with-offset '
                         '"YYYY-MM-DDTHH:MM:SS+00:00".')
    cl.add_argument("--tz", default="America/New_York")
    cl.add_argument("--lead-time", type=int, default=120, metavar="SECONDS",
                    help="Seconds before --at to start warmup (auth + navigate "
                         "+ locate button). Default 120. The script then "
                         "busy-waits until --at and clicks with ~100ms precision.")
    cl.add_argument("--wake", choices=("pmset", "ac", "off"), default="pmset")
    cl.set_defaults(func=cmd_click)

    rn = sub.add_parser("run-now",
                        help="Perform the click immediately (used by launchd).")
    add_common(rn)
    rn.add_argument("--at", default=None,
                    help="Optional target click moment for sub-second precision. "
                         "If set, script holds at the Submit button and waits "
                         "until this wall-clock moment.")
    rn.add_argument("--tz", default="America/New_York")
    rn.set_defaults(func=cmd_run_now)

    ls = sub.add_parser("list", help="List scheduled jobs.")
    ls.set_defaults(func=cmd_list)

    cn = sub.add_parser("cancel", help="Cancel a scheduled job by label.")
    cn.add_argument("label")
    cn.set_defaults(func=cmd_cancel)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
