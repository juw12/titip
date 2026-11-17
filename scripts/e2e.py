"""End-to-end check in a real browser against the demo mailbox.

    python scripts/e2e.py [folder-for-screenshots]

Starts Titip with invented mail on spare ports, pairs a browser through the real QR link (HTTPS on this
PC's network address), then reads, answers, undoes, sends, saves a draft and searches, at phone and
desktop sizes, light and dark. Then Autopilot: on the paired phone, on the public page (demo), and
through a stand-in relay that only ever gets ciphertext. Then mail from any network, a full round
trip: the public page asks, a stand-in relay with the worker's rules passes sealed text, this Titip
answers. Fails on any console error, page error or sideways overflow.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "shots"
PHONE_PORT, DESK_PORT = 47891, 47892
WIDTHS = (320, 390, 768, 1024, 1440)
problems: list[str] = []


def step(text: str) -> None:
    print(text, flush=True)


def check(condition: bool, label: str) -> None:
    step(("ok   " if condition else "FAIL ") + label)
    if not condition:
        problems.append(label)


def desk(path: str, post: bool = False, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{DESK_PORT}{path}",
        data=json.dumps(body or {}).encode("utf-8") if post or body else None,
        headers={"X-Titip": "1", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as reply:
        return json.load(reply)


def autopilot(command: str, **details) -> dict:
    return desk("/api/auto", body={"command": command, **details})


class QuietPages(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def serve_public_pages() -> str:
    """docs/ the way GitHub Pages serves it: plain files, no Titip server behind them."""
    handler = functools.partial(QuietPages, directory=str(ROOT / "docs"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


class StandInRelay:
    """worker/titip-relay.js in a few lines. Titip reaches it over HTTP, the browser page through page.route.

    It keeps every body that passed in `wire`, so the run can prove none of it was readable.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.owner: dict[str, str] = {}
        self.state: dict[str, str] = {}
        self.orders: dict[str, list[str]] = {}
        self.asks: dict[str, list[dict]] = {}
        self.answers: dict[str, str] = {}
        self.wire: list[str] = []
        self.asked: list[str] = []
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{httpd.server_address[1]}"

    def handle(self, method: str, path: str, body: str, token: str) -> tuple[int, str]:
        parts = path.split("?")[0].split("/")
        if len(parts) < 3 or parts[1] != "c":
            return 404, ""
        channel, what = parts[2], "/".join(parts[3:])
        with self.lock:
            if body:
                self.wire.append(body)
            owns = bool(token) and self.owner.get(channel) == token
            if what == "state" and method == "PUT":
                if self.owner.setdefault(channel, token) != token:
                    return 403, ""
                self.state[channel] = body
                return 200, "ok"
            if what == "state" and method == "GET":
                return (200, self.state[channel]) if channel in self.state else (404, "")
            if what == "orders" and method == "POST":
                self.orders.setdefault(channel, []).append(body)
                return 200, "ok"
            if what == "orders/take" and method == "POST":
                return (200, json.dumps({"orders": self.orders.pop(channel, [])})) if owns else (404, "")
            if what.startswith("ask/") and method == "POST":
                self.asks.setdefault(channel, []).append({"id": parts[4], "body": body})
                self.asked.append(parts[4])
                return 200, "ok"
            if what == "asks/take" and method == "POST":
                return (200, json.dumps({"asks": self.asks.pop(channel, [])})) if owns else (404, "")
            if what.startswith("answer/") and method == "PUT":
                if not owns:
                    return 404, ""
                self.answers[parts[4]] = body
                return 200, "ok"
            if what.startswith("answer/") and method == "GET":  # handed out once
                answer = self.answers.pop(parts[4], None)
                if answer is not None:
                    self.wire.append(answer)
                return (200, answer) if answer is not None else (204, "")
        return 404, ""

    def _handler(self):
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("ascii") if length else ""
                status, text = relay.handle(self.command, self.path, body, self.headers.get("X-Titip-Write", ""))
                data = text.encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if status != 204:
                    self.wfile.write(data)

            do_GET = do_PUT = do_POST = do_DELETE = _any

        return Handler


