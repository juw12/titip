"""Titip from any network, without opening this PC to the internet.

Titip itself is only reachable on your own network. There is a second way: a small relay
(worker/titip-relay.js, a Cloudflare Worker you deploy yourself) that passes things between this PC
and your phone, all encrypted before they leave either:

    PC  --state-->  relay  --state-->  phone        what Autopilot did and what waits for you
    PC  <--orders--  relay  <--orders--  phone       pause, send this draft, don't answer
    PC  <--asks--  relay  <--asks--  phone           show my inbox, open this mail, send this
    PC  --answers-->  relay  --answers-->  phone     (only while "Mail from anywhere" is switched on)

The key (AES-256-GCM) is made on this PC and reaches the phone only inside the QR code, after the
"#" of the address, which a browser never sends to any server. The relay sees ciphertext and its
size, and can at worst lose or delay it. Orders older than ORDER_MAX_AGE_S or seen before are dropped.

Autopilot data (Gmail) always goes this way once a relay is set. Outlook mail goes this way only
while "mail" is true in the settings (the switch on the PC page; off unless you turn it on). An ask is
answered once: asks seen before, older than ASK_MAX_AGE_S, or (for a send) made before this program
started are refused, so a mail can never go out twice.

Settings live in <home>/remote.json: {"relay": "https://...workers.dev", "channel", "key", "write", "mail"}.
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
ASK_ROUTES = ("state", "mail", "item", "attachment", "send")
ASK_MAX_AGE_S = 120
ASK_IDLE_S = 5  # how often the PC looks for a question while nobody is reading mail
ASK_BUSY_S = 0.7  # ... and while somebody is
ASK_BUSY_FOR_S = 120
ASK_RETRY_S = 20  # after the relay could not be reached
MAX_ANSWER = 1_900_000  # sealed characters; the relay refuses more than 2,000,000
MAX_WORKERS = 4


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


def ways_out() -> list[dict]:
    """The ways to the internet from this PC, as proxy settings, in the order to try them:
    what the environment says, what Windows itself is set to, and straight out.

    A PC that moves between an office and home, or whose local proxy program is not running,
    still reaches the relay through one of them.
    """
    ways = [urllib.request.getproxies()]
    if hasattr(urllib.request, "getproxies_registry"):
        ways.append(urllib.request.getproxies_registry())
    different = []
    for way in ways:
        way = {scheme: address for scheme, address in way.items() if scheme in ("http", "https")}
        if way and way not in different:
            different.append(way)
    return [*different, {}]


def _open(way: dict, request: urllib.request.Request) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(way))
    with opener.open(request, timeout=TIMEOUT_S) as reply:
        return reply.status, reply.read(8_000_000)


_good_way = [0]  # the way that worked last is tried first, so a dead one costs time only once


def http(
    method: str, url: str, body: bytes | None = None, headers: dict | None = None, ways=ways_out, open_with=_open,
) -> tuple[int, bytes]:
    """(status, body). Raises OSError only when no way out of this network reaches the address."""
    choices = ways()
    start = _good_way[0] if _good_way[0] < len(choices) else 0
    failure: OSError = OSError("no way out")
    for index in [start, *(i for i in range(len(choices)) if i != start)]:
        # a request remembers the proxy it was sent through, so each way gets its own
        request = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "titip", **(headers or {})})
        try:
            answer = open_with(choices[index], request)
        except urllib.error.HTTPError as error:
            if error.code == 407:  # the proxy wants a password, so it will not carry this: try the next way
                failure = OSError("the proxy wants a password")
                continue
            answer = (error.code, b"")
        except OSError as error:  # refused, unknown host, timed out
            failure = error
            continue
        _good_way[0] = index
        return answer
    raise failure


class Remote:
    def __init__(self, home: Path, autopilot, send=http, clock=time.time, mail=None):
        self.path = Path(home) / "remote.json"
        self.autopilot = autopilot
        self.send = send
        self.clock = clock
        self.mail = mail  # (route, params) -> (status, data): the same mail calls a paired phone makes
        self.mail_on = False
        self.mail_error = ""
        self.last_ask = 0.0
        self.started = clock()
        self.working = 0  # asks being answered right now
        self._asked: list[str] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._mail_thread: threading.Thread | None = None
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
            self.mail_on = saved.get("mail") is True
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
        self.path.write_text(json.dumps({**settings, "mail": self.mail_on}, indent=1), encoding="utf-8")

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
            "mail": self.mail_on,
            "mailError": self.mail_error,
            "lastAsk": int(self.last_ask),
        }

    def set_mail(self, on: bool) -> None:
        """The switch on the PC page: may a phone with the code read and send Outlook mail through the relay?"""
        self.mail_on = bool(on)
        self.mail_error = ""
        self._save(self.settings)
        self._published = ""  # the Autopilot page shows or hides its Mail tab
        self.touch()
        self._wake.set()

    # ------------------------------------------------------------ the two directions
    def touch(self) -> None:
        self._dirty.set()

    def publish(self, force: bool = False) -> bool:
        """Send the current state if it changed. True when the relay took it."""
        snapshot = {**slim(self.autopilot.snapshot()), "mail": self.mail_on and self.mail is not None}
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

    # ------------------------------------------------------------ mail from anywhere
    def take_asks(self) -> list[tuple[str, dict | None]]:
        """Questions the phone left: (id, opened question or None); they are removed from the relay."""
        status, body = self.send("POST", self._url("asks/take"), b"", {"X-Titip-Write": self.settings["write"]})
        if status == 404:  # the relay does not know this PC yet, or is an older one without mail
            return []
        if status != 200:
            raise OSError(f"the relay answered {status}")
        try:
            waiting = json.loads(body.decode("utf-8")).get("asks", [])
        except (ValueError, AttributeError):
            return []
        asks = []
        for entry in waiting if isinstance(waiting, list) else []:
            if isinstance(entry, dict) and entry.get("id"):
                asks.append((str(entry["id"]), unseal(self.key, str(entry.get("body", "")))))
        return asks

    def _refusal(self, identity: str, ask: dict) -> str | None:
        """Why this ask is not carried out ("" = say nothing at all), or None when it is fine."""
        if str(ask.get("id", "")) != identity or ask.get("route") not in ASK_ROUTES:
            return ""
        with self._lock:
            if identity in self._asked:
                return ""  # seen before: the first one got the answer
            self._asked.append(identity)
            del self._asked[:-500]
        try:
            at = float(ask.get("at", 0))
        except (TypeError, ValueError):
            return ""
        if abs(self.clock() - at) > ASK_MAX_AGE_S:
            return "The clocks of your phone and your PC are more than two minutes apart. Set both to automatic time."
        if ask["route"] == "send" and at < self.started:
            return "Titip on your PC had just restarted, so nothing was sent. Send it again."
        return None

    def answer(self, identity: str, ask: dict | None) -> bool:
        """Carry out one ask and leave the sealed answer. False when it was dropped without an answer."""
        if ask is None or self.mail is None:
            return False  # not sealed with our key: nobody we know asked
        refused = self._refusal(identity, ask)
        if refused == "":
            return False
        if refused:
            status, data = 400, {"error": refused}
        else:
            params = ask.get("params")
            try:
                status, data = self.mail(ask["route"], params if isinstance(params, dict) else {})
            except Exception:  # never leave the phone waiting for nothing
                status, data = 500, {"error": "Titip on your PC could not do that."}
        sealed = seal(self.key, {"id": identity, "status": status, "data": data})
        if len(sealed) > MAX_ANSWER and status == 200 and data.get("html"):
            data = {**data, "html": "", "cut": True}  # a very long formatted mail goes as plain text
            sealed = seal(self.key, {"id": identity, "status": status, "data": data})
        if len(sealed) > MAX_ANSWER:
            sealed = seal(self.key, {"id": identity, "status": 413, "data": {
                "error": "That is too large to pass through the relay. Open it on your own network or at your PC."}})
        status, _ = self.send(
            "PUT", self._url(f"answer/{identity}"), sealed.encode("ascii"),
            {"X-Titip-Write": self.settings["write"], "Content-Type": "text/plain"},
        )
        if status != 200:
            raise OSError(f"the relay answered {status}")
        return True

    def _answer_in_thread(self, identity: str, ask: dict | None) -> None:
        try:
            self.answer(identity, ask)
        except Exception as error:
            self.mail_error = f"An answer could not be handed to the relay ({type(error).__name__})."
        finally:
            with self._lock:
                self.working -= 1

    def mail_step(self) -> float:
        """One look for asks. Returns how long to wait before the next one."""
        asks = self.take_asks()
        self.mail_error = ""
        if asks:
            self.last_ask = self.clock()
        for identity, ask in asks:
            with self._lock:
                self.working += 1
                crowded = self.working > MAX_WORKERS
            if crowded:  # in this thread: the next look waits until it is done
                self._answer_in_thread(identity, ask)
            else:
                threading.Thread(target=self._answer_in_thread, args=(identity, ask), daemon=True).start()
        return ASK_BUSY_S if self.clock() - self.last_ask < ASK_BUSY_FOR_S else ASK_IDLE_S

    def _run_mail(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if not (self.mail_on and self.ready and self.mail is not None):
                self._wake.wait(30)
                continue
            try:
                wait = self.mail_step()
            except Exception as error:
                self.mail_error = f"The relay could not be reached ({type(error).__name__})."
                wait = ASK_RETRY_S
            self._stop.wait(wait)

    # ------------------------------------------------------------ the threads
    def start(self) -> None:
        if self._thread is None and self.ready:
            self._thread = threading.Thread(target=self._run, daemon=True, name="titip-remote")
            self._thread.start()
            self._mail_thread = threading.Thread(target=self._run_mail, daemon=True, name="titip-remote-mail")
            self._mail_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
                self.error = ""
            except Exception as error:  # no network, proxy trouble: try again next round
                self.error = f"The relay could not be reached ({type(error).__name__})."
            self._stop.wait(POLL_S)
