"""The Autopilot page from any network, without opening this PC to the internet.

Titip itself is only reachable on your own network. For the Autopilot page there is a second way: a
small relay (worker/titip-relay.js, a Cloudflare Worker you deploy yourself) that holds two things
per PC, both encrypted before they leave it:

    PC  --state-->  relay  --state-->  phone        what Autopilot did and what waits for you
    PC  <--orders--  relay  <--orders--  phone       pause, send this draft, don't answer

The key (AES-256-GCM) is made on this PC and reaches the phone only inside the QR code, after the
"#" of the address, which a browser never sends to any server. The relay sees ciphertext and its
size, and can at worst lose or delay it. Orders older than ORDER_MAX_AGE_S or seen before are dropped.

Only Autopilot data (Gmail) goes this way. Outlook mail never does.

Settings live in <home>/remote.json: {"relay": "https://...workers.dev", "channel", "key", "write"}.
Without a relay address this module does nothing.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PAGE = "https://juw12.github.io/titip/auto.html"
POLL_S = 20
PUBLISH_GAP_S = 30  # the relay's free plan allows 1,000 writes a day; a burst of changes becomes one write
HEARTBEAT_S = 30 * 60
ORDER_MAX_AGE_S = 15 * 60
MAX_EVENTS = 40
MAX_TEXT = 700
TIMEOUT_S = 25
ALLOWED_ORDERS = ("switch", "mode", "check", "send", "drop")


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal(key: bytes, data: dict) -> str:
    """JSON -> base64url(nonce + ciphertext + tag), the layout WebCrypto's AES-GCM uses too."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    plain = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b64(nonce + AESGCM(key).encrypt(nonce, plain, None))


