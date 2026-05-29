"""Schedule the click via launchd + optionally wake the Mac via pmset.

launchd fires `StartCalendarInterval` in *local* time. We accept a tz-aware
datetime and convert. The job runs:

    /usr/bin/caffeinate -i -s <python> -m arxiv_submitter run-now <args...>

So display-off / screensaver is irrelevant (caffeinate keeps system awake,
headless Playwright doesn't need a display), and on battery `pmset schedule`
wakes the Mac if needed.
"""
from __future__ import annotations

import datetime as _dt
import plistlib
import subprocess
import sys
from pathlib import Path

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "com.arxiv-submitter.click"


def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{LABEL_PREFIX}.{label}.plist"


def install_launchd(label: str, when_local: _dt.datetime, argv: list[str],
                    log_dir: Path) -> Path:
    """Write + bootstrap a launchd plist that fires once at `when_local`."""
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": f"{LABEL_PREFIX}.{label}",
        "ProgramArguments": [
            "/usr/bin/caffeinate", "-i", "-s",
            sys.executable, "-m", "arxiv_submitter", *argv,
        ],
        "StartCalendarInterval": {
            "Year": when_local.year,
            "Month": when_local.month,
            "Day": when_local.day,
            "Hour": when_local.hour,
            "Minute": when_local.minute,
        },
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / f"{label}.out.log"),
        "StandardErrorPath": str(log_dir / f"{label}.err.log"),
    }
    path = _plist_path(label)
    with path.open("wb") as f:
        plistlib.dump(plist, f)

    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{plist['Label']}"],
                   capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                   check=True, capture_output=True)
    return path


def schedule_pmset_wake(when_local: _dt.datetime) -> tuple[bool, str]:
    """Schedule `pmset wakeorpoweron` ~60s before the target. Returns (ok, message).

    Uses `sudo -n` (non-interactive). If sudo NOPASSWD isn't set for pmset,
    returns (False, reason) and caller falls back to an AC-required warning.
    """
    wake_at = when_local - _dt.timedelta(seconds=60)
    stamp = wake_at.strftime("%m/%d/%Y %H:%M:%S")
    cmd = ["sudo", "-n", "pmset", "schedule", "wakeorpoweron", stamp]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, f"pmset wake scheduled for {stamp}"
    return False, (proc.stderr or proc.stdout or "pmset failed").strip()


def uninstall_launchd(label: str) -> None:
    path = _plist_path(label)
    if not path.exists():
        return
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL_PREFIX}.{label}"],
                   capture_output=True)
    path.unlink()


def list_jobs() -> list[str]:
    if not LAUNCH_AGENTS.exists():
        return []
    return sorted(
        p.stem.replace(f"{LABEL_PREFIX}.", "")
        for p in LAUNCH_AGENTS.glob(f"{LABEL_PREFIX}.*.plist")
    )