def overflow(page) -> int:
    return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def main() -> None:
    from playwright.sync_api import expect, sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    home = tempfile.mkdtemp(prefix="titip-e2e-")
    environment = {k: v for k, v in os.environ.items() if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    environment["NO_PROXY"] = "127.0.0.1,localhost"  # the stand-in relay is on this PC
    stand_in = StandInRelay()
    (Path(home) / "remote.json").write_text(json.dumps({"relay": stand_in.url, "mail": True}), encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "titip", "--demo", "--quiet", "--home", home,
         "--phone-port", str(PHONE_PORT), "--desk-port", str(DESK_PORT)],
        cwd=ROOT, env=environment,
    )
    try:
        for _ in range(40):
            try:
                desk("/desk/status")
                break
            except OSError:
                time.sleep(0.25)
        pairing = desk("/desk/pair", post=True)
        check(pairing["url"].startswith("https://") and bool(pairing["qr"]), "desk hands out an https QR link")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-proxy-server"])
            context = browser.new_context(
                viewport={"width": 390, "height": 844}, device_scale_factor=2, ignore_https_errors=True,
                is_mobile=True, has_touch=True, color_scheme="light",
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))

            # ---- an unpaired browser sees only the pairing page
            page.goto(pairing["url"].split("/pair#")[0] + "/")
            check("not paired" in page.inner_text("body"), "unpaired phone is told to scan the QR code")
            errors.clear()  # the 401 above is expected and logged by the browser

            # ---- pair with the QR link
            page.goto(pairing["url"])
            page.wait_for_selector(".row", timeout=15000)
            check("/pair" not in page.url, "QR link pairs and lands in the inbox")
            rows = page.locator(".row")
            check(rows.count() >= 6, f"inbox lists mail ({rows.count()} rows)")
            check(page.locator("#demoNote").is_visible(), "demo banner says nothing is sent")
            check(page.locator(".row.unread").count() == 3, "unread mail is marked in the list")
            page.screenshot(path=SHOTS / "phone-inbox.png")

            # ---- read a formatted message
            rows.first.click()
            page.wait_for_selector("#letter:not([hidden]) iframe")
            frame = page.frame_locator("#body iframe")
            check(frame.locator("table").count() == 1, "formatted mail shows its table inside the sandboxed frame")
            sandbox = page.get_attribute("#body iframe", "sandbox")
            check("allow-scripts" not in sandbox, "mail frame cannot run scripts")
            check(page.locator("#files a").count() == 2, "attachments are listed")
            check(overflow(page) <= 0, "reader does not overflow sideways at 390")
            page.wait_for_timeout(300)
            page.screenshot(path=SHOTS / "phone-reader.png")
            page.click("#viewToggle")
            check("Average opening torque" in page.inner_text("#body pre"), "plain-text view works")

            # ---- reply, undo, send
            page.click("#actions button:has-text('Reply')")
            check(page.locator("#toField").is_hidden(), "reply hides To (Outlook addresses it)")
            page.fill("#bodyInput", "Run the remaining 5,000 cycles please. We section one on Monday.")
            page.screenshot(path=SHOTS / "phone-compose.png")
            page.click("#send")
            page.wait_for_selector("#toast:not([hidden])")
            check("Sending in" in page.inner_text("#toastText"), "send waits behind an undo countdown")
            page.screenshot(path=SHOTS / "phone-undo.png")
            page.click("#toastAction")
            check(page.input_value("#bodyInput").startswith("Run the remaining"), "undo brings the text back")
            page.click("#send")
            expect(page.locator("#toastText")).to_have_text("Sent to Mateo Silva", timeout=30000)
            page.screenshot(path=SHOTS / "phone-sent.png")
            page.click("#back")
            check(page.locator(".row.unread").count() == 2, "mail opened on the phone is no longer marked unread")

            # ---- a send that cannot reach the PC keeps the text and says so
            page.locator(".row").nth(3).click()
            page.wait_for_selector("#actions button:has-text('Reply')")
            page.click("#actions button:has-text('Reply')")
            page.fill("#bodyInput", "Thursday 10:00 works for me. I will bring both units.")
            page.route("**/api/send", lambda route: route.abort())
            page.click("#send")
            expect(page.locator("#composeError")).to_contain_text("Not sent.", timeout=30000)
            check(page.input_value("#bodyInput").startswith("Thursday 10:00"), "a failed send keeps the text and says it was not sent")
            page.screenshot(path=SHOTS / "phone-failed.png")
            page.unroute("**/api/send")
            errors[:] = [e for e in errors if "ERR_FAILED" not in e]  # the blocked request above, on purpose
            page.click("#discard")
            page.click("#back")

            # ---- Outlook busy on the PC: the phone waits and asks again by itself
            busy_replies = []

            def busy_once(route):
                if busy_replies:
                    return route.continue_()
                busy_replies.append(1)
                route.fulfill(
                    status=503, content_type="application/json",
                    body='{"error": "Outlook on your PC is busy right now. Try again in a moment.", "busy": true}',
                )

            page.route("**/api/item*", busy_once)
            page.locator(".row").nth(3).click()
            expect(page.locator("#toastText")).to_contain_text("Still trying", timeout=5000)
            check(page.locator("#back").is_visible() and "Opening" in page.inner_text("#body"),
                  "a message that is slow to open shows its subject and the way back")
            page.screenshot(path=SHOTS / "phone-busy.png")
            page.wait_for_selector("#actions button:has-text('Reply')", timeout=15000)
            check(page.locator("#toast").is_hidden(), "a busy Outlook is asked again and the message opens")
            page.unroute("**/api/item*")
            errors[:] = [e for e in errors if "503" not in e]  # the busy answer above, on purpose
            page.click("#back")

            # ---- Outlook busy but the PC has the list in memory: the phone shows it, says so, asks again
            saved_replies = []

            def saved_once(route):
                if saved_replies:
                    return route.continue_()
                saved_replies.append(1)
                reply = route.fetch()
                data = reply.json()
                data.update(saved=True, savedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                route.fulfill(response=reply, json=data)

            page.route("**/api/mail*", saved_once)
            page.click("#refresh")
            expect(page.locator("#savedNote")).to_contain_text("This is the list from", timeout=5000)
            check(page.locator(".row").count() > 3, "a list from the PC's copy is shown with a note that says from when")
            page.screenshot(path=SHOTS / "phone-saved.png")
            expect(page.locator("#savedNote")).to_be_hidden(timeout=15000)
            check(True, "the fresh list replaces it by itself and the note goes away")
            page.unroute("**/api/mail*")
            page.click(".tabs a[data-folder=sent]")
            page.wait_for_selector(".row .subject:has-text('RE: Hinge sample B')")
            check(True, "the reply shows up in Sent")

            # ---- new message: validation, then save as draft
            page.click("#write")
            page.fill("#bodyInput", "Thursday works.")
            page.click("#send")
            check(page.inner_text("#composeError") == "Add at least one recipient.", "sending without a recipient is stopped")
            page.fill("#subjectInput", "Drop test")
            page.screenshot(path=SHOTS / "phone-new.png")
            page.fill("#toInput", "jonas.weber@lumenworks.example")
            page.fill("#subjectInput", "Drop test")
            page.click("#saveDraft")
            expect(page.locator("#toastText")).to_have_text("Saved to Drafts", timeout=30000)
            apart = page.evaluate(
                """() => { const t = document.getElementById('toast').getBoundingClientRect();
                           const w = document.getElementById('write').getBoundingClientRect();
                           return t.bottom <= w.top || t.top >= w.bottom; }"""
            )
            check(apart, "a notice never covers the Write button")
            page.screenshot(path=SHOTS / "phone-saved.png")
            page.click(".tabs a[data-folder=drafts]")
            page.wait_for_selector(".row .subject:has-text('Drop test')")
            check(page.inner_text("#draftCount") == "2", "draft is saved and counted")
            page.locator(".row:has(.subject:text-is('Drop test'))").click()
            page.wait_for_selector("#actions button:has-text('Edit draft')")
            check(True, "a draft opens with Edit draft")
            page.click("#back")

            # ---- search and empty state
            page.click(".tabs a[data-folder=inbox]")
            page.click("#searchToggle")
            page.fill("#q", "gasket")
            expect(page.locator(".row")).to_have_count(1)
            page.fill("#q", "zzzz")
            page.wait_for_selector("#state strong:has-text('No match')")
            check(True, "search narrows the list and says when nothing matches")
            page.screenshot(path=SHOTS / "phone-empty.png")
            page.click("#searchToggle")
            expect(page.locator(".row").nth(5)).to_be_visible()

            # ---- the PC out of reach: plain words and a way back
            expect(page.locator("#toast")).to_be_hidden(timeout=8000)
            page.route("**/api/mail*", lambda route: route.abort())
            page.click("#refresh")
            page.wait_for_selector("#state strong:has-text(\"Can't show your mail\")")
            page.screenshot(path=SHOTS / "phone-offline.png")
            page.unroute("**/api/mail*")
            errors[:] = [e for e in errors if "ERR_FAILED" not in e]  # the blocked request above, on purpose
            page.click("#state button")
            expect(page.locator(".row").nth(5)).to_be_visible()
            check(True, "an unreachable PC is explained and Try again recovers")

            # ---- every width, list and reader, no sideways scroll, tap targets
            for width in WIDTHS:
                page.set_viewport_size({"width": width, "height": 844 if width < 860 else 900})
                page.goto(page.url.split("#")[0] + "#/inbox")
                page.wait_for_selector(".row")
                over_list = overflow(page)
                page.locator(".row").first.click()
                page.wait_for_selector("#letter:not([hidden]) #body > *")
                page.wait_for_timeout(250)
                check(over_list <= 0 and overflow(page) <= 0, f"no sideways overflow at {width}px")
                page.screenshot(path=SHOTS / f"w{width}-reader.png")
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(page.url.split("#")[0] + "#/inbox")
            page.wait_for_selector(".row")
            small = page.evaluate(
                """() => [...document.querySelectorAll('button, a, input, textarea')]
                    .filter(e => e.offsetParent !== null)
                    .map(e => [e.id || e.className || e.textContent.trim().slice(0, 20), Math.round(e.getBoundingClientRect().height)])
                    .filter(pair => pair[1] < 44)"""
            )
            check(not small, f"phone tap targets are at least 44 px {small if small else ''}")

            # ---- dark theme, phone and desktop
            page.click("#theme")
            check(page.get_attribute("html", "data-theme") == "dark", "theme button switches to dark")
            page.wait_for_timeout(200)
            page.screenshot(path=SHOTS / "phone-inbox-dark.png")
            page.set_viewport_size({"width": 1280, "height": 800})
            page.locator(".row").nth(1).click()
            page.wait_for_selector("#letter:not([hidden]) #body > *")
            page.click("#write")
            page.wait_for_timeout(300)
            page.screenshot(path=SHOTS / "desktop-dark.png")
            page.click("#discard")
            page.click("#theme")
            page.locator(".row").first.click()
            page.wait_for_selector("#letter:not([hidden]) iframe")
            page.wait_for_timeout(300)
            reply_box = page.locator("#actions .primary").bounding_box()
            check(reply_box["y"] + reply_box["height"] <= 800, "desktop keeps Reply in view without scrolling")
            page.screenshot(path=SHOTS / "desktop-light.png")

            # ---- Autopilot on the paired phone: the demo mailbox gets one new mail per look
            autopilot("switch", on=True)
            for look in range(5):
                before = len(desk("/api/auto")["events"])
                autopilot("check")
                for _ in range(40):
                    state = desk("/api/auto")
                    if not state["busy"] and (len(state["events"]) > before or look > 3):
                        break
                    time.sleep(0.25)
            kinds = [event["kind"] for event in desk("/api/auto")["events"]]
            check({"sent", "held", "left"} <= set(kinds), f"demo Autopilot answered one, kept one, left one ({kinds})")
            origin = page.url.split("#")[0].rstrip("/")
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(origin + "/auto.html")
            page.wait_for_selector("#held .letter")
            check(page.inner_text("#headline") == "On", "Autopilot page says it is on")
            letter = page.inner_text("#held .letter")
            check("Invoice 2291" in letter and "money" in letter, "the invoice waits with the reason it was kept")
            check(page.locator("#feed .entry.sent blockquote").count() == 1, "the answer that went out is shown in full")
            check(page.locator(".primary:visible").count() == 1, "one primary button on the Autopilot page")
            page.screenshot(path=SHOTS / "phone-autopilot.png", full_page=True)
            for width in (320, 390, 1024):
                page.set_viewport_size({"width": width, "height": 844})
                page.wait_for_timeout(150)
                check(overflow(page) <= 0, f"Autopilot page has no sideways overflow at {width}px")
            page.set_viewport_size({"width": 390, "height": 844})
            small = page.evaluate(
                """() => [...document.querySelectorAll('button, a, textarea, .choice label')]
                    .filter(e => e.offsetParent !== null)
                    .map(e => [e.id || e.className || e.textContent.trim().slice(0, 20), Math.round(e.getBoundingClientRect().height)])
                    .filter(pair => pair[1] < 44)"""
            )
            check(not small, f"Autopilot tap targets are at least 44 px {small if small else ''}")
            page.check('input[name="mode"][value="review"]')
            expect(page.locator("#toast")).to_contain_text("Every answer waits for you")
            check(desk("/api/auto")["mode"] == "review", "Show me first reaches the PC")
            page.fill("#held textarea", "Hello,\n\nI will check it against my order.\n\nNadia")
            page.click("#held .primary")
            expect(page.locator("#held .letter")).to_have_count(0, timeout=10000)
            expect(page.locator("#feed .entry.sent blockquote").first).to_contain_text("against my order")
            check(True, "an edited draft is sent from the phone and shows under What it did")
            page.click("#switch")
            expect(page.locator("#headline")).to_have_text("Paused")
            check(desk("/api/auto")["on"] is False, "Pause reaches the PC")
            page.screenshot(path=SHOTS / "phone-autopilot-paused.png", full_page=True)

            # ---- the public page: the demo, then a relay that is only ever handed ciphertext
            from titip import remote

            public = serve_public_pages()
            far = context.new_page()
            far.on("console", lambda message: errors.append("far: " + message.text) if message.type == "error" else None)
            far.on("pageerror", lambda error: errors.append("far: " + str(error)))
            far.goto(public + "/auto.html?demo")
            far.wait_for_selector("#held .letter")
            far.click("#held button:has-text(\"Don't answer\")")
            expect(far.locator("#held .letter")).to_have_count(0)
            check(far.locator("#demoNote").is_visible(), "the public Autopilot page is a demo that says so")

            key = os.urandom(32)
            wire: list[str] = []
            orders: list[dict | None] = []

            def relay(route) -> None:
                request, cors = route.request, {"Access-Control-Allow-Origin": "*"}
                if request.method == "GET":
                    sealed = remote.seal(key, {**remote.slim(desk("/api/auto")), "published": int(time.time()) - 5})
                    wire.append(sealed)
                    return route.fulfill(status=200, headers=cors, content_type="text/plain", body=sealed)
                wire.append(request.post_data or "")
                orders.append(remote.unseal(key, request.post_data or ""))
                route.fulfill(status=200, headers=cors, content_type="text/plain", body="ok")

            far.route("https://relay.invalid/**", relay)
            ticket = remote.b64(json.dumps({"u": "https://relay.invalid", "c": "c" * 22, "k": remote.b64(key)}).encode())
            far.goto("about:blank")
            far.goto(f"{public}/auto.html#r={ticket}")
            far.wait_for_selector("#feed .entry")
            check("#" not in far.url, "the key leaves the address bar once the phone has kept it")
            check(far.inner_text("#headline") == "Paused" and "relay" in far.inner_text("#far"),
                  "the phone decrypts what the PC wrote and says it came through the relay")
            far.click("#switch")
            for _ in range(40):
                if orders:
                    break
                far.wait_for_timeout(100)
            order = orders[0] if orders else None
            check(bool(order) and order.get("name") == "switch" and order.get("on") is True
                  and bool(order.get("nonce")) and abs(time.time() - order.get("at", 0)) < 120,
                  "the phone's order decrypts on the PC side, with a nonce and a time")
            check(far.inner_text("#headline") == "On", "the switch shows at once while the PC picks the order up")
            check(all("Invoice" not in text and "nadia" not in text and "switch" not in text for text in wire),
                  "nothing readable passes through the relay")
            far.set_viewport_size({"width": 390, "height": 844})
            far.screenshot(path=SHOTS / "phone-autopilot-far.png", full_page=True)
            far.close()

            # ---- mail from any network: the public page asks, the stand-in relay passes it on, this Titip answers
            settings = json.loads((Path(home) / "remote.json").read_text(encoding="utf-8"))
            check(desk("/desk/status")["far"]["mail"] is True and settings.get("mail") is True and bool(settings.get("key")),
                  "Titip starts with Mail from anywhere on when its settings say so")

            def through(route) -> None:
                request = route.request
                status, text = stand_in.handle(request.method, "/" + request.url.split("/", 3)[3], request.post_data or "", "")
                route.fulfill(status=status, headers={"Access-Control-Allow-Origin": "*"}, content_type="text/plain", body=text)

            away = context.new_page()
            away.set_viewport_size({"width": 390, "height": 844})
            away.on("console", lambda message: errors.append("away: " + message.text) if message.type == "error" else None)
            away.on("pageerror", lambda error: errors.append("away: " + str(error)))
            away.route("https://relay.invalid/**", through)
            real_ticket = remote.b64(json.dumps(
                {"u": "https://relay.invalid", "c": settings["channel"], "k": settings["key"]}).encode())
            away.goto(f"{public}/auto.html#r={real_ticket}")
            away.wait_for_selector("#feed .entry")
            expect(away.locator("#mailTab")).to_be_visible(timeout=20000)
            check(True, "the Autopilot page shows the Mail tab once the PC says Mail from anywhere is on")
            away.click("#mailTab")
            away.wait_for_selector(".row", timeout=40000)
            check(away.locator(".row").count() >= 6 and len(stand_in.asked) >= 2,
                  f"the inbox arrives through the relay ({away.locator('.row').count()} rows, {len(stand_in.asked)} asks)")
            away.click(".tabs a[data-folder=sent]")
            away.wait_for_selector(".row .subject:has-text('RE: Hinge sample B')", timeout=40000)
            check(True, "Sent through the relay shows what this PC sent earlier in the run")
            away.click(".tabs a[data-folder=inbox]")
            away.wait_for_selector(".row .subject:has-text('Hinge sample B')", timeout=40000)
            away.locator(".row").first.click()
            away.wait_for_selector("#letter:not([hidden]) iframe", timeout=40000)
            check(away.frame_locator("#body iframe").locator("table").count() == 1 and away.locator("#files a").count() == 2,
                  "a formatted message opens through the relay with its attachments listed")
            check(overflow(away) <= 0, "mail through the relay does not overflow sideways at 390")
            away.screenshot(path=SHOTS / "phone-mail-far.png")
            with away.expect_download(timeout=40000) as saving:
                away.locator("#files a").first.click()
            saved = Path(saving.value.path()).read_text(encoding="utf-8")
            check(saved.startswith("Demo attachment: ") and saving.value.suggested_filename in saved,
                  f"an attachment comes through the relay and is saved ({saving.value.suggested_filename})")
            away.click("#actions button:has-text('Reply')")
            away.fill("#bodyInput", "Understood from the road. Keep unit seven aside for me.")
            away.click("#send")
            away.wait_for_selector("#toast:not([hidden])")
            check("Sending in" in away.inner_text("#toastText"), "a send through the relay waits behind the same undo countdown")
            expect(away.locator("#toastText")).to_have_text("Sent to Mateo Silva", timeout=60000)
            away.click("#back")
            away.click(".tabs a[data-folder=sent]")
            away.wait_for_selector(".row .preview:has-text('Understood from the road')", timeout=40000)
            check(True, "the reply written away from home is sent by the PC and shows in Sent")
            sends = [text for text in stand_in.wire if "Understood" in str(remote.unseal(remote.unb64(settings["key"]), text))]
            check(len(sends) >= 1 and len(stand_in.wire) > 10, f"the PC could open what the phone sealed ({len(stand_in.wire)} bodies passed)")
            sealed_only = all(re.fullmatch(r"[A-Za-z0-9_-]+", text) for text in stand_in.wire)
            readable = [word for text in stand_in.wire if re.fullmatch(r"[A-Za-z0-9_-]+", text)
                        for word in (b"Hinge", b"Mateo", b"Understood", b"torque", b"Demo attachment", b"inbox", b"route")
                        if word in remote.unb64(text)]
            check(sealed_only and not readable,
                  "the relay only ever held sealed text: no subject, name, body, file or folder")
            check(not stand_in.answers and stand_in.handle("GET", f"/c/{settings['channel']}/answer/{stand_in.asked[0]}", "", "")[0] == 204,
                  "every answer was handed out once and is gone from the relay")
            away.close()

            # ---- the desk page on the PC
            desk_page = context.new_page()
            desk_page.set_viewport_size({"width": 900, "height": 1000})
            desk_page.on("console", lambda message: errors.append("desk: " + message.text) if message.type == "error" else None)
            desk_page.on("pageerror", lambda error: errors.append("desk: " + str(error)))
            desk_page.goto(f"http://127.0.0.1:{DESK_PORT}/")
            desk_page.wait_for_selector("#mail .good")
            expect(desk_page.locator("#mailFarLine")).to_contain_text("A phone last asked at")
            check(desk_page.inner_text("#mailFar") == "Switch off", "the PC page says Outlook mail from anywhere is on and when a phone last asked")
            desk_page.click("#mailFar")
            expect(desk_page.locator("#mailFarLine")).to_contain_text("Outlook mail stays on your own network.")
            check(desk("/desk/status")["far"]["mail"] is False
                  and json.loads((Path(home) / "remote.json").read_text(encoding="utf-8"))["mail"] is False,
                  "Switch off on the PC page stops mail through the relay and is remembered")
            asked: list[str] = []
            desk_page.once("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
            desk_page.click("#mailFar")
            desk_page.wait_for_timeout(300)
            check(len(asked) == 1 and "any network" in asked[0] and desk("/desk/status")["far"]["mail"] is False,
                  "switching it on asks first, and saying no leaves it off")
            expect(desk_page.locator("#updateLine")).to_contain_text("not installed with git")
            check(desk_page.locator("#updateSwitch").is_disabled() and desk_page.locator("#updateNow").is_disabled(),
                  "the demo never updates itself, and the PC page says so")
            desk_page.click("#pair")
            desk_page.wait_for_selector("#qr svg")
            check("Browser" in desk_page.inner_text("#devices"), "desk page lists the paired device")
            # the screenshot hides this PC's network address, so it is safe to share
            desk_page.screenshot(
                path=SHOTS / "desk.png", full_page=True, mask_color="#d9cfbd",
                mask=[desk_page.locator("#url"), desk_page.locator("#addresses")],
            )

            # ---- unpair cuts the phone off
            desk("/desk/unpair", post=True)
            page.goto(page.url.split("#")[0])
            check("not paired" in page.inner_text("body"), "after Unpair all, the phone is locked out")
            errors[:] = [e for e in errors if "401" not in e]
            check(not errors, f"no console or page errors {errors[:3] if errors else ''}")

            # ---- Stop Titip ends the process
            desk_page.click("#quit")
            expect(desk_page.locator("#stopped")).to_contain_text("Stopped")
            try:
                check(server.wait(timeout=10) == 0, "Stop Titip on the PC page ends the program")
            except subprocess.TimeoutExpired:
                check(False, "Stop Titip on the PC page ends the program")
    finally:
        server.terminate()

    step(f"\n{'PASS' if not problems else 'FAILED: ' + '; '.join(problems)}  (screenshots: {SHOTS})")
    sys.stdout.flush()
    os._exit(1 if problems else 0)


if __name__ == "__main__":
    main()