def unseal(key: bytes, text: str) -> dict | None:
    """None for anything that was not sealed with this key."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = unb64(text.strip())
        data = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8"))
    except (InvalidTag, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def slim(snapshot: dict) -> dict:
    """What the phone needs: the newest events, long texts cut."""
    cut = lambda text: text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rstrip() + " ..."  # noqa: E731
    events = [{**event, "text": cut(event.get("text", ""))} for event in snapshot.get("events", [])[:MAX_EVENTS]]
    return {**snapshot, "events": events}


def http(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "titip", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
            return reply.status, reply.read(600_000)
    except urllib.error.HTTPError as error:
        return error.code, b""


class Remote:
    def __init__(self, home: Path, autopilot, send=http, clock=time.time):
        self.path = Path(home) / "remote.json"
        self.autopilot = autopilot
        self.send = send
        self.clock = clock
        self.error = ""
        self.last_publish = 0.0
        self.last_contact = 0.0
        self._published = ""
        self._dirty = threading.Event()
        self._seen: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.settings = self._load()

    # ------------------------------------------------------------ settings
    def _load(self) -> dict:
        settings = {"relay": "", "page": PAGE, "channel": "", "key": "", "write": ""}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            settings.update({name: str(saved[name]) for name in settings if saved.get(name)})
        except (OSError, ValueError, AttributeError):
            pass
        if settings["relay"] and not (settings["channel"] and settings["key"] and settings["write"]):
            settings.update(self._fresh())
            self._save(settings)
        return settings

    @staticmethod
    def _fresh() -> dict:
        return {"channel": secrets.token_urlsafe(16), "key": b64(os.urandom(32)), "write": secrets.token_urlsafe(24)}

    def _save(self, settings: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(settings, indent=1), encoding="utf-8")

    @property
    def ready(self) -> bool:
        return bool(self.settings["relay"])

    @property
    def key(self) -> bytes:
        return unb64(self.settings["key"])

    def _url(self, what: str) -> str:
        return f"{self.settings['relay'].rstrip('/')}/c/{self.settings['channel']}/{what}"

    def link(self) -> str:
        """The address the phone opens. Everything after # stays in the phone's browser."""
        if not self.ready:
            return ""
        ticket = {"u": self.settings["relay"].rstrip("/"), "c": self.settings["channel"], "k": self.settings["key"]}
        return f"{self.settings['page']}#r={b64(json.dumps(ticket, separators=(',', ':')).encode('utf-8'))}"

    def renew(self) -> None:
        """A new channel and key: every phone paired with the old code is cut off."""
        old = dict(self.settings)
        self.settings.update(self._fresh())
        self._save(self.settings)
        self._published = ""
        try:
            self.send("DELETE", f"{old['relay'].rstrip('/')}/c/{old['channel']}", None, {"X-Titip-Write": old["write"]})
        except OSError:
            pass  # the old channel expires on the relay by itself
        self.touch()

    def status(self) -> dict:
        return {
            "ready": self.ready,
            "error": self.error,
            "lastContact": int(self.last_contact),
            "running": self._thread is not None,
        }

    # ------------------------------------------------------------ the two directions
    def touch(self) -> None:
        self._dirty.set()

    def publish(self, force: bool = False) -> bool:
        """Send the current state if it changed. True when the relay took it."""
        snapshot = slim(self.autopilot.snapshot())
        comparable = json.dumps({**snapshot, "now": 0}, sort_keys=True)
        if not force and comparable == self._published:
            return False
        snapshot["published"] = int(self.clock())
        status, _ = self.send(
            "PUT", self._url("state"), seal(self.key, snapshot).encode("ascii"),
            {"X-Titip-Write": self.settings["write"], "Content-Type": "text/plain"},
        )
        if status != 200:
            raise OSError(f"the relay answered {status}")
        self._published = comparable
        self.last_publish = self.last_contact = self.clock()
        return True

    def fetch_orders(self) -> list[dict]:
        """Orders the phone left, decrypted and checked; they are removed from the relay."""
        status, body = self.send("POST", self._url("orders/take"), b"", {"X-Titip-Write": self.settings["write"]})
        if status == 404:  # the relay does not know this PC yet: it learns it from the first state
            return []
        if status != 200:
            raise OSError(f"the relay answered {status}")
        self.last_contact = self.clock()
        try:
            sealed = json.loads(body.decode("utf-8")).get("orders", [])
        except (ValueError, AttributeError):
            return []
        orders = []
        for text in sealed if isinstance(sealed, list) else []:
            order = unseal(self.key, str(text))
            if order and self._fresh_order(order):
                orders.append(order)
        return orders

    def _fresh_order(self, order: dict) -> bool:
        identity = str(order.get("nonce", ""))
        try:
            age = self.clock() - float(order.get("at", 0))
        except (TypeError, ValueError):
            return False
        if not identity or identity in self._seen or not -120 <= age <= ORDER_MAX_AGE_S:
            return False
        if order.get("name") not in ALLOWED_ORDERS:
            return False
        self._seen.append(identity)
        del self._seen[:-300]
        return True

    def carry_out(self, order: dict) -> None:
        details = {name: order[name] for name in ("on", "mode", "id", "text") if name in order}
        try:
            self.autopilot.command(order["name"], **details)
        except ValueError:
            pass  # the mail is no longer waiting, or the order made no sense: the next state shows it

    def step(self) -> None:
        """One round: take orders, then publish if something changed or the heartbeat is due."""
        for order in self.fetch_orders():
            self.carry_out(order)
        due = self.clock() - self.last_publish
        if (self._dirty.is_set() and due >= PUBLISH_GAP_S) or due >= HEARTBEAT_S or not self._published:
            self._dirty.clear()
            self.publish(force=due >= HEARTBEAT_S)

    # ------------------------------------------------------------ the thread
    def start(self) -> None:
        if self._thread is None and self.ready:
            self._thread = threading.Thread(target=self._run, daemon=True, name="titip-remote")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
                self.error = ""
            except Exception as error:  # no network, proxy trouble: try again next round
                self.error = f"The relay could not be reached ({type(error).__name__})."
            self._stop.wait(POLL_S)
