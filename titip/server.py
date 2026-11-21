"""Two small web servers in one process.

* Phone listener (all network interfaces, port 47779, HTTPS): the mail app and its API. Every request
  needs a paired device's cookie; without it the only thing served is the "scan the QR code" page.
* Desk listener (127.0.0.1 only, port 47780): the page on the PC that shows the QR code, the paired
  phones and "Unpair all phones". It never serves mail.

Both also serve the Autopilot page (titip.autopilot): what it did with new Gmail and what waits for you.

A phone on another network has a third way in, without any listener: titip.remote asks a relay for
encrypted questions and answers them with Titip.mail_call, the same function the phone listener uses.

No request path, mail id or mail text is ever written to a log.
"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from .autopilot import Autopilot
from .mailbox import WEB, MailError, OutlookBusy
from .pairing import (
    COOKIE,
    COOKIE_MAX_AGE,
    DeviceStore,
    PairingCodes,
    device_name,
    lan_addresses,
    qr_svg,
    tailscale_name,
)
from .remote import Remote
from .tls import ensure_certificate, server_context
from .update import Updater
from .uploads import FilesGone, Uploads

PHONE_PORT = 47779
DESK_PORT = 47780
MAX_POST = 1_000_000  # also what one piece of an attached file must fit in (see titip/uploads.py)
MAX_FAR_FILE = 1_000_000  # an attachment this size still fits through the relay once it is sealed
HEADER = "X-Titip"  # a custom header a cross-site form cannot send: blocks forged POSTs

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/auto.html": ("auto.html", "text/html; charset=utf-8"),
    "/auto.js": ("auto.js", "text/javascript; charset=utf-8"),
    "/far.js": ("far.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}
FONTS = {  # the three typefaces styles.css asks for (see THIRD_PARTY_NOTICES.md); nothing private in them
    "/instrument-serif.woff2": "font/woff2",
    "/geist.woff2": "font/woff2",
    "/geist-mono.woff2": "font/woff2",
}
PUBLIC = {  # what an unpaired phone may load: the pairing page's script, style, icon and typefaces
    "/pair.js": "text/javascript; charset=utf-8",
    "/styles.css": "text/css; charset=utf-8",
    "/icon.svg": "image/svg+xml",
    **FONTS,
}
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "frame-src 'self' about:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
PAIR_PAGE = (WEB / "pair.html").read_text(encoding="utf-8")
NOT_PAIRED = "This phone is not paired yet. On your PC open Titip and scan the QR code it shows."
CODE_USED = "This QR code has expired or was already used. Make a new one in Titip on your PC."


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    tls = None

    def get_request(self):
        sock, address = self.socket.accept()
        if self.tls is not None:  # handshake happens on first read, in the request's own thread
            sock = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address

    def handle_error(self, request, client_address):
        pass  # dropped connections and plain-HTTP probes on the TLS port are not worth a traceback


class _Handler(BaseHTTPRequestHandler):
    app: "Titip"
    server_version = "Titip"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, format, *args):  # noqa: A002 - never log paths (they carry mail ids)
        pass

    # -------------------------------------------------- replies
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict, extra: dict | None = None) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _static(self, route: str) -> bool:
        if route not in STATIC:
            return False
        name, content_type = STATIC[route]
        try:
            self._send(200, (WEB / name).read_bytes(), content_type)
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_POST:
            raise ValueError("That message is too long to send from the phone.")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _checked_post(self) -> dict | None:
        """The JSON body of a POST that carries our header; replies with the error and returns None otherwise."""
        if self.headers.get(HEADER) != "1":
            self.close_connection = True
            self._error(HTTPStatus.FORBIDDEN, f"Missing {HEADER} header.")
            return None
        try:
            return self._body()
        except (ValueError, UnicodeDecodeError) as error:
            self.close_connection = True
            self._error(HTTPStatus.BAD_REQUEST, str(error) or "Could not read the request.")
            return None

    def _auto_order(self, body: dict) -> None:
        """Pause, mode, look now, send a waiting answer, or leave it unanswered."""
        details = {name: body[name] for name in ("on", "mode", "id", "text") if name in body}
        try:
            self._json(200, self.app.auto_state(self.app.autopilot.command(str(body.get("command", "")), **details)))
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def do_HEAD(self):
        self.do_GET()


# ============================================================ phone

class PhoneHandler(_Handler):
    def _token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE:
                return value
        return ""

    def _paired(self) -> bool:
        return self.app.devices.check(self._token())

    def _cookie(self, token: str) -> str:
        secure = "; Secure" if self.app.https else ""
        return f"{COOKIE}={token}; Max-Age={COOKIE_MAX_AGE}; Path=/; HttpOnly; SameSite=Strict{secure}"

    def _pair_page(self, status: int) -> None:
        self._send(status, PAIR_PAGE.replace("__MESSAGE__", NOT_PAIRED).encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        route = url.path
        if route == "/pair":
            return self._pair_page(200)
        if route in PUBLIC:
            return self._send(200, (WEB / route.lstrip("/")).read_bytes(), PUBLIC[route])
        if not self._paired():
            if route.startswith("/api/"):
                return self._error(HTTPStatus.UNAUTHORIZED, NOT_PAIRED)
            return self._pair_page(HTTPStatus.UNAUTHORIZED)
        if route == "/":  # renew the cookie while the phone keeps using the app
            name, content_type = STATIC["/"]
            return self._send(200, (WEB / name).read_bytes(), content_type, {"Set-Cookie": self._cookie(self._token())})
        if self._static(route):
            return None
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if route == "/api/auto":
            return self._json(200, self.app.auto_state())
        if route == "/api/attachment":
            try:
                return self._attachment(query.get("id", ""), int(query.get("index", 0)))
            except OutlookBusy as error:
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error), "busy": True})
            except MailError as error:
                return self._error(HTTPStatus.BAD_GATEWAY, str(error))
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "Bad request.")
        if route in ("/api/state", "/api/mail", "/api/item", "/api/people", "/api/calendar", "/api/event", "/api/folders", "/api/signature", "/api/calendars", "/api/rules"):
            return self._json(*self.app.mail_call(route[5:], query))
        return self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def _attachment(self, mail_id: str, index: int) -> None:
        name, data = self.app.attachment(mail_id, index)
        fallback = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "._- ") else "_" for ch in name) or "attachment"
        self._send(200, data, "application/octet-stream", {
            "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}",
        })

    def do_POST(self):
        route = urlparse(self.path).path
        body = self._checked_post()
        if body is None:
            return None
        if route == "/api/pair":
            if not self.app.codes.redeem(str(body.get("code", ""))):
                return self._error(HTTPStatus.FORBIDDEN, CODE_USED)
            token = self.app.devices.add(device_name(self.headers.get("User-Agent", "")))
            return self._json(200, {"paired": True}, {"Set-Cookie": self._cookie(token)})
        if not self._paired():
            return self._error(HTTPStatus.UNAUTHORIZED, NOT_PAIRED)
        if route == "/api/auto":
            return self._auto_order(body)
        if route in ("/api/send", "/api/upload", "/api/act", "/api/plan", "/api/arrange"):
            return self._json(*self.app.mail_call(route[5:], body))
        return self._error(HTTPStatus.NOT_FOUND, "Not found.")


# ============================================================ desk (this PC only)

class DeskHandler(_Handler):
    def _local(self) -> bool:
        """Only a browser on this PC that typed our own address: stops DNS-rebinding pages."""
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self._local():
            return self._error(HTTPStatus.FORBIDDEN, "Open this page on the PC that runs Titip.")
        route = urlparse(self.path).path
        if route == "/":
            return self._send(200, (WEB / "desk.html").read_bytes(), "text/html; charset=utf-8")
        if route in ("/desk.js", "/styles.css", "/icon.svg") or route in FONTS:
            content_type = {**PUBLIC, "/desk.js": "text/javascript; charset=utf-8"}[route]
            return self._send(200, (WEB / route.lstrip("/")).read_bytes(), content_type)
        if route == "/auto.html":  # the Autopilot page, on the PC itself; its other tab leads back here
            page = (WEB / "auto.html").read_text(encoding="utf-8").replace(">Mail</a>", ">This PC</a>")
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if route == "/auto.js":
            return self._static(route)
        if route == "/api/auto":
            return self._json(200, self.app.auto_state())
        if route == "/desk/status":
            return self._json(200, self.app.status())
        return self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self):
        if not self._local():
            return self._error(HTTPStatus.FORBIDDEN, "Open this page on the PC that runs Titip.")
        route = urlparse(self.path).path
        body = self._checked_post()
        if body is None:
            return None
        if route == "/api/auto":
            return self._auto_order(body)
        if route == "/desk/far":  # the code a phone scans on any network: the mail app, or Autopilot only
            if body.get("renew"):
                self.app.remote.renew()
            return self._json(200, self.app.far_pairing())
        if route == "/desk/mailfar":  # may a phone with that code also read and send Outlook mail?
            self.app.remote.set_mail(body.get("on") is True)
            return self._json(200, self.app.status())
        if route == "/desk/update":  # switch self-updating on or off, or look for a new version now
            if "on" in body:
                self.app.updater.switch(body.get("on") is True)
            if body.get("check"):
                self.app.updater.check_soon()
            return self._json(200, self.app.status())
        if route == "/desk/pair":
            return self._json(200, self.app.new_pairing())
        if route == "/desk/unpair":
            self.app.unpair_all()
            return self._json(200, self.app.status())
        if route == "/desk/quit":
            self._json(200, {"stopping": True})
            self.app.quit.set()  # the main thread is waiting on this
            return None
        return self._error(HTTPStatus.NOT_FOUND, "Not found.")


# ============================================================ the app

class Titip:
    def __init__(
        self,
        mailbox,
        home: Path,
        phone_port: int = PHONE_PORT,
        desk_port: int = DESK_PORT,
        https: bool = True,
        phone_host: str = "0.0.0.0",
        autopilot: Autopilot | None = None,
        updater: Updater | None = None,
    ):
        self.mailbox = mailbox
        self.home = Path(home)
        self.autopilot = autopilot or Autopilot(self.home)  # without a mailbox: the page says it cannot run
        self.remote = Remote(self.home, self.autopilot, mail=self.mail_call)
        self.autopilot.on_change = self.remote.touch
        self.updater = updater or Updater(self.home, root=None)  # without a checkout: the page says so
        self.updater.busy = self.busy
        self.updater.restart = self.restart_for_update
        self.restarting = False
        self.uploads = Uploads()  # files the phone attaches, until the mail they belong to is sent
        self._calls = 0
        self._calls_lock = threading.Lock()
        self.devices = DeviceStore(self.home / "devices.json")
        self.codes = PairingCodes()
        self.https = https
        self.https_note = ""
        self._phone_address = (phone_host, phone_port)
        self._desk_address = ("127.0.0.1", desk_port)
        self.phone: _Server | None = None
        self.desk: _Server | None = None
        self.quit = threading.Event()

    def start(self) -> None:
        phone_handler = type("BoundPhoneHandler", (PhoneHandler,), {"app": self})
        desk_handler = type("BoundDeskHandler", (DeskHandler,), {"app": self})
        self.desk = _Server(self._desk_address, desk_handler)
        self.phone = _Server(self._phone_address, phone_handler)
        if self.https:
            pair = ensure_certificate(self.home, self.hosts())
            if pair is None:
                self.https = False
                self.https_note = "The 'cryptography' package is missing, so the phone link is plain HTTP."
            else:
                self.phone.tls = server_context(*pair)
        for server in (self.desk, self.phone):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.autopilot.start()
        self.remote.start()
        self.updater.start()
        # a phone can ask for mail: have the newest ready, so its first look after a restart does not wait for Outlook
        read_ahead = getattr(self.mailbox, "read_ahead_now", None)
        if read_ahead is not None and (self.remote.mail_on or self.devices.devices()):
            threading.Thread(target=read_ahead, daemon=True, name="titip-start").start()

    def stop(self) -> None:
        self.updater.stop()
        self.autopilot.stop()
        self.remote.stop()
        for server in (self.phone, self.desk):
            if server is not None:
                server.shutdown()
                server.server_close()
        self.phone = self.desk = None

    @property
    def phone_port(self) -> int:
        return self.phone.server_address[1] if self.phone else self._phone_address[1]

    @property
    def desk_url(self) -> str:
        port = self.desk.server_address[1] if self.desk else self._desk_address[1]
        return f"http://127.0.0.1:{port}/"

    def hosts(self) -> list[str]:
        """Names the phone can use for this PC, best first (Tailscale works from any network)."""
        name = tailscale_name()
        return ([name] if name else []) + lan_addresses()

    def addresses(self) -> list[str]:
        scheme = "https" if self.https else "http"
        return [f"{scheme}://{host}:{self.phone_port}" for host in self.hosts()]

    def new_pairing(self) -> dict:
        code = self.codes.new()
        bases = self.addresses() or [f"{'https' if self.https else 'http'}://127.0.0.1:{self.phone_port}"]
        url = f"{bases[0]}/pair#{code}"
        return {
            "url": url,
            "qr": qr_svg(url),
            "others": [f"{base}/pair#{code}" for base in bases[1:]],
            "expiresIn": self.codes.seconds,
        }

    # -------------------------------------------------- mail, for a paired phone and for the relay alike
    def mail_call(self, route: str, params: dict) -> tuple[int, dict]:
        """One mail request: (HTTP status, JSON answer). Never raises for anything Outlook does."""
        with self._calls_lock:
            self._calls += 1
        try:
            if route == "send":
                try:
                    # the phone names the files it handed over piece by piece; the mailbox gets their bytes
                    return 200, self.mailbox.send({**params, "files": self.uploads.take(params.get("files"))})
                except FilesGone as error:
                    return 400, {"error": str(error), "filesGone": True}
                except MailError as error:
                    return 400, {"error": str(error)}
            if route == "upload":  # one piece of a file that will be attached
                try:
                    return 200, self.uploads.put(params)
                except MailError as error:
                    return 400, {"error": str(error)}
            if route == "act":  # delete, move, flag or mark a message, answer an invitation
                try:
                    return 200, self.mailbox.act(params)
                except OutlookBusy:
                    raise  # only marking as read comes back this way: nothing was done, and nobody waits for it
                except MailError as error:
                    return 400, {"error": str(error)}
            if route == "plan":  # make, change or take out a calendar entry
                try:
                    return 200, self.mailbox.plan(params)
                except MailError as error:
                    return 400, {"error": str(error)}
            if route == "arrange":  # a folder or a category made, a folder renamed, a rule switched or made
                try:
                    return 200, self.mailbox.arrange(params)
                except MailError as error:
                    return 400, {"error": str(error)}
            if route == "calendars":  # the other calendars Outlook shows
                return 200, self.mailbox.calendars()
            if route == "rules":
                return 200, self.mailbox.rules()
            if route == "folders":  # your own folders and the categories
                return 200, self.mailbox.folders()
            if route == "signature":
                return 200, self.mailbox.signature()
            if route == "state":  # last known counts when Outlook is busy: lists and messages do not wait for them
                return 200, {**self.mailbox.quick_state(), "version": __version__}
            if route == "mail":
                return 200, self.mailbox.list(
                    str(params.get("folder", "inbox")),
                    int(params.get("limit", 40)),
                    int(params.get("offset", 0)),
                    str(params.get("q", "")),
                    *((True,) if str(params.get("deep", "")) in ("1", "True", "true") else ()),  # also in the text of the mail
                )
            if route == "item":
                return 200, self.mailbox.get(str(params.get("id", "")))
            if route == "calendar":
                which = str(params.get("which") or "")
                return 200, self.mailbox.calendar(int(params.get("offset", 0)), int(params.get("days", 14)), *((which,) if which else ()))
            if route == "event":
                return 200, self.mailbox.event(str(params.get("id", "")))
            if route == "people":
                typed = str(params.get("q", "")).strip()
                if typed:  # a name being typed: Outlook's address book is asked for it
                    return 200, self.mailbox.lookup(typed)
                return 200, self.mailbox.people()  # who the composer suggests: from memory, Outlook is not asked
            if route == "attachment":  # only the relay asks this way; a paired phone downloads the file itself
                name, data = self.attachment(str(params.get("id", "")), int(params.get("index", 0)), MAX_FAR_FILE)
                return 200, {"name": name, "size": len(data), "data": base64.b64encode(data).decode("ascii")}
        except OutlookBusy as error:  # the phone asks again by itself: reading twice does no harm
            return 503, {"error": str(error), "busy": True}
        except MailError as error:
            return 502, {"error": str(error)}
        except (ValueError, TypeError):
            return 400, {"error": "Bad request."}
        except Exception as error:  # something no part of Titip expected: the phone still gets an answer, in words
            if route in ("send", "act", "plan", "arrange"):
                return 502, {"error": f"Something went wrong on your PC while doing that ({type(error).__name__}). Look in Outlook before you try it again."}
            return 502, {"error": f"Titip on your PC could not read that ({type(error).__name__})."}
        finally:
            with self._calls_lock:
                self._calls -= 1
        return 404, {"error": "Not found."}

    def attachment(self, mail_id: str, index: int, limit: int = 0) -> tuple[str, bytes]:
        """(file name, bytes) of one attachment. With a limit, a larger file is refused instead of read."""
        folder = Path(tempfile.mkdtemp(prefix="titip-"))
        try:
            name, path = self.mailbox.attachment(mail_id, index, folder)
            if limit and path.stat().st_size > limit:
                raise MailError(
                    f"\u201c{name}\u201d is larger than 1 MB, too large for the relay. "
                    "Open it on your own network or at your PC."
                )
            return name, path.read_bytes()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # -------------------------------------------------- updating itself
    def busy(self) -> bool:
        """True while a restart would cut something short: Autopilot at work, a mail request under way,
        or a file on its way into a mail."""
        return (
            bool(self.autopilot.snapshot().get("busy")) or self._calls > 0 or self.remote.working > 0
            or self.uploads.active()
        )

    def restart_for_update(self) -> None:
        self.restarting = True
        self.quit.set()  # the main thread stops the listeners and starts the new version

    def auto_state(self, snapshot: dict | None = None) -> dict:
        return {**(snapshot or self.autopilot.snapshot()), "version": __version__}

    def far_pairing(self) -> dict:
        """The address (with the key after #) a phone opens from any network, as a QR code."""
        url = self.remote.link()
        return {"ready": bool(url), "url": url, "qr": qr_svg(url) if url else "", **self.remote.status()}

    def unpair_all(self) -> None:
        self.codes.clear()
        self.devices.clear()

    def status(self) -> dict:
        try:
            mail = self.mailbox.quick_state()
        except MailError as error:
            mail = {"ok": False, "error": str(error)}
        except Exception:
            mail = {"ok": False, "error": "Outlook could not be reached on this PC."}
        auto = self.autopilot.snapshot()
        return {
            "version": __version__,
            "https": self.https,
            "httpsNote": self.https_note,
            "addresses": self.addresses(),
            "devices": self.devices.devices(),
            "mail": mail,
            "autopilot": {key: auto[key] for key in ("available", "on", "busy", "mailboxes", "lastCheck", "now")}
            | {"waiting": len(auto["held"])},
            "far": self.remote.status(),
            "update": self.updater.status(),
        }
