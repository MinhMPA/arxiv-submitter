"""macOS user-facing notifications and per-run log/screenshot storage."""
from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

RUNS_ROOT = Path.home() / ".arxiv-submitter" / "runs"


def _as_script_string(s: str) -> str:
    """Quote a Python string for safe embedding in AppleScript.

    AppleScript strings are double-quoted; backslashes and double-quotes are
    backslash-escaped. Newlines are converted to ' — ' because AppleScript
    does not interpret \\n inside string literals.
    """
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " — ")
    return '"' + s + '"'


def new_run_dir() -> Path:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = RUNS_ROOT / stamp
    (d / "screenshots").mkdir(parents=True, exist_ok=True)
    return d


def log(run_dir: Path, msg: str, level: str = "info") -> None:
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    line = f"{ts} [{level}] {msg}\n"
    (run_dir / "log.txt").open("a", encoding="utf-8").write(line)
    print(line, end="")


def banner(title: str, body: str, sound: str | None = None) -> None:
    """Show a macOS notification banner. Best-effort — never raises."""
    script = (f"display notification {_as_script_string(body)} "
              f"with title {_as_script_string(title)}")
    if sound:
        script += f" sound name {_as_script_string(sound)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:
        pass
