"""Two small web servers in one process.

* Phone listener (all network interfaces, port 47779, HTTPS): the mail app and its API. Every request
  needs a paired device's cookie; without it the only thing served is the "scan the QR code" page.
* Desk listener (127.0.0.1 only, port 47780): the page on the PC that shows the QR code, the paired
  phones and "Unpair all phones". It never serves mail.

No request path, mail id or mail text is ever written to a log.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
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
from .tls import ensure_certificate, server_context

PHONE_PORT = 47779
DESK_PORT = 47780
MAX_POST = 1_000_000
HEADER = "X-Titip"  # a custom header a cross-site form cannot send: blocks forged POSTs

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}
PUBLIC = {  # what an unpaired phone may load: the pairing page's script, style and icon
    "/pair.js": "text/javascript; charset=utf-8",
    "/styles.css": "text/css; charset=utf-8",
    "/icon.svg": "image/svg+xml",
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
        try:
            if route == "/api/state":
                return self._json(200, {**self.app.mailbox.state(), "version": __version__})
            if route == "/api/mail":
                return self._json(200, self.app.mailbox.list(
                    query.get("folder", "inbox"),
                    int(query.get("limit", 40)),
                    int(query.get("offset", 0)),
                    query.get("q", ""),
                ))
            if route == "/api/item":
                return self._json(200, self.app.mailbox.get(query.get("id", "")))
            if route == "/api/attachment":
                return self._attachment(query.get("id", ""), int(query.get("index", 0)))
        except OutlookBusy as error:  # the phone asks again by itself: reading twice does no harm
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error), "busy": True})
        except MailError as error:
            return self._error(HTTPStatus.BAD_GATEWAY, str(error))
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "Bad request.")
        return self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def _attachment(self, mail_id: str, index: int) -> None:
        folder = Path(tempfile.mkdtemp(prefix="titip-"))
        try:
            name, path = self.app.mailbox.attachment(mail_id, index, folder)
            data = path.read_bytes()
        finally:
            shutil.rmtree(folder, ignore_errors=True)
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
        if route == "/api/send":
            try:
                return self._json(200, self.app.mailbox.send(body))
            except MailError as error:
                return self._error(HTTPStatus.BAD_REQUEST, str(error))
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
        if route in ("/desk.js", "/styles.css", "/icon.svg"):
            content_type = {**PUBLIC, "/desk.js": "text/javascript; charset=utf-8"}[route]
            return self._send(200, (WEB / route.lstrip("/")).read_bytes(), content_type)
        if route == "/desk/status":
            return self._json(200, self.app.status())
        return self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self):
        if not self._local():
            return self._error(HTTPStatus.FORBIDDEN, "Open this page on the PC that runs Titip.")
        route = urlparse(self.path).path
        if self._checked_post() is None:
            return None
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
    ):
        self.mailbox = mailbox
        self.home = Path(home)
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

    def stop(self) -> None:
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
        return {
            "version": __version__,
            "https": self.https,
            "httpsNote": self.https_note,
            "addresses": self.addresses(),
            "devices": self.devices.devices(),
            "mail": mail,
        }
