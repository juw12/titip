"""Pair a phone by QR code, the way Cairn does (this file is adapted from cairn/phone.py, MIT, same author).

The PC shows a QR code for https://<this PC>:47779/pair#<code>. The code is single-use, lasts 5 minutes
and sits in the URL fragment, so it never reaches a log or proxy. The /pair page posts it back and gets
a random device cookie (400 days, renewed while used). Only a hash of each device token is stored, in
~/.titip/devices.json. "Unpair all phones" deletes them, which cuts every phone off at once.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import ipaddress
import json
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

CODE_SECONDS = 5 * 60
COOKIE = "titip_device"
COOKIE_MAX_AGE = 400 * 24 * 3600  # the longest a browser keeps a cookie; renewed on every page load
SEEN_WRITE_SECONDS = 3600
TAILSCALE_EXE = Path(r"C:\Program Files\Tailscale\tailscale.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def device_name(user_agent: str) -> str:
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone")):
        if needle in (user_agent or ""):
            return name
    return "Browser"


class DeviceStore:
    """devices.json: {"devices": [{"id", "hash", "name", "paired", "seen"}]}. Tokens are never stored."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self._written: dict[str, float] = {}

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [d for d in data.get("devices", []) if isinstance(d, dict) and d.get("hash")]
        except (OSError, ValueError):
            return []

    def _save(self, devices: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"devices": devices}, indent=2), encoding="utf-8")

    def devices(self) -> list[dict]:
        with self.lock:
            return [{k: d.get(k) for k in ("id", "name", "paired", "seen")} for d in self._load()]

    def add(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            devices = self._load()
            devices.append({
                "id": secrets.token_hex(4),
                "hash": _hash(token),
                "name": (name or "Phone")[:60],
                "paired": _now(),
                "seen": _now(),
            })
            self._save(devices)
        return token

    def check(self, token: str) -> bool:
        if not token:
            return False
        wanted = _hash(token)
        with self.lock:
            devices = self._load()
            match = next((d for d in devices if hmac.compare_digest(d["hash"], wanted)), None)
            if match is None:
                return False
            if time.monotonic() - self._written.get(match["id"], -SEEN_WRITE_SECONDS) >= SEEN_WRITE_SECONDS:
                match["seen"] = _now()
                self._written[match["id"]] = time.monotonic()
                try:
                    self._save(devices)
                except OSError:
                    pass
            return True

    def clear(self) -> None:
        with self.lock:
            self._save([])


class PairingCodes:
    """Single-use codes shown as QR codes; only their hashes are kept, in memory."""

    def __init__(self, seconds: int = CODE_SECONDS):
        self.seconds = seconds
        self._codes: dict[str, float] = {}
        self._lock = threading.Lock()

    def new(self) -> str:
        code = secrets.token_urlsafe(16)
        with self._lock:
            now = time.monotonic()
            self._codes = {h: t for h, t in self._codes.items() if t > now}
            self._codes[_hash(code)] = now + self.seconds
        return code

    def redeem(self, code: str) -> bool:
        with self._lock:
            expiry = self._codes.pop(_hash(code or ""), None)
        return expiry is not None and expiry >= time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self._codes.clear()


def lan_addresses() -> list[str]:
    """This PC's IPv4 addresses a phone could use, best first: Tailscale, then local networks."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:  # the address of the default route (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            found.add(probe.getsockname()[0])
    except OSError:
        pass

    def rank(address: str) -> tuple:
        ip = ipaddress.ip_address(address)
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return (0, address)
        if ip in ipaddress.ip_network("192.168.0.0/16"):
            return (1, address)
        if ip.is_private:
            return (2, address)
        return (3, address)

    usable = [a for a in found if not ipaddress.ip_address(a).is_loopback and not ipaddress.ip_address(a).is_link_local]
    return sorted(usable, key=rank)


def tailscale_name(run=subprocess.run) -> str:
    """This PC's Tailscale DNS name when Tailscale is running (works from any network), else ''."""
    exe = shutil.which("tailscale") or (str(TAILSCALE_EXE) if TAILSCALE_EXE.exists() else None)
    if not exe:
        return ""
    try:
        result = run([exe, "status", "--json"], capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
        data = json.loads(result.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if data.get("BackendState") != "Running":
        return ""
    return ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")


def qr_svg(text: str) -> str | None:
    """An SVG QR code, or None when the `segno` library isn't installed (the link is still shown)."""
    try:
        import segno
    except ImportError:
        return None
    out = io.BytesIO()
    # svgns: the page parses this as XML, where an <svg> without its namespace is not a picture at all.
    segno.make(text, error="m").save(
        out, kind="svg", xmldecl=False, svgns=True, nl=False, omitsize=True,
        scale=6, border=2, dark="#16202b", light="#ffffff",
    )
    return out.getvalue().decode("utf-8")
