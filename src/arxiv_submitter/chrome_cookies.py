"""Extract arxiv.org cookies from Chrome's local cookie DB on macOS.

Chrome encrypts cookie values with AES-128-CBC, key derived via PBKDF2-HMAC-SHA1
from the password in Keychain item "Chrome Safe Storage" (salt="saltysalt",
iterations=1003, key length=16). IV is 16 bytes of 0x20.

Reads a snapshot copy of the cookie DB so we don't fight Chrome's SQLite lock.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2

_CHROME_TIME_EPOCH_DIFF = 11644473600  # seconds between 1601-01-01 and 1970-01-01

DEFAULT_CHROME_PROFILE = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default"


def _get_chrome_key() -> bytes:
    out = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
        check=True, capture_output=True, text=True,
    )
    password = out.stdout.strip().encode("utf-8")
    return PBKDF2(password, b"saltysalt", dkLen=16, count=1003)


def _decrypt(value: bytes, key: bytes) -> str:
    # Chrome on macOS prefixes encrypted values with "v10" or "v11".
    if value[:3] not in (b"v10", b"v11"):
        return value.decode("utf-8", errors="replace")
    cipher = AES.new(key, AES.MODE_CBC, IV=b" " * 16)
    decrypted = cipher.decrypt(value[3:])
    pad_len = decrypted[-1]
    return decrypted[:-pad_len].decode("utf-8", errors="replace")


def _samesite(code: int) -> str:
    # Chromium net::CookieSameSite: -1=unspec, 0=None, 1=Lax, 2=Strict
    return {0: "None", 1: "Lax", 2: "Strict"}.get(code, "Lax")


def extract_arxiv_cookies(chrome_profile: Path = DEFAULT_CHROME_PROFILE) -> list[dict]:
    """Return Playwright-compatible cookie dicts for *.arxiv.org from Chrome."""
    src = chrome_profile / "Cookies"
    if not src.exists():
        raise FileNotFoundError(f"Chrome cookie DB not found at {src}")

    key = _get_chrome_key()

    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "Cookies"
        shutil.copy2(src, snap)
        conn = sqlite3.connect(snap)
        try:
            rows = conn.execute(
                "SELECT host_key, name, encrypted_value, path, expires_utc, "
                "is_secure, is_httponly, samesite "
                "FROM cookies WHERE host_key LIKE '%arxiv.org'"
            ).fetchall()
        finally:
            conn.close()

    cookies: list[dict] = []
    for host, name, enc, path, expires_utc, secure, httponly, ss in rows:
        value = _decrypt(enc, key)
        if expires_utc == 0:
            expires = -1.0  # session cookie
        else:
            expires = expires_utc / 1_000_000 - _CHROME_TIME_EPOCH_DIFF
        cookies.append({
            "name": name,
            "value": value,
            "domain": host,
            "path": path,
            "expires": expires,
            "httpOnly": bool(httponly),
            "secure": bool(secure),
            "sameSite": _samesite(ss),
        })
    return cookies
