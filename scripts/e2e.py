"""End-to-end check in a real browser against the demo mailbox.

    python scripts/e2e.py [folder-for-screenshots]

Starts Titip with invented mail on spare ports, pairs a browser through the real QR link (HTTPS on this
PC's network address), then reads, answers, undoes, sends, saves a draft and searches, at phone and
desktop sizes, light and dark. Changes who a Reply all goes to, attaches files, forwards without one of
the original's files, opens the Outbox and stops a mail there, reads the calendar and accepts an
invitation. Opens a folder of your own, flags, labels, moves and marks a mail, searches in the text,
writes with the signature and high importance, and makes, changes and cancels a meeting. Formats a
mail and asks for a read receipt, makes, renames and takes out a folder, switches a rule on and makes
one, makes a category, reads a shared calendar, and makes an entry that repeats, changes one time of it,
deletes the series and changes who is invited to a meeting. Makes, changes, ticks off and deletes a
task, a note and a contact, writes to a contact, sends a mail for later and stops it in the Outbox,
switches the automatic replies, and lets a reminder ring on the phone. Narrows the list to what is
unread or flagged, pushes rows aside, changes the subject of a forward, opens what Outlook knows of a
sender, makes an entry over three days with an optional guest and one every second week, and answers
an invitation with a note. Then Autopilot: on the paired phone, on the public page (demo), and
through a stand-in relay that only ever gets ciphertext. Then mail from any network, a full round
trip: the code for any network opens the public mail app, a stand-in relay with the worker's rules
passes sealed text, this Titip answers. Then the iPhone Home Screen: a browser with an iPhone's name
scans the code and gets the page that carries the key, and a second browser with empty storage, the
icon, opens that page's address and reads, answers and unlinks. Fails on any console error, page
error or sideways overflow.
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
    with opener.open(request, timeout=30) as reply:  # a full PC can keep the server waiting for its turn
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
        self.given: list[str] = []
        self.largest_ask = 0
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
                self.largest_ask = max(self.largest_ask, len(body))
                if len(body) > 200000:  # MAX_ASK in worker/titip-relay.js
                    return 413, "Too large."
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
                    self.given.append(parts[4])
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
            check(page.locator("#toField .chipName").all_inner_texts() == ["Mateo Silva"] and page.locator("#ccField .chip").count() == 0
                  and page.evaluate("document.activeElement.id") == "bodyInput",
                  "a reply shows who it goes to as a label that can be changed, and the cursor is in the text")
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
            page.screenshot(path=SHOTS / "phone-pc-copy.png")
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

            # ---- To suggests people by name, with their address, the way Outlook does
            page.locator("#toInput").press_sequentially("pri")
            expect(page.locator("#people .person")).to_have_count(2)
            check(page.locator("#people .personName").all_inner_texts() == ["Priya Nair", "Priscilla Tan"]
                  and page.locator("#people .personAddress").first.inner_text() == "priya.nair@lumenworks.example",
                  "typing in To suggests at once the people of your mail whose name starts that way, each with the address")
            expect(page.locator("#people .peopleNote")).to_have_text("From the address book", timeout=10000)
            check(page.locator("#people .personName").all_inner_texts() == ["Priya Nair", "Priscilla Tan", "Pritam Das", "Priyanka Rao"]
                  and page.locator("#people .person.on .personName").inner_text() == "Priya Nair",
                  "a moment later the PC's address book adds the people your mail does not hold, nobody twice")
            low = [round(box["height"]) for box in (button.bounding_box() for button in page.locator("#people .person").all())
                   if box["height"] < 44]
            check(not low and overflow(page) <= 0, f"suggestions are at least 44 px high and do not overflow sideways {low if low else ''}")
            page.screenshot(path=SHOTS / "phone-people.png")
            page.locator("#people .person").first.click()
            chips = page.locator("#toField .chip")
            check(chips.locator(".chipName").all_inner_texts() == ["Priya Nair"]
                  and chips.first.get_attribute("title") == "priya.nair@lumenworks.example"
                  and page.input_value("#toInput") == "" and page.locator("#people").is_hidden()
                  and page.evaluate("document.activeElement.id") == "toInput",
                  "a tap turns the name into a rounded label that knows the address, and the cursor stays for the next one")
            page.locator("#toInput").press_sequentially("web")
            expect(page.locator("#people .personName")).to_have_text(["Jonas Weber"])
            page.keyboard.press("Enter")
            check(chips.locator(".chipName").all_inner_texts() == ["Priya Nair", "Jonas Weber"]
                  and page.locator("#composer").is_visible() and page.locator("#toast").is_hidden(),
                  "Enter takes the suggested name and does not send")
            page.locator("#toInput").press_sequentially("okon")
            expect(page.locator("#people .peopleNote")).to_have_text("From the address book", timeout=10000)
            expect(page.locator("#people .personName")).to_have_text(["Okonkwo, Chidi"])
            page.locator("#people .person").first.click()
            check(chips.locator(".chipName").all_inner_texts() == ["Priya Nair", "Jonas Weber", "Okonkwo, Chidi"]
                  and chips.nth(2).get_attribute("title") == "chidi.okonkwo@lumenworks.example",
                  "someone found only in the address book can be picked, and a name with a comma stays one person")
            page.locator("#toInput").press_sequentially("new.person@lumenworks.example;")
            expect(chips).to_have_count(4)
            check(chips.nth(3).inner_text().startswith("new.person@lumenworks.example") and page.input_value("#toInput") == "",
                  "an address typed by hand becomes a label at the semicolon")
            page.keyboard.press("Backspace")
            expect(chips).to_have_count(3)
            page.locator("#toInput").press_sequentially("Tomas")
            page.keyboard.press("Tab")
            expect(page.locator("#toField .chip.asName .chipName")).to_have_text(["Tomas"])
            check(page.input_value("#toInput") == "", "Backspace takes the last label away, and a bare name is kept for Outlook to look up")
            page.locator("#toField .chip.asName").click()
            expect(chips).to_have_count(3)
            small_chips = [round(box["height"]) for box in (chip.bounding_box() for chip in chips.all()) if box["height"] < 32]
            check(not small_chips and overflow(page) <= 0, f"a tap on a label removes it; labels do not overflow sideways {small_chips if small_chips else ''}")
            page.keyboard.press("Escape")
            expect(page.locator("#toastText")).to_contain_text("Kept on this phone")
            page.click("#toastAction")
            expect(chips).to_have_count(3)
            check(chips.locator(".chipName").all_inner_texts() == ["Priya Nair", "Jonas Weber", "Okonkwo, Chidi"]
                  and page.input_value("#subjectInput") == "Drop test",
                  "a message put aside comes back with its people")
            page.locator("#ccInput").press_sequentially("okafor")
            expect(page.locator("#people .personName")).to_have_text(["Okafor Supplies", "Marek Novak"])
            page.keyboard.press("Escape")
            check(page.locator("#people").is_hidden() and page.locator("#composer").is_visible(),
                  "Cc suggests too (the address matches as well), and Escape closes the suggestions, not the message")
            page.fill("#ccInput", "")
            page.screenshot(path=SHOTS / "phone-new.png")
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
            check("priya.nair@lumenworks.example; jonas.weber@lumenworks.example; chidi.okonkwo@lumenworks.example"
                  in page.inner_text(".row:has(.subject:text-is('Drop test')) .who"),
                  "the PC was handed the three addresses, not the names shown on the labels")
            page.locator(".row:has(.subject:text-is('Drop test'))").click()
            page.wait_for_selector("#actions button:has-text('Edit draft')")
            page.click("#actions button:has-text('Edit draft')")
            expect(page.locator("#toField .chip")).to_have_count(3)
            check(page.input_value("#bodyInput") == "Thursday works.", "a draft opens with Edit draft, its people as labels")
            page.click("#discard")
            page.click("#back")

            # ---- 0.9.0: who an answer goes to can be changed, Bcc, and files that go along
            posted: list[tuple[str, dict]] = []
            page.on("request", lambda request: posted.append((request.url.rsplit("/", 1)[1], request.post_data_json))
                    if request.method == "POST" and "/api/" in request.url else None)
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            page.locator(".row").first.click()
            page.wait_for_selector("#actions button:has-text('Reply all')")
            page.click("#actions button:has-text('Reply all')")
            to_chips, cc_chips = page.locator("#toField .chip"), page.locator("#ccField .chip")
            check(to_chips.locator(".chipName").all_inner_texts() == ["Mateo Silva"]
                  and cc_chips.locator(".chipName").all_inner_texts() == ["Priya Nair"] and page.locator("#bccField").is_hidden(),
                  "Reply all lists everyone it goes to, To and Cc, each as a label")
            cc_chips.first.click()
            page.locator("#ccInput").press_sequentially("jon")
            expect(page.locator("#people .personName").first).to_have_text("Jonas Weber")
            page.locator("#people .person").first.click()
            page.click("#bccToggle")
            page.locator("#bccInput").press_sequentially("quiet@lumenworks.example;")
            check(cc_chips.locator(".chipName").all_inner_texts() == ["Jonas Weber"]
                  and page.locator("#bccField .chipName").all_inner_texts() == ["quiet@lumenworks.example"],
                  "somebody is taken out of Cc, somebody else put in, and Bcc opens when asked for")
            page.set_input_files("#fileInput", [
                {"name": "cam-close-up.jpg", "mimeType": "image/jpeg", "buffer": os.urandom(700_000)},
                {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"Section unit seven first."},
                {"name": "wrong-file.zip", "mimeType": "application/zip", "buffer": b"PK"},
            ])
            files = page.locator("#attached .fileChip")
            expect(files).to_have_count(3)
            files.nth(2).click()
            check(page.locator("#attached .fileName").all_inner_texts() == ["cam-close-up.jpg", "notes.txt"]
                  and "684 KB" in files.first.inner_text(),
                  "files picked on the phone are listed with their size, and a tap takes one out again")
            low = [round(box["height"]) for box in (chip.bounding_box() for chip in files.all()) if box["height"] < 44]
            check(not low and overflow(page) <= 0 and page.locator("#attach").bounding_box()["height"] >= 44,
                  f"the file labels are at least 44 px high and nothing overflows sideways {low if low else ''}")
            page.fill("#bodyInput", "Thanks. A closer photo of the cam is attached.")
            page.screenshot(path=SHOTS / "phone-answer-files.png")
            too_large = Path(home) / "whole-test-video.mov"  # a file on disk: its 15 MB are never read, only its size is asked
            with too_large.open("wb") as empty:
                empty.truncate(15_000_001)
            page.set_input_files("#fileInput", str(too_large))
            check("does not fit" in page.inner_text("#composeError") and files.count() == 2,
                  "a file that would make the mail too large is refused with the reason, and the others stay")
            posted.clear()
            page.click("#send")
            expect(page.locator("#toastText")).to_have_text("Sent to Mateo Silva and 2 more", timeout=30000)
            pieces = [body for name, body in posted if name == "upload"]
            sent_as = next(body for name, body in posted if name == "send")
            check(len(pieces) == 3 and [piece["index"] for piece in pieces] == [0, 1, 0] and [piece["last"] for piece in pieces] == [False, True, True],
                  "a file goes to the PC in pieces, each small enough for one request")
            check(sent_as["recipients"] == "exact" and sent_as["to"] == ["mateo.silva@lumenworks.example"]
                  and sent_as["cc"] == ["jonas.weber@lumenworks.example"] and sent_as["bcc"] == ["quiet@lumenworks.example"]
                  and len(sent_as["files"]) == 2 and "kept" not in sent_as,
                  "the PC is handed exactly the people on the labels and the names of the two files")
            page.click("#back")
            page.click(".tabs a[data-folder=sent]")
            page.wait_for_selector(".row .preview:has-text('A closer photo')")
            page.locator(".row:has(.preview:has-text('A closer photo'))").click()
            page.wait_for_selector("#files a")
            held = ["".join(text.split()) for text in page.locator("#files a").all_inner_texts()]
            check(held == ["cam-close-up.jpg684KB", "notes.txt1KB"] and "quiet@lumenworks.example" in page.inner_text("#bccRow"),
                  f"the sent mail holds both files whole, and shows who was on Bcc {held if len(held) != 2 else ''}")
            page.click("#back")

            # ---- forward: the original's files are listed and one can be left behind
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            page.locator(".row").first.click()
            page.wait_for_selector("#actions button:has-text('Forward')")
            page.click("#actions button:has-text('Forward')")
            check(page.locator("#attached .fileName").all_inner_texts() == ["hinge-B-torque.xlsx", "cam-scuffing.jpg"]
                  and page.locator("#toField").is_visible() and page.input_value("#subjectInput") == "FW: Hinge sample B: torque results",
                  "a forward lists the files of the original that go along")
            page.locator("#attached .fileChip").nth(1).click()
            page.locator("#toInput").press_sequentially("pri")
            page.locator("#people .person").first.click()
            posted.clear()
            page.click("#send")
            expect(page.locator("#toastText")).to_have_text("Sent to Priya Nair", timeout=30000)
            forwarded = next(body for name, body in posted if name == "send")
            check(forwarded["drop"] == [{"index": 2, "name": "cam-scuffing.jpg"}] and "files" not in forwarded,
                  "the PC is told which file of the original stays behind")
            page.click("#back")
            page.click(".tabs a[data-folder=sent]")
            page.wait_for_selector(".row .subject:has-text('FW: Hinge sample B')")
            page.locator(".row:has(.subject:has-text('FW: Hinge sample B'))").click()
            page.wait_for_selector("#files a")
            check(page.locator("#files a").count() == 1 and "hinge-B-torque.xlsx" in page.inner_text("#files"),
                  "the forward left with one file, not two")
            page.click("#back")

            # ---- every folder under the folder's name; a mail in the Outbox can be stopped
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            expect(page.locator("#outboxNote")).to_be_visible(timeout=10000)
            check("1 mail is waiting in the Outbox" in page.inner_text("#outboxNote"), "a mail that has not left yet is said over the list")
            page.click("#folderPick")
            check(page.locator("#folderMenu a").all_inner_texts()[:3] == ["Inbox", "Drafts", "Sent"]
                  and page.locator("#folderMenu a[data-folder=outbox]").inner_text().split() == ["Outbox", "1"]
                  and page.locator("#folderMenu a[aria-current=page]").inner_text() == "Inbox" and overflow(page) <= 0,
                  "the folder's name opens the list of every folder, with the one you are in marked")
            page.screenshot(path=SHOTS / "phone-folders.png")
            page.keyboard.press("Escape")
            check(page.locator("#folderMenu").is_hidden(), "Escape closes the list of folders")
            page.click("#outboxNote a")
            page.wait_for_selector(".row .subject:has-text('RE: Quote Q-2291')")
            check(page.inner_text("#folderName") == "Outbox" and page.locator("#outboxNote").is_hidden()
                  and "Okafor Supplies" in page.inner_text(".row .who"),
                  "the Outbox lists the mail by who it goes to")
            page.locator(".row").first.click()
            page.wait_for_selector("#body pre")
            check("Waiting in Outlook's Outbox" in page.inner_text("#body") and page.locator("#actions").is_hidden()
                  and page.inner_text("#remove") == "Delete",
                  "a mail in the Outbox says that it waits, and can be deleted but not answered or changed")
            page.click("#remove")
            expect(page.locator("#toastText")).to_contain_text("Deleting in")
            page.click("#toastAction")
            page.wait_for_timeout(3600)
            check(page.locator("#letter").is_visible() and page.locator("#toast").is_hidden(), "Undo within three seconds and nothing is deleted")
            page.click("#remove")
            expect(page.locator("#toastText")).to_have_text("Deleted. It will not be sent.", timeout=30000)
            page.wait_for_selector("#state strong:has-text('Nothing is waiting')")
            check(page.locator("#letter").is_hidden() and page.locator(".row").count() == 0, "deleted from the Outbox, the mail is gone from it")
            page.click("#folderPick")
            page.click("#folderMenu a[data-folder=deleted]")
            page.wait_for_selector(".row .subject:has-text('RE: Quote Q-2291')")
            page.locator(".row").first.click()
            page.wait_for_selector("#body pre")
            check(page.locator("#remove").is_hidden(), "it is in Deleted, where nothing can be removed for good from the phone")
            page.click("#back")

            # ---- the calendar, and an invitation answered from it
            page.click(".tabs a[data-folder=calendar]")
            page.wait_for_selector(".row.event")
            days = page.locator("#rows .day").all_inner_texts()
            check(page.locator(".row.event").count() >= 6 and days[0].upper().startswith("TODAY") and days[1].upper().startswith("TOMORROW")
                  and page.locator("#searchToggle").is_hidden() and page.inner_text("#more") == "Show later days",
                  f"the calendar lists the days ahead under Today, Tomorrow and their dates ({page.locator('.row.event').count()} entries)")
            invited = page.locator(".row.event.unread")
            check(invited.count() == 1 and "Drop test, corner drops" in invited.inner_text() and "INVITED" in invited.inner_text().upper()
                  and "Jonas Weber" in invited.inner_text(),
                  "an invitation nobody answered yet is marked the way unread mail is, with who sent it")
            check(overflow(page) <= 0, "the calendar does not overflow sideways at 390")
            page.screenshot(path=SHOTS / "phone-calendar.png")
            invited.click()
            page.wait_for_selector("#body pre")
            check("high-speed camera" in page.inner_text("#body") and page.inner_text("#inviteSaid") == "You have not answered yet."
                  and "Drop tower" in page.inner_text("#inviteWhere") and " to " in page.inner_text("#inviteWhen")
                  and "Rin Aoki" in page.inner_text("#to") and "Priya Nair" in page.inner_text("#cc")
                  and page.locator("#actions").is_hidden() and page.locator("#remove").is_hidden(),
                  "a meeting opens with when, where, who is invited and its text")
            page.screenshot(path=SHOTS / "phone-meeting.png")
            page.click("#answers button[data-answer=accept]")
            expect(page.locator("#toastText")).to_contain_text("Accepting in")
            expect(page.locator("#toastText")).to_have_text("Accepted. Jonas Weber is told.", timeout=30000)
            expect(page.locator("#inviteSaid")).to_have_text("You accepted.")
            check(page.get_attribute("#answers button[data-answer=accept]", "aria-pressed") == "true", "Accept waits behind Undo, then the meeting says You accepted")
            page.click("#back")
            expect(page.locator(".row.event.unread")).to_have_count(0)
            shown = page.locator(".row.event").count()
            page.click("#more")
            expect(page.locator("#refresh.busy")).to_have_count(0)
            check(page.locator(".row.event").count() >= shown and page.locator("#state strong").count() == 0
                  and page.locator("#more").is_visible(), "Show later days adds the next two weeks")

            # ---- the page can be newer than the Titip on the PC: what is new says so instead of waiting for an answer
            def older_pc(route) -> None:
                reply = route.fetch()
                route.fulfill(response=reply, json={**reply.json(), "version": "0.8.3"})

            page.route("**/api/state", older_pc)
            page.click("#refresh")
            expect(page.locator("#state")).to_contain_text("Titip on your PC is version 0.8.3 and this needs 0.9.0", timeout=10000)
            check(page.locator(".row").count() == 0, "with an older Titip on the PC the calendar says that it is on its way")
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            page.click("#write")
            page.click("#attach")
            check("this needs 0.9.0" in page.inner_text("#composeError"), "and so does Attach a file, while the inbox and writing work as before")
            page.click("#discard")
            page.unroute("**/api/state")
            page.click("#refresh")
            page.click(".tabs a[data-folder=calendar]")
            page.wait_for_selector(".row.event", timeout=15000)
            check(True, "once the PC is on the same version the calendar is there again")

            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Drop test, corner drops')")
            page.locator(".row:has(.subject:has-text('Drop test, corner drops'))").click()
            page.wait_for_selector("#invite:not([hidden])")
            check(page.inner_text("#inviteSaid") == "You accepted." and page.locator("#answers button").count() == 3
                  and page.locator("#actions button").count() == 3,
                  "the invitation in the inbox shows the same meeting and answer, next to Reply")
            page.click("#back")

            # ---- the rest of Outlook: marks in the list, your own folders, More (flag, categories, move, unread)
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            check(page.locator(".row:has(.subject:has-text('Drop test slot')) .flag").count() == 1
                  and page.locator(".row:has(.subject:has-text('Hinge sample B')) .bang").count() == 1,
                  "a flagged mail and one of high importance are marked in the list")
            read_there = page.evaluate(
                "fetch('/api/mail?folder=inbox&limit=1', {headers: {'X-Titip': '1'}}).then(reply => reply.json()).then(page => page.mail[0].unread)")
            check(read_there is False, "a mail read on the phone is marked read on the PC too")
            page.click("#folderPick")
            expect(page.locator("#ownFolders a")).to_have_count(3, timeout=10000)
            first_own = page.locator("#ownFolders a").first.inner_text()
            check("Hinge project" in first_own and "Inbox" in first_own and page.inner_text("#ownNote").upper() == "YOUR FOLDERS" and overflow(page) <= 0,
                  "the list of folders goes on with your own, each with where it is")
            page.screenshot(path=SHOTS / "phone-own-folders.png")
            page.locator("#ownFolders a").first.click()
            page.wait_for_selector(".row .subject:has-text('Hinge sample A')")
            check(page.inner_text("#folderName") == "Hinge project" and page.locator(".row").count() == 2,
                  "a folder of your own opens like any other")
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Thanks for covering')")
            page.locator(".row:has(.subject:has-text('Thanks for covering'))").click()
            page.wait_for_selector("#actions button:has-text('Reply')")
            page.click("#moreToggle")
            check(page.locator("#moreMenu button").all_inner_texts() == ["Mark as unread", "Flag", "Move to a folder", "Categories", "Always move mail from this sender"]
                  and overflow(page) <= 0, "More, next to Delete, lists what else can be done with the message")
            page.screenshot(path=SHOTS / "phone-more.png")
            page.click("#doFlag")
            expect(page.locator("#toastText")).to_have_text("Flagged", timeout=15000)
            expect(page.locator("#tags")).to_have_text("Flagged")
            page.click("#moreToggle")
            check(page.inner_text("#doFlag") == "Clear the flag", "a flag is set, shown under the sender, and can be cleared again")
            page.click("#doLabel")
            expect(page.locator("#pickerList .pickOne")).to_have_count(4, timeout=10000)
            page.locator("#pickerList .pickOne:has-text('Customer')").click()
            page.wait_for_timeout(200)
            page.screenshot(path=SHOTS / "phone-categories.png")
            low = [round(box["height"]) for box in (one.bounding_box() for one in page.locator("#pickerList .pickOne").all()) if box["height"] < 44]
            page.click("#pickerDone")
            expect(page.locator("#toastText")).to_have_text("Categories saved", timeout=15000)
            check("Customer" in page.inner_text("#tags") and "Flagged" in page.inner_text("#tags") and not low,
                  f"Outlook's categories can be ticked for a message {low if low else ''}")
            page.click("#moreToggle")
            page.click("#doMove")
            expect(page.locator("#pickerList .pickOne")).to_have_count(4, timeout=10000)
            places = [text.split("\n")[0] for text in page.locator("#pickerList .pickOne").all_inner_texts()]
            check(places == ["Hinge project", "Suppliers", "Archive", "Junk"], f"Move lists the folders it can go to, not the one it is in {places}")
            page.locator("#pickerList .pickOne:has-text('Archive')").click()
            expect(page.locator("#toastText")).to_contain_text("Moving to Archive in")
            expect(page.locator("#toastText")).to_have_text("Moved to Archive", timeout=30000)
            expect(page.locator(".row:has(.subject:has-text('Thanks for covering'))")).to_have_count(0)
            check(page.locator("#letter").is_hidden(), "moved behind Undo, the mail is gone from the inbox")
            page.click("#folderPick")
            page.locator("#ownFolders a:has-text('Archive')").click()
            page.wait_for_selector(".row .subject:has-text('Thanks for covering')")
            check(page.locator(".row .flag").count() == 1, "and is in the folder it was moved to, flag and all")
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Lab 2 air')")
            page.locator(".row:has(.subject:has-text('Lab 2 air'))").click()
            page.wait_for_selector("#actions button:has-text('Reply')")
            page.click("#moreToggle")
            page.click("#doUnread")
            expect(page.locator("#toastText")).to_have_text("Marked as unread", timeout=15000)
            expect(page.locator(".row.unread:has(.subject:has-text('Lab 2 air'))")).to_have_count(1)
            check(page.locator("#letter").is_hidden(), "Mark as unread goes back to the list, where the mail is bold again")

            # ---- a search can look in the text of the mail too
            page.click("#searchToggle")
            page.fill("#q", "shore")
            page.wait_for_selector("#state strong:has-text('No match')")
            page.check("#deep")
            expect(page.locator(".row")).to_have_count(1, timeout=15000)
            check("Quote Q-2291" in page.inner_text(".row .subject"), "ticked, a search finds a word that is only in the text of a mail")
            page.uncheck("#deep")
            page.wait_for_selector("#state strong:has-text('No match')")
            page.click("#searchToggle")
            expect(page.locator(".row").nth(5)).to_be_visible()

            # ---- a new mail starts with Outlook's signature; importance; an answer nobody re-addressed
            page.click("#write")
            check(page.input_value("#bodyInput") == "\n\nRin Aoki\nHardware, Lumenworks\nrin.aoki@lumenworks.example"
                  and page.evaluate("document.getElementById('bodyInput').selectionStart") == 0,
                  "a new mail starts with the signature Outlook has for it, the cursor above it")
            page.locator("#toInput").press_sequentially("pri")
            page.locator("#people .person").first.click()
            page.fill("#subjectInput", "Fixture drawing")
            page.click("#send")
            check(page.inner_text("#composeError") == "Write a message first.", "a signature alone is not a message")
            page.locator("#bodyInput").press_sequentially("Rev C is on the share.")
            page.click("#important")
            check(page.get_attribute("#important", "aria-pressed") == "true", "High importance can be pressed")
            posted.clear()
            page.click("#send")
            expect(page.locator("#toastText")).to_have_text("Sent to Priya Nair", timeout=30000)
            urgent = next(body for name, body in posted if name == "send")
            check(urgent["importance"] == "high" and urgent["body"] == "Rev C is on the share.\n\nRin Aoki\nHardware, Lumenworks\nrin.aoki@lumenworks.example",
                  "the PC is told it is important, and the text goes with the signature under it")
            page.locator(".row").first.click()
            page.wait_for_selector("#actions button:has-text('Reply')")
            page.click("#actions button:has-text('Reply')")
            check(page.input_value("#bodyInput") == "\n\nRin", "an answer starts with the signature for answers")
            page.locator("#bodyInput").press_sequentially("Noted, thanks.")
            posted.clear()
            page.click("#send")
            expect(page.locator("#toastText")).to_have_text("Sent to Mateo Silva", timeout=30000)
            plain = next(body for name, body in posted if name == "send")
            check(plain["recipients"] == "exact" and plain.get("kept") is True and plain["to"] == ["mateo.silva@lumenworks.example"],
                  "an answer nobody re-addressed says so, and the PC leaves it addressed as Outlook addressed it")
            page.click("#back")
            page.click(".tabs a[data-folder=sent]")
            page.wait_for_selector(".row .subject:has-text('Fixture drawing')")
            check(page.locator(".row:has(.subject:has-text('Fixture drawing')) .bang").count() == 1, "the important mail is marked in Sent")

            # ---- a calendar entry made on the phone, changed, and cancelled
            page.click(".tabs a[data-folder=calendar]")
            page.wait_for_selector(".row.event")
            check(page.inner_text("#write").strip() == "New entry", "on the calendar the button makes a new entry")
            page.click("#write")
            expect(page.locator("#planner")).to_be_visible()
            check(page.input_value("#planDay") == time.strftime("%Y-%m-%d", time.localtime(time.time() + 3600))
                  and page.input_value("#planFrom").endswith(":00") and page.locator("#withField").is_visible(),
                  "a new entry starts at the next full hour, half an hour long")
            page.click("#planSave")
            check(page.inner_text("#planError") == "Give it a title first.", "an entry without a title is stopped")
            page.fill("#planSubject", "Gasket fit check")
            page.fill("#planDay", time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400)))
            page.fill("#planFrom", "16:30")
            page.fill("#planTo", "17:00")
            page.fill("#planWhere", "Lab 2")
            page.locator("#withInput").press_sequentially("pri")
            expect(page.locator("#people .personName").first).to_have_text("Priya Nair")
            page.locator("#people .person").first.click()
            page.fill("#planNotes", "Bring both units.")
            check(page.locator("#withField .chipName").all_inner_texts() == ["Priya Nair"] and overflow(page) <= 0,
                  "people are put in the way they are put on a mail")
            page.screenshot(path=SHOTS / "phone-entry.png")
            posted.clear()
            page.click("#planSave")
            expect(page.locator("#toastText")).to_contain_text("Sending the invitation in")
            expect(page.locator("#toastText")).to_have_text("Invitation sent to Priya Nair", timeout=30000)
            made = next(body for name, body in posted if name == "plan")
            check(made["with"] == ["priya.nair@lumenworks.example"] and made["start"].endswith("Z") and made["subject"] == "Gasket fit check",
                  "the PC is handed the people by address and the time as one moment, whatever clock the phone is on")
            entry = page.locator(".row.event:has-text('Gasket fit check')")
            expect(entry).to_have_count(1, timeout=15000)
            check("4:30" in entry.inner_text() and "Lab 2" in entry.inner_text(), "the meeting is in the calendar, under tomorrow")
            entry.click()
            page.wait_for_selector("#actions button:has-text('Change')")
            check(page.locator("#actions button").all_inner_texts() == ["Change", "Cancel the meeting"]
                  and "priya.nair@lumenworks.example" in page.inner_text("#to") and page.locator("#answers").is_hidden(),
                  "your own meeting can be changed or cancelled, not answered")
            page.click("#actions button:has-text('Change')")
            check(page.inner_text("#planTitle") == "Change entry" and page.input_value("#planSubject") == "Gasket fit check"
                  and page.input_value("#planFrom") == "16:30" and page.locator("#withField .chipName").all_inner_texts() == ["Priya Nair"]
                  and "told about the change" in page.inner_text("#planNote"),
                  "Change opens it with what it holds, the people invited among it")
            page.fill("#planSubject", "Gasket fit check, both units")
            page.click("#planSave")
            expect(page.locator("#toastText")).to_have_text("Saved. The people invited are told.", timeout=30000)
            entry = page.locator(".row.event:has-text('both units')")
            expect(entry).to_have_count(1, timeout=15000)
            entry.click()
            page.wait_for_selector("#actions button:has-text('Cancel the meeting')")
            page.click("#actions button:has-text('Cancel the meeting')")
            expect(page.locator("#toastText")).to_contain_text("Cancelling the meeting in")
            expect(page.locator("#toastText")).to_have_text("Cancelled. The people invited are told.", timeout=30000)
            expect(page.locator(".row.event:has-text('both units')")).to_have_count(0, timeout=15000)
            check(page.locator("#letter").is_hidden(), "cancelled behind Undo, the meeting is gone from the calendar")

            # ---- 0.10.0: formatted text and a read receipt
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            page.click("#write")
            page.locator("#toInput").press_sequentially("pri")
            page.locator("#people .person").first.click()
            page.fill("#subjectInput", "Section plan")
            page.fill("#bodyInput", "Stop here and section one.\nunit seven\nunit nine")
            page.evaluate("document.getElementById('bodyInput').setSelectionRange(0, 9)")
            page.click("#formatBar button[data-mark='**']")
            page.evaluate("(() => { const box = document.getElementById('bodyInput'); box.setSelectionRange(box.value.indexOf('unit seven'), box.value.length); })()")
            page.click("#formatBar button[data-mark='- ']")
            check(page.input_value("#bodyInput") == "**Stop here** and section one.\n- unit seven\n- unit nine"
                  and page.locator("#formatNote").is_visible() and overflow(page) <= 0,
                  "the keys over the text put marks around what is selected and in front of the lines of a list")
            page.click("#receipt")
            page.screenshot(path=SHOTS / "phone-format.png")
            posted.clear()
            page.click("#send")
            expect(page.locator("#toastText")).to_have_text("Sent to Priya Nair", timeout=30000)
            marked = next(body for name, body in posted if name == "send")
            check(marked.get("format") == "marks" and marked.get("receipt") is True, "the PC is told to turn the marks into formatting and to ask for a read receipt")
            page.click(".tabs a[data-folder=sent]")
            page.wait_for_selector(".row .subject:has-text('Section plan')")
            page.locator(".row:has(.subject:has-text('Section plan'))").click()
            page.wait_for_selector("#letter:not([hidden]) iframe")
            sent_frame = page.frame_locator("#body iframe")
            check(sent_frame.locator("b").inner_text() == "Stop here" and sent_frame.locator("ul li").all_inner_texts() == ["unit seven", "unit nine"],
                  "the mail that left is bold where it was marked, with a real list")
            page.click("#back")

            # ---- folders made, renamed and taken out; a category made; rules
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            page.click("#folderPick")
            expect(page.locator("#ownFolders a")).to_have_count(3, timeout=10000)
            check(page.locator("#folderDo button:visible").all_inner_texts() == ["New folder", "Rules", "Automatic replies: off"],
                  "under the folders: New folder, Rules and the automatic replies")
            page.click("#folderNew")
            expect(page.locator("#pickerForm")).to_be_visible()
            page.fill("#pickerText", "Tool/ing")
            page.click("#pickerForm button")
            check("without" in page.inner_text("#pickerError"), "a name a folder cannot have is refused before the PC is asked")
            page.fill("#pickerText", "Tooling")
            page.screenshot(path=SHOTS / "phone-new-folder.png")
            page.click("#pickerForm button")
            expect(page.locator("#toastText")).to_have_text("Folder \u201cTooling\u201d made", timeout=15000)
            page.click("#folderPick")
            expect(page.locator("#ownFolders a")).to_have_count(4, timeout=10000)
            made = page.locator("#ownFolders a:has-text('Tooling')")
            check("Inbox" in made.inner_text(), "a folder made while the Inbox is open is made inside it")
            made.click()
            page.wait_for_selector("#state strong:has-text('Nothing here yet')")
            page.click("#folderPick")
            check(page.locator("#folderDo button:visible").all_inner_texts() == ["New folder", "Rename this folder", "Take out this folder", "Rules", "Automatic replies: off"],
                  "a folder of your own can be renamed and taken out")
            page.click("#folderRename")
            check(page.input_value("#pickerText") == "Tooling", "Rename starts from the name it has")
            page.fill("#pickerText", "Tooling 2027")
            page.click("#pickerForm button")
            expect(page.locator("#toastText")).to_have_text("Renamed to \u201cTooling 2027\u201d", timeout=15000)
            expect(page.locator("#folderName")).to_have_text("Tooling 2027", timeout=10000)
            page.click("#folderPick")
            page.click("#folderGone")
            expect(page.locator("#toastText")).to_contain_text("Taking out \u201cTooling 2027\u201d in")
            expect(page.locator("#toastText")).to_have_text("Folder \u201cTooling 2027\u201d taken out", timeout=30000)
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            page.click("#folderPick")
            expect(page.locator("#ownFolders a")).to_have_count(3, timeout=10000)
            check(page.inner_text("#folderName") == "Inbox", "an empty folder is taken out behind Undo, and the Inbox is back")
            page.click("#rulesOpen")
            expect(page.locator("#pickerList .pickOne")).to_have_count(2, timeout=10000)
            check([one.get_attribute("aria-checked") for one in page.locator("#pickerList .pickOne").all()] == ["true", "false"],
                  "Rules lists Outlook's rules, each on or off")
            page.screenshot(path=SHOTS / "phone-rules.png")
            page.locator("#pickerList .pickOne").nth(1).click()
            page.click("#pickerDone")
            expect(page.locator("#toastText")).to_have_text("\u201cNewsletters \u2192 Junk\u201d is on", timeout=15000)
            page.locator(".row").first.click()
            page.wait_for_selector("#actions button:has-text('Reply')")
            page.click("#moreToggle")
            page.click("#doRule")
            expect(page.locator("#pickerList .pickOne")).to_have_count(4, timeout=10000)
            check(page.inner_text("#pickerTitle") == "Mail from Mateo Silva goes to", "a rule can be made for whoever wrote the mail that is open")
            page.locator("#pickerList .pickOne:has-text('Suppliers')").click()
            expect(page.locator("#toastText")).to_contain_text("Making the rule in")
            expect(page.locator("#toastText")).to_have_text("Rule made: Mateo Silva \u2192 Suppliers", timeout=30000)
            page.click("#moreToggle")
            page.click("#doLabel")
            expect(page.locator("#pickerList .pickOne")).to_have_count(4, timeout=10000)
            page.click("#pickerExtra")
            page.fill("#pickerText", "Urgent")
            page.click("#pickerForm button")
            expect(page.locator("#toastText")).to_have_text("Category \u201cUrgent\u201d made", timeout=15000)
            expect(page.locator("#pickerList .pickOne")).to_have_count(5, timeout=10000)
            check("Urgent" in page.locator("#pickerList .pickOne").last.inner_text(), "a category made on the phone is there to tick at once")
            page.click("#pickerCancel")
            page.click("#back")

            # ---- another calendar; an entry that repeats, with a reminder; who is invited
            page.click(".tabs a[data-folder=calendar]")
            page.wait_for_selector(".row.event")
            check(page.locator("#calBar").is_visible() and page.inner_text("#calName") == "Your calendar", "the calendar says whose it is")
            page.click("#calPick")
            expect(page.locator("#pickerList .pickOne")).to_have_count(2, timeout=10000)
            page.locator("#pickerList .pickOne:has-text('Priya Nair')").click()
            page.wait_for_selector(".row.event:has-text('Customer call')")
            check(page.inner_text("#calName") == "Priya Nair" and page.locator(".row.event").count() == 3 and page.locator(".row.event.unread").count() == 0,
                  "a calendar somebody shares can be read")
            page.screenshot(path=SHOTS / "phone-shared-calendar.png")
            page.locator(".row.event:has-text('Customer call')").click()
            page.wait_for_selector("#body pre")
            check(page.locator("#actions").is_hidden() and page.locator("#answers").is_hidden(), "and only read: nothing in it is answered or changed")
            page.click("#back")
            page.click("#calPick")
            page.locator("#pickerList .pickOne").first.click()
            page.wait_for_selector(".row.event:has-text('Stand-up')")
            page.click("#write")
            page.fill("#planSubject", "Weekly sync")
            page.fill("#planDay", time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400)))
            page.fill("#planFrom", "08:00")
            page.fill("#planTo", "08:30")
            page.select_option("#planRemind", "30")
            page.select_option("#planRepeat", "week")
            check(page.locator("#planTimes").is_visible(), "an entry that repeats asks how many times")
            page.fill("#planTimes", "3")
            page.screenshot(path=SHOTS / "phone-repeat.png")
            posted.clear()
            page.click("#planSave")
            expect(page.locator("#toastText")).to_have_text("Saved to the calendar", timeout=30000)
            weekly = next(body for name, body in posted if name == "plan")
            check(weekly["repeat"] == {"every": "week", "times": 3} and weekly["reminder"] == 30, "the PC is told how it repeats and when to remind")
            expect(page.locator(".row.event:has-text('Weekly sync')")).to_have_count(2, timeout=15000)
            check("Repeats" in page.locator(".row.event:has-text('Weekly sync')").first.inner_text(), "each time it takes place in these two weeks is listed")
            page.locator(".row.event:has-text('Weekly sync')").first.click()
            page.wait_for_selector("#actions button:has-text('Change')")
            page.click("#actions button:has-text('Change')")
            expect(page.locator("#pickerList .pickOne")).to_have_count(2)
            check([text.split("\n")[0] for text in page.locator("#pickerList .pickOne").all_inner_texts()] == ["Only this one", "The whole series"],
                  "an entry that repeats asks: this one, or the whole series")
            page.locator("#pickerList .pickOne").first.click()
            check(page.inner_text("#planTitle") == "Change this one" and page.input_value("#planRemind") == "30" and page.locator("#withField").is_hidden(),
                  "this one opens with its own day and the reminder it has")
            page.fill("#planSubject", "Weekly sync, short")
            posted.clear()
            page.click("#planSave")
            expect(page.locator("#toastText")).to_have_text("Saved to the calendar", timeout=30000)
            once = next(body for name, body in posted if name == "plan")
            check(once["scope"] == "one" and once["occurrence"].endswith("Z") and "reminder" not in once and "withExact" not in once,
                  "only that one time is named to the PC, and what was not touched is not sent")
            expect(page.locator(".row.event:has-text('Weekly sync, short')")).to_have_count(1, timeout=15000)
            expect(page.locator(".row.event:has-text('Weekly sync')")).to_have_count(2)
            page.locator(".row.event:has-text('Weekly sync')").last.click()
            page.wait_for_selector("#actions button:has-text('Delete')")
            page.click("#actions button:has-text('Delete')")
            page.locator("#pickerList .pickOne:has-text('The whole series')").click()
            expect(page.locator("#toastText")).to_have_text("Deleted from the calendar", timeout=30000)
            expect(page.locator(".row.event:has-text('Weekly sync')")).to_have_count(0, timeout=15000)
            check(True, "the whole series is taken out at once")
            page.locator(".row.event:has-text('Call with Okafor Supplies')").click()
            page.wait_for_selector("#actions button:has-text('Change')")
            page.click("#actions button:has-text('Change')")
            invited = page.locator("#withField .chip")
            check(invited.locator(".chipName").all_inner_texts() == ["Okafor Supplies", "Marek Novak"], "Change lists who is invited")
            invited.nth(1).click()
            page.locator("#withInput").press_sequentially("pri")
            page.locator("#people .person").first.click()
            posted.clear()
            page.click("#planSave")
            expect(page.locator("#toastText")).to_have_text("Saved. The people invited are told.", timeout=30000)
            guests = next(body for name, body in posted if name == "plan")
            check(guests["withExact"] is True and guests["with"] == ["orders@okafor-supplies.example", "priya.nair@lumenworks.example"],
                  "somebody is taken off the meeting and somebody else invited, and the PC gets the whole list")
            page.locator(".row.event:has-text('Call with Okafor Supplies')").click()
            page.wait_for_selector("#actions button:has-text('Change')")
            check("priya.nair@lumenworks.example" in page.inner_text("#to") and "Marek" not in page.inner_text("#to"), "the meeting holds the people it was changed to")
            page.click("#back")

            # ---- 0.11.0: tasks, notes and contacts; a mail that leaves later; automatic replies; reminders on this phone
            tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            page.click("#folderPick")
            check([name.split()[0] for name in page.locator("#folderMenu a").all_inner_texts()[6:10]] == ["Calendar", "Tasks", "Notes", "Contacts"],
                  "under the mail folders and the calendar: Tasks, Notes and Contacts")
            page.click("#folderMenu a[data-folder=tasks]")
            things = page.locator("#rows .row")
            expect(things).to_have_count(4, timeout=15000)
            check(page.inner_text("#folderName") == "Tasks" and "Due today" in things.first.inner_text() and things.first.locator(".bang").count() == 1
                  and page.inner_text("#thingSay") == "Open tasks" and page.inner_text("#write").strip() == "New task"
                  and page.locator("#remindBar").is_hidden() and overflow(page) <= 0,
                  "the tasks that are open, the one due first on top, and the button makes a new task")
            page.screenshot(path=SHOTS / "phone-tasks.png")
            page.click("#write")
            check(page.inner_text("#thingTitleBar") == "New task" and page.locator("#thingDue").is_visible() and page.locator("#thingCompany").is_hidden(),
                  "a new task asks for what a task has, not for a company")
            page.click("#thingSave")
            check("Say what is to be done first" in page.inner_text("#thingError"), "a task without a name is refused before the PC is asked")
            page.fill("#thingName", "Check the tool quote")
            page.fill("#thingDue", tomorrow)
            page.check("#thingImportant")
            page.screenshot(path=SHOTS / "phone-new-task.png")
            posted.clear()
            page.click("#thingSave")
            expect(page.locator("#toastText")).to_have_text("Saved", timeout=30000)
            kept = next(body for name, body in posted if name == "keep")
            check((kept["kind"], kept["what"], kept["id"], kept["due"], kept["important"]) == ("tasks", "save", "", tomorrow, True),
                  "the PC is handed the task with its day and that it matters")
            quote = page.locator("#rows .row:has-text('Check the tool quote')")
            expect(quote).to_have_count(1, timeout=15000)
            check("Due tomorrow" in quote.inner_text() and things.count() == 5, "the new task is in the list with the day it is due")
            quote.click()
            page.wait_for_selector("#actions button:text-is('Done')")
            check(page.inner_text("#subject") == "Check the tool quote" and page.inner_text("#tags") == "Due tomorrow \u00b7 High importance"
                  and page.locator("#actions button").all_inner_texts() == ["Done", "Change", "Delete"] and overflow(page) <= 0,
                  "a task opens with its day, and can be ticked off, changed or deleted")
            task_id = page.url.split("#/tasks/")[1]
            page.click("#actions button:has-text('Change')")
            check(page.inner_text("#thingTitleBar") == "Change the task" and page.input_value("#thingName") == "Check the tool quote"
                  and page.input_value("#thingDue") == tomorrow and page.is_checked("#thingImportant"), "Change opens the task as it is")
            page.fill("#thingBody", "Ask for 100 pieces first.")
            posted.clear()
            page.click("#thingSave")
            expect(page.locator("#toastText")).to_have_text("Saved", timeout=30000)
            check(next(body for name, body in posted if name == "keep")["id"] == task_id != "", "the change names the task that is there, and no second one is made")
            expect(quote).to_have_count(1, timeout=15000)
            quote.click()
            page.wait_for_selector("#body pre:has-text('Ask for 100 pieces first.')")
            page.click("#actions button:text-is('Done')")
            expect(page.locator("#toastText")).to_have_text("Done", timeout=30000)
            expect(quote).to_have_count(0, timeout=15000)
            page.click("#thingFlip")
            expect(page.locator("#rows .row.off")).to_have_count(2, timeout=15000)
            check(page.inner_text("#thingSay") == "Done tasks" and quote.count() == 1, "ticked off, it leaves the open tasks and is among the done ones")
            quote.click()
            page.wait_for_selector("#actions button:text-is('Not done')")
            page.click("#actions button:text-is('Not done')")
            expect(page.locator("#toastText")).to_have_text("Open again", timeout=30000)
            expect(page.locator("#rows .row.off")).to_have_count(1, timeout=15000)
            page.click("#thingFlip")
            expect(quote).to_have_count(1, timeout=15000)
            quote.click()
            page.wait_for_selector("#actions button:has-text('Delete')")
            page.click("#actions button:has-text('Delete')")
            expect(page.locator("#toastText")).to_contain_text("Deleting in")
            page.click("#toastAction")
            page.wait_for_timeout(3600)
            check(page.inner_text("#subject") == "Check the tool quote" and page.locator("#toast").is_hidden(), "Undo within three seconds and the task stays")
            page.click("#actions button:has-text('Delete')")
            expect(page.locator("#toastText")).to_have_text("Moved to Deleted", timeout=30000)
            expect(things).to_have_count(4, timeout=15000)
            check(quote.count() == 0, "deleted, the task is gone from the list")

            page.click("#folderPick")
            page.click("#folderMenu a[data-folder=notes]")
            expect(things).to_have_count(3, timeout=15000)
            check(page.inner_text("#folderName") == "Notes" and "Hinge torque window" in things.first.inner_text() and "0.40" in things.first.inner_text()
                  and page.inner_text("#write").strip() == "New note" and page.locator("#thingBar").is_hidden(),
                  "the notes, newest first, each with its first line as its name")
            page.click("#write")
            check(page.locator("#thingName").is_hidden() and page.locator("#thingBody").is_visible(), "a note is its text and nothing else")
            page.click("#thingSave")
            check("Write the note first" in page.inner_text("#thingError"), "an empty note is not saved")
            page.fill("#thingBody", "Tool quote\nAsk Okafor for 100 pieces first.")
            page.click("#thingSave")
            expect(page.locator("#toastText")).to_have_text("Saved", timeout=30000)
            expect(things).to_have_count(4, timeout=15000)
            check("Tool quote" in things.first.inner_text(), "the new note is on top")
            page.screenshot(path=SHOTS / "phone-notes.png")
            things.first.click()
            page.wait_for_selector("#body pre")
            check(page.inner_text("#subject") == "Tool quote" and page.inner_text("#body pre") == "Ask Okafor for 100 pieces first."
                  and page.locator("#actions button").all_inner_texts() == ["Change", "Delete"], "a note opens with its first line as the title and the rest under it")
            page.click("#actions button:has-text('Change')")
            check(page.input_value("#thingBody") == "Tool quote\nAsk Okafor for 100 pieces first.", "Change opens the whole text of the note")
            page.fill("#thingBody", "Tool quote\nAsk Okafor for 100 pieces first.\nAnd the lead time.")
            page.click("#thingSave")
            expect(page.locator("#toastText")).to_have_text("Saved", timeout=30000)
            expect(page.locator("#rows .row:has-text('And the lead time.')")).to_have_count(1, timeout=15000)
            things.first.click()
            page.wait_for_selector("#actions button:has-text('Delete')")
            page.click("#actions button:has-text('Delete')")
            expect(page.locator("#toastText")).to_have_text("Moved to Deleted", timeout=30000)
            expect(things).to_have_count(3, timeout=15000)
            check(True, "a note is changed and deleted")

            page.click("#folderPick")
            page.click("#folderMenu a[data-folder=contacts]")
            expect(things).to_have_count(5, timeout=15000)
            names = [text.split("\n")[1] if len(text.split("\n")[0]) <= 2 else text.split("\n")[0] for text in things.all_inner_texts()]
            check(names == sorted(names) and names[0] == "Ada Okafor" and "Okafor Supplies" in things.first.inner_text()
                  and page.inner_text("#write").strip() == "New contact" and overflow(page) <= 0, f"the contacts by name, each with its company ({names})")
            page.screenshot(path=SHOTS / "phone-contacts.png")
            page.click("#searchToggle")
            check(page.locator("#deep").is_hidden(), "a search in the contacts does not offer to look in the text of the mail")
            page.fill("#q", "okafor")
            expect(things).to_have_count(2, timeout=15000)
            check(True, "a search finds contacts by name or company")
            page.click("#searchToggle")
            expect(things).to_have_count(5, timeout=15000)
            things.first.click()
            page.wait_for_selector("#actions button:has-text('Write a mail')")
            ways = page.locator("#files a")
            check(page.inner_text("#subject") == "Ada Okafor" and page.inner_text("#tags") == "Sales \u00b7 Okafor Supplies"
                  and ways.first.inner_text() == "orders@okafor-supplies.example" and ways.nth(1).get_attribute("href") == "tel:+000000142"
                  and "Quotes come back within two days." in page.inner_text("#body") and overflow(page) <= 0,
                  "a contact opens with the address to write to and the numbers to call")
            page.screenshot(path=SHOTS / "phone-contact.png")
            page.click("#actions button:has-text('Write a mail')")
            expect(page.locator("#composer")).to_be_visible()
            check(page.locator("#toField .chipName").all_inner_texts() == ["Ada Okafor"], "Write a mail opens a new mail to that contact")

            # a mail that leaves later
            page.fill("#subjectInput", "Quote for tomorrow")
            page.fill("#bodyInput", "Dear Ada,\nplease send the quote for 100 pieces.")
            check(page.locator("#laterField").is_hidden() and page.inner_text("#send") == "Send", "a mail leaves at once unless you say otherwise")
            page.click("#laterFlip")
            check(page.locator("#laterField").is_visible() and page.input_value("#laterDay") == tomorrow and page.input_value("#laterTime") == "08:00"
                  and page.inner_text("#send") == "Send later" and page.get_attribute("#laterFlip", "aria-pressed") == "true" and overflow(page) <= 0,
                  "Send later asks when, and starts from tomorrow morning")
            page.screenshot(path=SHOTS / "phone-send-later.png")
            page.fill("#laterDay", time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400)))
            page.click("#send")
            check("still to come" in page.inner_text("#composeError") and page.locator("#composer").is_visible(), "a time that has passed is refused, and nothing is sent")
            page.fill("#laterDay", tomorrow)
            posted.clear()
            page.click("#send")
            expect(page.locator("#toastText")).to_contain_text("It waits in the Outbox until then.", timeout=30000)
            leaves = time.mktime(time.strptime(tomorrow + " 08:00", "%Y-%m-%d %H:%M"))
            waiting = next(body for name, body in posted if name == "send")
            check(waiting["later"].startswith(time.strftime("%Y-%m-%dT%H:%M", time.gmtime(leaves))) and page.inner_text("#toastText").startswith("Leaves "),
                  f"the PC is told the moment it is to leave ({waiting['later']}), and the phone says when that is")
            page.click("#back")
            page.click("#folderPick")
            page.click("#folderMenu a[data-folder=outbox]")
            page.wait_for_selector(".row .subject:has-text('Quote for tomorrow')")
            check(page.locator(".row").count() == 1, "until then the mail waits in the Outbox")
            page.locator(".row").first.click()
            page.wait_for_selector("#body pre")
            page.click("#remove")
            expect(page.locator("#toastText")).to_have_text("Deleted. It will not be sent.", timeout=30000)
            check(True, "and there it can still be stopped")

            # a new contact
            page.click("#folderPick")
            page.click("#folderMenu a[data-folder=contacts]")
            expect(things).to_have_count(5, timeout=15000)
            page.click("#write")
            check(page.inner_text("#thingTitleBar") == "New contact" and page.locator("#thingDue").is_hidden() and page.locator("#thingEmail").is_visible(),
                  "a new contact asks for a name, a company, an address and numbers")
            page.fill("#thingName", "Lena Fischer")
            page.fill("#thingCompany", "Fischer Tooling")
            page.fill("#thingEmail", "lena@")
            page.click("#thingSave")
            check("not complete" in page.inner_text("#thingError"), "half an address is refused")
            page.fill("#thingEmail", "lena@fischer-tooling.example")
            page.fill("#thingMobile", "+00 000 0177")
            page.click("#thingSave")
            expect(page.locator("#toastText")).to_have_text("Saved", timeout=30000)
            expect(things).to_have_count(6, timeout=15000)
            lena = page.locator("#rows .row:has-text('Lena Fischer')")
            check("Fischer Tooling" in lena.inner_text(), "the new contact is in the list")
            lena.click()
            page.wait_for_selector("#actions button:has-text('Delete')")
            page.click("#actions button:has-text('Delete')")
            expect(page.locator("#toastText")).to_have_text("Moved to Deleted", timeout=30000)
            expect(things).to_have_count(5, timeout=15000)

            # automatic replies: the switch
            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            check(page.locator("#awayNote").is_hidden(), "nothing is said over the list while automatic replies are off")
            page.click("#folderPick")
            page.click("#awayOpen")
            expect(page.locator("#pickerList .pickOne")).to_have_count(1)
            check("Switch on" in page.inner_text("#pickerList") and "set in Outlook" in page.inner_text("#picker"), "the automatic replies can be switched on; their text is Outlook's")
            page.screenshot(path=SHOTS / "phone-away.png")
            posted.clear()
            page.locator("#pickerList .pickOne").first.click()
            expect(page.locator("#toastText")).to_have_text("Automatic replies are on", timeout=30000)
            check(next(body for name, body in posted if name == "arrange") == {"what": "away", "on": True} and page.locator("#awayNote").is_visible()
                  and overflow(page) <= 0, "switched on, the list says so until they are off again")
            page.click("#folderPick")
            check(page.inner_text("#awayOpen") == "Automatic replies: on", "and so does the list of folders")
            page.keyboard.press("Escape")
            page.click("#awayOff")
            expect(page.locator("#toastText")).to_have_text("Automatic replies are off", timeout=30000)
            check(page.locator("#awayNote").is_hidden(), "Switch off, right there, puts them off")

            # reminders that ring on this phone
            page.click(".tabs a[data-folder=calendar]")
            page.wait_for_selector(".row.event")
            check(page.locator("#remindBar").is_visible() and page.inner_text("#remindSay") == "Reminders do not ring on this phone."
                  and page.inner_text("#remindFlip") == "Switch on", "on the calendar: reminders can ring on this phone, and do not until asked")
            soon = time.time() + 180
            page.click("#write")
            page.fill("#planSubject", "Call the tool shop")
            page.fill("#planDay", time.strftime("%Y-%m-%d", time.localtime(soon)))
            page.fill("#planFrom", time.strftime("%H:%M", time.localtime(soon)))
            page.fill("#planTo", time.strftime("%H:%M", time.localtime(soon + 1800)))
            page.select_option("#planRemind", "5")
            page.click("#planSave")
            expect(page.locator("#toastText")).to_have_text("Saved to the calendar", timeout=30000)
            expect(page.locator(".row.event:has-text('Call the tool shop')")).to_have_count(1, timeout=15000)
            page.click("#remindFlip")
            expect(page.locator("#toastText")).to_contain_text("Call the tool shop, ", timeout=45000)
            check(page.inner_text("#toastText").startswith("Coming up: ") and page.inner_text("#toastAction") == "Show"
                  and page.inner_text("#remindSay") == "Reminders ring on this phone while Titip is open." and overflow(page) <= 0,
                  "switched on, an entry whose reminder is due is said on the phone")
            page.screenshot(path=SHOTS / "phone-reminder.png")
            told = page.evaluate("localStorage.getItem('titip.reminded')")
            page.click("#toastAction")
            if len(json.loads(told)) == 1:   # at some hours an entry of the demo calendar is due too: Show then opens the calendar, not one entry
                page.wait_for_selector("#actions button:has-text('Delete')")
                check(page.inner_text("#subject") == "Call the tool shop" and page.locator("#toast").is_hidden(), "Show opens the entry that is due")
                page.click("#back")
            page.click("#remindFlip")
            check(page.inner_text("#remindSay") == "Reminders do not ring on this phone." and page.evaluate("localStorage.getItem('titip.remind')") == "0",
                  "Switch off stops them")
            page.click("#remindFlip")   # on again: the phone looks once more, and what it said already it does not say again
            page.wait_for_timeout(1500)
            check(len(json.loads(told)) >= 1 and page.evaluate("localStorage.getItem('titip.reminded')") == told and page.locator("#toast").is_hidden(),
                  "an entry is said once, not every time the phone looks")
            page.click("#remindFlip")
            page.locator(".row.event:has-text('Call the tool shop')").click()
            page.wait_for_selector("#actions button:has-text('Delete')")
            page.click("#actions button:has-text('Delete')")
            expect(page.locator("#toastText")).to_have_text("Deleted from the calendar", timeout=30000)
            expect(page.locator(".row.event:has-text('Call the tool shop')")).to_have_count(0, timeout=15000)

            # ---- 0.12.0: only what is unread or flagged; a row pushed aside; the subject of an answer; who somebody is;
            # guests who need not come, an entry over several days, every second week; an answer with a note
            def settled():
                page.wait_for_timeout(200)
                expect(page.locator("#refresh.busy")).to_have_count(0, timeout=30000)

            def push(index, dx):   # a finger pushes the row that far sideways and lets go
                page.evaluate("""([index, dx]) => {
                    const row = document.querySelectorAll('#rows .row')[index];
                    const box = row.getBoundingClientRect();
                    const at = (x) => new Touch({identifier: 1, target: row, clientX: x, clientY: box.top + 20});
                    const fire = (type, x, down) => row.dispatchEvent(new TouchEvent(type,
                        {bubbles: true, cancelable: true, touches: down ? [at(x)] : [], changedTouches: [at(x)]}));
                    const start = box.left + box.width / 2;
                    fire('touchstart', start, true);
                    for (let step = 1; step <= 6; step += 1) fire('touchmove', start + dx * step / 6, true);
                    window.pushed = (row.parentNode.getAttribute('data-swipe') || '') + '|' + row.parentNode.className + '|' + row.style.transform;
                    fire('touchend', start + dx, false);
                }""", [index, dx])
                return page.evaluate("window.pushed")

            page.click(".tabs a[data-folder=inbox]")
            page.wait_for_selector(".row .subject:has-text('Hinge sample B')")
            settled()
            rows = page.locator("#rows .row")
            every = rows.count()
            page.click("#searchToggle")
            check(page.locator("#shows button").all_inner_texts() == ["All", "Unread", "Flagged"]
                  and page.get_attribute("#shows button[data-show='']", "aria-pressed") == "true" and overflow(page) <= 0,
                  "under the search box: All, Unread, Flagged, and All is what is shown")
            page.click("#shows button[data-show=flagged]")
            settled()
            flagged = rows.count()
            check(0 < flagged < every and page.locator("#rows .row:has(.flag)").count() == flagged
                  and page.get_attribute("#shows button[data-show=flagged]", "aria-pressed") == "true", f"Flagged lists only the flagged mail ({flagged} of {every})")
            page.screenshot(path=SHOTS / "phone-flagged.png")
            page.fill("#q", "zzzz")
            page.wait_for_selector("#state strong:has-text('No match')")
            page.fill("#q", "")
            settled()
            expect(rows).to_have_count(flagged, timeout=15000)
            check(True, "a search looks among the flagged ones only, and without it they are all back")
            page.click("#shows button[data-show=unread]")
            settled()
            unread = rows.count()
            check(unread < every and page.locator("#rows .row.unread").count() == unread
                  and (unread > 0 or page.inner_text("#state strong") == "Nothing unread"), f"Unread lists only what is unread ({unread} of {every})")
            page.click("#searchToggle")
            settled()
            expect(rows).to_have_count(every, timeout=15000)
            page.click("#searchToggle")
            check(page.get_attribute("#shows button[data-show='']", "aria-pressed") == "true", "closing the search shows everything again")
            page.click("#searchToggle")

            was_unread = "unread" in rows.first.get_attribute("class")
            posted.clear()
            said = push(0, 150)
            expect(page.locator("#toastText")).to_have_text("Marked as read" if was_unread else "Marked as unread", timeout=15000)
            check(said.startswith(("Mark as read" if was_unread else "Mark as unread") + "|swiping far|translateX(150px)")
                  and [body["what"] for name, body in posted if name == "act"] == ["read" if was_unread else "unread"]
                  and ("unread" in rows.first.get_attribute("class")) != was_unread,
                  f"a row pushed to the right is marked {'read' if was_unread else 'unread'}, and the list shows it ({said})")
            push(0, 150)
            expect(page.locator("#toastText")).to_have_text("Marked as unread" if was_unread else "Marked as read", timeout=15000)
            check(("unread" in rows.first.get_attribute("class")) == was_unread and page.locator("#letter").is_hidden(), "pushed again it is as it was, and no mail was opened by it")
            posted.clear()
            said = push(1, -60)
            page.wait_for_timeout(400)
            check(said.startswith("Delete|swiping|") and not [name for name, body in posted if name == "act"] and rows.count() == every,
                  "a row pushed a little to the left says Delete, and let go it stays")
            said = push(1, -170)
            expect(page.locator("#toastText")).to_contain_text("Deleting in")
            check(said.startswith("Delete|swiping far|"), "pushed far to the left it is deleted, behind Undo")
            page.click("#toastAction")
            page.wait_for_timeout(3600)
            check(rows.count() == every and not [name for name, body in posted if name == "act"], "Undo, and the mail is still there")

            rows.first.click()
            page.wait_for_selector("#actions button:has-text('Reply')")
            check("tap" in (page.get_attribute("#from", "class") or ""), "the sender's name can be tapped")
            page.click("#from")
            expect(page.locator("#pickerList .pickOne")).to_have_count(2, timeout=15000)
            check(page.inner_text("#pickerTitle") == "Mateo Silva" and "Mechanical engineer" in page.inner_text("#pickerNote")
                  and page.locator("#pickerList .pickOne").first.inner_text().split("\n")[0] == "Write a mail" and overflow(page) <= 0,
                  "a tap on the sender shows what Outlook knows of them: job, company, address and number")
            page.screenshot(path=SHOTS / "phone-person.png")
            page.locator("#pickerList .pickOne").first.click()
            expect(page.locator("#composer")).to_be_visible()
            check(page.locator("#toField .chipName").all_inner_texts() == ["Mateo Silva"], "and Write a mail starts one to them")
            page.click("#discard")
            page.click("#actions button:has-text('Reply')")
            check(page.locator("#subjectField").is_visible() and page.input_value("#subjectInput") == "RE: Hinge sample B: torque results",
                  "an answer shows its subject, as Outlook will write it")
            page.fill("#bodyInput", "Thanks, Mateo.")
            posted.clear()
            page.click("#send")
            expect(page.locator("#toastText")).to_have_text("Sent to Mateo Silva", timeout=30000)
            check(next(body for name, body in posted if name == "send")["subject"] == "", "left as it is, the subject is Outlook's to write")
            page.click("#actions button:has-text('Forward')")
            check(page.input_value("#subjectInput") == "FW: Hinge sample B: torque results", "a forward shows its subject too")
            page.fill("#subjectInput", "Torque results, for the review")
            page.locator("#toInput").press_sequentially("pri")
            page.locator("#people .person").first.click()
            posted.clear()
            page.click("#send")
            expect(page.locator("#toastText")).to_have_text("Sent to Priya Nair", timeout=30000)
            check(next(body for name, body in posted if name == "send")["subject"] == "Torque results, for the review", "changed, the PC is handed the new subject")
            page.click("#back")
            page.click(".tabs a[data-folder=sent]")
            page.wait_for_selector(".row .subject:has-text('Torque results, for the review')")
            settled()
            sent = rows.count()
            check("Torque results, for the review" in rows.first.inner_text(), "and the mail left under it")
            said = push(0, 150)
            check(said == "||", f"in Sent nothing is unread, so a push to the right does nothing ({said})")
            push(0, -170)
            expect(page.locator("#toastText")).to_have_text("Moved to Deleted", timeout=30000)
            expect(rows).to_have_count(sent - 1, timeout=15000)
            check(page.locator(".row .subject:has-text('Torque results, for the review')").count() == 0, "a mail pushed away in Sent is moved to Deleted")
            page.click("#folderPick")
            page.click("#folderMenu a[data-folder=deleted]")
            page.wait_for_selector(".row .subject:has-text('Torque results, for the review')")
            said = push(0, -170)
            page.wait_for_timeout(400)
            check(said == "||" and (page.locator("#toast").is_hidden() or "Deleting" not in page.inner_text("#toastText")),
                  "in Deleted a push does nothing: nothing is removed for good from the phone")

            page.click(".tabs a[data-folder=calendar]")
            page.wait_for_selector(".row.event")
            page.click("#write")
            day_one = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
            day_three = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 86400))
            page.fill("#planDay", day_one)
            check(page.input_value("#planEndDay") == day_one and page.locator("#optField").is_visible() and page.input_value("#planBusy") == ""
                  and not page.is_checked("#planPrivate") and overflow(page) <= 0,
                  "a new entry ends on the day it starts unless you say otherwise, and shows the way Outlook sets it")
            page.fill("#planEndDay", day_three)
            page.fill("#planDay", time.strftime("%Y-%m-%d", time.localtime(time.time() + 2 * 86400)))
            check(page.input_value("#planEndDay") == time.strftime("%Y-%m-%d", time.localtime(time.time() + 4 * 86400)), "moved a day later, its last day moves with it")
            page.fill("#planDay", day_one)
            check(page.input_value("#planEndDay") == day_three, "and moved back, it is as long as it was, not a day longer")
            page.fill("#planEndDay", day_one)
            page.fill("#planSubject", "Climate chamber run")
            page.fill("#planFrom", "09:00")
            page.fill("#planTo", "17:00")
            page.fill("#planEndDay", day_three)
            page.select_option("#planBusy", "away")
            page.check("#planPrivate")
            page.locator("#optInput").press_sequentially("jon")
            page.locator("#people .person").first.click()
            page.screenshot(path=SHOTS / "phone-entry-days.png", full_page=True)
            posted.clear()
            page.click("#planSave")
            expect(page.locator("#toastText")).to_have_text("Invitation sent to Jonas Weber", timeout=30000)
            run = next(body for name, body in posted if name == "plan")
            begins, ends = (time.mktime(time.strptime(run[name][:19], "%Y-%m-%dT%H:%M:%S")) for name in ("start", "end"))
            check(run["optional"] == ["jonas.weber@lumenworks.example"] and run["with"] == [] and run["busy"] == "away" and run["private"] is True
                  and ends - begins == 2 * 86400 + 8 * 3600 and "optionalExact" not in run,
                  "the PC is told who need not come, the two more days with their hours, how it shows and that it is private")
            chamber = page.locator(".row.event:has-text('Climate chamber run')")
            expect(chamber.first).to_be_visible(timeout=15000)
            chamber.first.click()
            page.wait_for_selector("#actions button:has-text('Change')")
            check("jonas.weber@lumenworks.example" in page.inner_text("#cc") and page.inner_text("#ccRowLabel") == "Optional:"
                  and page.locator("#actions button").all_inner_texts() == ["Change", "Cancel the meeting"],
                  "the entry lists the guest as optional, and one over several days can be changed like any other")
            page.click("#actions button:has-text('Change')")
            check(page.locator("#optField .chipName").all_inner_texts() == ["Jonas Weber"] and page.locator("#withField .chip").count() == 0
                  and page.input_value("#planEndDay") == day_three and page.input_value("#planBusy") == "away" and page.is_checked("#planPrivate"),
                  "Change opens it as it is: its last day, who is optional, out of office, private")
            page.locator("#optField .chip").first.click()
            page.uncheck("#planPrivate")
            posted.clear()
            page.click("#planSave")
            expect(page.locator("#toastText")).to_have_text("Saved to the calendar", timeout=30000)
            changed = next(body for name, body in posted if name == "plan")
            check(changed["optionalExact"] is True and changed["optional"] == [] and changed["private"] is False and "busy" not in changed,
                  "the guest taken off is handed over as the whole list, and what was not touched is not sent")
            chamber.first.click()
            page.wait_for_selector("#actions button:has-text('Delete')")
            page.click("#actions button:has-text('Delete')")
            expect(page.locator("#toastText")).to_have_text("Deleted from the calendar", timeout=30000)
            expect(chamber).to_have_count(0, timeout=15000)

            page.click("#write")
            check(page.locator("#planRepeat option").all_inner_texts()[:5] == ["Never", "Every day", "Every working day", "Every week", "Every 2 weeks"],
                  "an entry can repeat every working day and every second week")
            page.fill("#planSubject", "Supplier call")
            page.select_option("#planRepeat", "week:2")
            page.fill("#planTimes", "3")
            posted.clear()
            page.click("#planSave")
            expect(page.locator("#toastText")).to_have_text("Saved to the calendar", timeout=30000)
            fortnightly = next(body for name, body in posted if name == "plan")
            check(fortnightly["repeat"] == {"every": "week", "times": 3, "interval": 2} and "busy" not in fortnightly and "private" in fortnightly,
                  "the PC is told: every week, every second one, three times; and how it shows is left to Outlook")
            calls = page.locator(".row.event:has-text('Supplier call')")
            expect(calls.first).to_be_visible(timeout=15000)
            calls.first.click()
            page.wait_for_selector("#actions button:has-text('Delete')")
            page.click("#actions button:has-text('Delete')")
            page.locator("#pickerList .pickOne:has-text('The whole series')").click()
            expect(page.locator("#toastText")).to_have_text("Deleted from the calendar", timeout=30000)
            expect(calls).to_have_count(0, timeout=15000)

            page.locator(".row.event:has-text('Drop test, corner drops')").click()
            page.wait_for_selector("#answers button[data-answer=tentative]")
            check(page.locator("#answerNote").is_visible() and overflow(page) <= 0, "an invitation can be answered with a note for the organizer")
            page.fill("#answerNote", "I can join after 15:00.")
            page.screenshot(path=SHOTS / "phone-answer-note.png")
            posted.clear()
            page.click("#answers button[data-answer=tentative]")
            expect(page.locator("#toastText")).to_have_text("Answered tentative. Jonas Weber is told.", timeout=30000)
            answer = next(body for name, body in posted if name == "act")
            check(answer["note"] == "I can join after 15:00." and answer["answer"] == "tentative" and page.input_value("#answerNote") == "",
                  "the note goes to the PC with the answer, and the box is empty again")
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

            # ---- the PC out of reach: the list stays on screen with a note, and comes back by itself
            expect(page.locator("#toast")).to_be_hidden(timeout=8000)
            page.route("**/api/mail*", lambda route: route.abort())
            page.click("#refresh")
            expect(page.locator("#savedNote")).to_contain_text("Can't reach your PC", timeout=5000)
            check(page.locator(".row").count() >= 6 and "This is the list from" in page.inner_text("#savedNote")
                  and page.locator("#state strong").count() == 0,
                  "an unreachable PC leaves the list on screen, with a note that says from when")
            page.screenshot(path=SHOTS / "phone-kept.png")
            page.unroute("**/api/mail*")
            page.evaluate("window.dispatchEvent(new Event('online'))")  # the phone found its network again
            expect(page.locator("#savedNote")).to_be_hidden(timeout=10000)
            check(page.locator(".row").count() >= 6, "the fresh list replaces it without a tap once the PC is back")

            # ---- opened while the PC is out of reach: the phone shows the list it kept
            page.route("**/api/mail*", lambda route: route.abort())
            page.reload()
            page.wait_for_selector(".row")
            expect(page.locator("#savedNote")).to_contain_text("This is the list from", timeout=5000)
            check(page.locator(".row").count() >= 6 and page.locator("#state strong").count() == 0,
                  "opened with the PC out of reach, the app shows the list this phone kept, not an error")

            # ---- nothing kept and the PC out of reach: plain words and a way back
            page.evaluate("localStorage.removeItem('titip.list.inbox')")
            page.reload()
            page.wait_for_selector("#state strong:has-text(\"Can't show your mail\")")
            page.screenshot(path=SHOTS / "phone-offline.png")
            page.unroute("**/api/mail*")
            errors[:] = [e for e in errors if "ERR_FAILED" not in e]  # the blocked requests above, on purpose
            page.click("#state button")
            expect(page.locator(".row").nth(5)).to_be_visible()
            check(True, "with no list kept, an unreachable PC is explained and Try again recovers")

            # ---- every width, list and reader, no sideways scroll, tap targets
            for width in WIDTHS:
                page.set_viewport_size({"width": width, "height": 844 if width < 860 else 900})
                page.goto(page.url.split("#")[0] + "#/inbox")
                page.wait_for_selector(".row")
                over_list = overflow(page)
                if width < 860:  # the folder names sit at the thumb: each on one line, none cut off
                    cramped = page.evaluate(
                        """() => [...document.querySelectorAll('.tabs a')].filter(a => {
                                const name = document.createRange();
                                name.selectNodeContents(a.firstChild);
                                return name.getClientRects().length > 1 || a.scrollWidth > a.clientWidth;
                            }).map(a => a.textContent.trim())"""
                    )
                    check(not cramped, f"every tab name fits on one line at {width}px {cramped if cramped else ''}")
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
            for name in ("tasks", "notes", "contacts", "calendar"):   # the pages of 0.11.0, and the calendar with its line about reminders
                page.set_viewport_size({"width": 320, "height": 700})
                page.goto(page.url.split("#")[0] + "#/" + name)
                page.wait_for_selector("#rows .row")
                low = page.evaluate(
                    """() => [...document.querySelectorAll('button, a, input, textarea')]
                        .filter(e => e.offsetParent !== null)
                        .map(e => [e.id || e.className || e.textContent.trim().slice(0, 20), Math.round(e.getBoundingClientRect().height)])
                        .filter(pair => pair[1] < 44)"""
                )
                check(overflow(page) <= 0 and not low, f"the {name} page fits a 320 px phone, and what is tapped there is 44 px high {low if low else ''}")
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(page.url.split("#")[0] + "#/inbox")
            page.wait_for_selector(".row")

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
            from titip import __version__ as titip_version
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
            try:  # the relay has the order a moment before the page has its "ok": wait for the page, not for the relay
                expect(far.locator("#headline")).to_have_text("On", timeout=3000)
            except AssertionError:
                pass
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
            code = desk("/desk/far", post=True)["url"]
            mail_page, _, told = code.partition("#r=")
            check(mail_page == remote.PAGE.rsplit("/", 1)[0] + "/"
                  and json.loads(remote.unb64(told)) == {"u": stand_in.url, "c": settings["channel"], "k": settings["key"]},
                  "with Mail from anywhere on, the code for any network leads to the mail app and carries the key after #")
            away.goto(f"{public}/#r={real_ticket}")  # what the phone's camera opens, on a network that is not this PC's
            away.wait_for_selector(".row", timeout=40000)
            check(away.locator(".row").count() >= 6 and len(stand_in.asked) >= 2,
                  f"scanning it shows the inbox through the relay ({away.locator('.row').count()} rows, {len(stand_in.asked)} asks)")
            check("r=" not in away.url, "the key leaves the address bar of the mail app once the phone has kept it")
            # a page that leaves in the middle of a question never collects its answer (the relay drops it after
            # a while); let this one finish, so the count at the end is about answers that were collected
            for _ in range(200):
                if len(stand_in.asked) >= 3 and len(stand_in.given) == len(stand_in.asked):
                    break
                away.wait_for_timeout(100)
            away.click(".tabs a[href='auto.html']")
            away.wait_for_selector("#feed .entry")
            expect(away.locator("#mailTab")).to_be_visible(timeout=20000)
            check(True, "the Autopilot page next to it uses the same code and shows the Mail tab")
            away.click("#mailTab")
            away.wait_for_selector(".row", timeout=40000)
            away.click(".tabs a[data-folder=sent]")
            away.wait_for_selector(".row .subject:has-text('RE: Hinge sample B')", timeout=40000)
            check(True, "Sent through the relay shows what this PC sent earlier in the run")
            away.wait_for_function("JSON.parse(localStorage.getItem('titip.people') || '[]').length >= 8", timeout=40000)
            check(True, "the names and addresses for To and Cc come through the relay too")
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

            # ---- a file attached away from home reaches the PC in sealed pieces the relay accepts
            away.click("#write")
            away.locator("#toInput").press_sequentially("priya.nair@lumenworks.example;")
            away.fill("#subjectInput", "Photo from the road")
            away.fill("#bodyInput", "The scuffed cam, close up.")
            away.set_input_files("#fileInput", [{"name": "cam-on-site.jpg", "mimeType": "image/jpeg", "buffer": os.urandom(250_000)}])
            expect(away.locator("#attached .fileName")).to_have_text(["cam-on-site.jpg"])
            asked_before = len(stand_in.asked)
            away.click("#send")
            expect(away.locator("#toastText")).to_have_text("Sent to priya.nair@lumenworks.example", timeout=90000)
            away.wait_for_selector(".row .subject:has-text('Photo from the road')", timeout=40000)
            away.locator(".row:has(.subject:has-text('Photo from the road'))").click()
            away.wait_for_selector("#files a", timeout=40000)
            check(["".join(text.split()) for text in away.locator("#files a").all_inner_texts()] == ["cam-on-site.jpg244KB"]
                  and 100_000 < stand_in.largest_ask <= 200_000
                  and len(stand_in.asked) - asked_before >= 4,
                  f"a file attached away from home arrives whole, in pieces the relay accepts (largest {stand_in.largest_ask} characters)")
            away.click("#back")
            sends = [text for text in stand_in.wire if "Understood" in str(remote.unseal(remote.unb64(settings["key"]), text))]
            check(len(sends) >= 1 and len(stand_in.wire) > 10, f"the PC could open what the phone sealed ({len(stand_in.wire)} bodies passed)")
            sealed_only = all(re.fullmatch(r"[A-Za-z0-9_-]+", text) for text in stand_in.wire)
            readable = [word for text in stand_in.wire if re.fullmatch(r"[A-Za-z0-9_-]+", text)
                        for word in (b"Hinge", b"Mateo", b"Understood", b"torque", b"Demo attachment", b"inbox", b"route")
                        if word in remote.unb64(text)]
            check(sealed_only and not readable,
                  "the relay only ever held sealed text: no subject, name, body, file or folder")
            for _ in range(100):  # the phone may still be on its way to pick up the last one
                if not stand_in.answers:
                    break
                time.sleep(0.1)
            check(not stand_in.answers and stand_in.handle("GET", f"/c/{settings['channel']}/answer/{stand_in.asked[0]}", "", "")[0] == 204,
                  "every answer was handed out once and is gone from the relay")
            # ---- the relay out of reach from the phone: the app finds out why and says what to do
            trouble = {"mode": "", "dropped": 0}
            other_key = os.urandom(32)

            def troubled(route) -> None:
                request, mode = route.request, trouble["mode"]
                cors = {"Access-Control-Allow-Origin": "*"}
                about_state = request.url.endswith("/state")
                if mode == "blocked" or (mode in ("unknown", "old key") and not about_state):
                    return route.abort()
                if mode == "hiccup" and not trouble["dropped"]:
                    trouble["dropped"] += 1
                    return route.abort()
                if mode == "error" and not about_state:
                    return route.fulfill(status=500, headers=cors, content_type="text/plain", body="")
                if mode == "unknown":
                    return route.fulfill(status=404, headers=cors, content_type="text/plain", body="")
                if mode == "old key":
                    return route.fulfill(status=200, headers=cors, content_type="text/plain",
                                         body=remote.seal(other_key, {"published": int(time.time()), "mail": True}))
                through(route)

            def refresh_in(mode: str) -> None:
                away.wait_for_timeout(3200)  # one look at what is wrong serves the requests of the same moment
                trouble["mode"] = mode
                away.click("#refresh")

            away.click(".tabs a[data-folder=inbox]")
            away.wait_for_selector(".row .subject:has-text('Hinge sample B')", timeout=40000)
            away.route("https://relay.invalid/**", troubled)  # on top of the plain one: with no trouble set it passes everything on
            refresh_in("hiccup")
            away.wait_for_function("!document.getElementById('refresh').classList.contains('busy')", timeout=40000)
            check(trouble["dropped"] == 1 and away.locator("#savedNote").is_hidden() and away.locator(".row").count() >= 6,
                  "one request that does not get through is made again: the list refreshes and nothing is said")
            refresh_in("blocked")
            expect(away.locator("#savedNote")).to_contain_text("does not let the phone reach the relay (relay.invalid)", timeout=30000)
            check("Switch between Wi-Fi and mobile data" in away.inner_text("#savedNote") and away.locator(".row").count() >= 6,
                  "a network that blocks the relay while the page's own site answers is named as that, over the kept list")
            away.screenshot(path=SHOTS / "phone-relay-blocked.png")
            refresh_in("error")
            expect(away.locator("#savedNote")).to_contain_text("The relay answered with an error (500)", timeout=30000)
            check(True, "a relay that answers with an error is reported with its status")
            refresh_in("unknown")
            expect(away.locator("#savedNote")).to_contain_text("The relay does not know the code this phone has", timeout=30000)
            check("show the code again and scan it" in away.inner_text("#savedNote"), "a code the relay does not know leads to: scan again")
            refresh_in("old key")
            expect(away.locator("#savedNote")).to_contain_text("This phone has an older code than your PC", timeout=30000)
            check(True, "a key that does not open what the PC wrote is told apart from a network problem")
            away.wait_for_timeout(3200)
            trouble["mode"] = ""
            away.evaluate("window.dispatchEvent(new Event('online'))")
            expect(away.locator("#savedNote")).to_be_hidden(timeout=40000)
            check(away.locator(".row").count() >= 6, "once the relay is back the list refreshes without a tap")
            check(re.fullmatch(r"Titip \d+\.\d+\.\d+", away.inner_text("#build")) is not None
                  and away.inner_text("#build") == "Titip " + titip_version,
                  f"the page says which version it is ({away.inner_text('#build')})")
            errors[:] = [e for e in errors if "ERR_FAILED" not in e and "status of 500" not in e and "status of 404" not in e]
            away.goto(f"{public}/home.html")  # not an iPhone: nothing to carry, on to the mail
            away.wait_for_selector(".row", timeout=40000)
            check(away.url == public + "/", "on a phone that is not an iPhone the Home Screen page goes straight on to the mail app")
            away.close()

            # ---- the iPhone Home Screen: an icon there has its own storage, so the key rides in the address of home.html.
            # Each browser context below has its own storage, the way Safari and an icon have on a real iPhone.
            site = remote.PAGE.rsplit("/", 1)[0] + "/"
            kinds = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8",
                     "json": "application/json", "svg": "image/svg+xml", "png": "image/png", "webmanifest": "application/manifest+json"}

            def hosted_pages(route) -> None:
                name = route.request.url.split("#")[0].split("?")[0][len(site):] or "index.html"
                file = ROOT / "docs" / name
                if not file.is_file():
                    return route.fulfill(status=404, body="")
                route.fulfill(status=200, content_type=kinds.get(name.rsplit(".", 1)[-1], "application/octet-stream"), body=file.read_bytes())

            def iphone(icon: bool, name: str):
                """An iPhone browser (icon=False) or a Home Screen icon (icon=True) looking at the public pages."""
                phone = browser.new_context(
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                               "Version/17.5 Mobile/15E148 Safari/604.1",
                    viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="light",
                )
                if icon:
                    phone.add_init_script("Object.defineProperty(Navigator.prototype, 'standalone', { get: function () { return true; } });")
                phone.route(re.compile("^" + re.escape(site)), hosted_pages)
                phone.route("https://relay.invalid/**", through)
                tab = phone.new_page()
                tab.on("console", lambda message: errors.append(f"{name}: " + message.text) if message.type == "error" else None)
                tab.on("pageerror", lambda error: errors.append(f"{name}: " + str(error)))
                return phone, tab

            def carried(address: str) -> dict | None:
                told = address.partition("#r=")[2]
                return json.loads(remote.unb64(told)) if told else None

            the_key = {"u": "https://relay.invalid", "c": settings["channel"], "k": settings["key"]}
            safari_context, safari = iphone(False, "iphone browser")
            safari.goto(site + "#r=" + real_ticket)  # the iPhone camera opens the code in the browser
            safari.wait_for_selector("#ready:not([hidden])")
            check(safari.url == site + "home.html#r=" + real_ticket and safari.locator(".steps li").count() == 3,
                  "an iPhone that scans the code lands on the Home Screen page: three steps, the key in the address")
            check(safari.locator("link[rel=manifest]").count() == 0 and safari.locator("link[rel=apple-touch-icon]").count() == 1
                  and safari.title() == "Titip",
                  "that page has no manifest (an iPhone then keeps its whole address for the icon), an icon and the name Titip")
            check(overflow(safari) <= 0, "the Home Screen page does not overflow sideways at 390")
            safari.screenshot(path=SHOTS / "iphone-home-ready.png", full_page=True)
            icon_address = safari.url  # what "Add to Home Screen" keeps

            icon_context, icon = iphone(True, "iphone icon")
            asked_before = len(stand_in.asked)
            icon.goto(icon_address)  # first start from the icon: empty storage, only the address
            icon.wait_for_selector(".row", timeout=40000)
            check(icon.locator(".row").count() >= 6 and len(stand_in.asked) > asked_before,
                  f"opened from the icon, Titip shows the inbox through the relay "
                  f"({icon.locator('.row').count()} rows, {len(stand_in.asked) - asked_before} asks)")
            check(icon.url == site, "and the key has left the icon's address once it is kept")
            icon.locator(".row").first.click()
            icon.wait_for_selector("#letter:not([hidden]) iframe", timeout=40000)
            check(icon.locator("#files a").count() == 2, "a message opens inside the icon with its attachments listed")
            icon.screenshot(path=SHOTS / "iphone-icon-mail.png")
            icon.click("#actions button:has-text('Reply')")
            icon.fill("#bodyInput", "Seen from the Home Screen icon. Thank you.")
            icon.click("#send")
            expect(icon.locator("#toastText")).to_have_text("Sent to Mateo Silva", timeout=60000)
            icon.click("#back")
            icon.click(".tabs a[data-folder=sent]")
            icon.wait_for_selector(".row .preview:has-text('Seen from the Home Screen icon')", timeout=40000)
            from_icon = [text for text in stand_in.wire
                         if "Seen from the Home Screen icon" in str(remote.unseal(remote.unb64(settings["key"]), text))]
            check(len(from_icon) >= 1, "a reply written inside the icon reaches the PC sealed, is sent there and shows in Sent")
            icon.click(".tabs a[href='auto.html']")
            icon.wait_for_selector("#feed .entry")
            expect(icon.locator("#mailTab")).to_be_visible(timeout=20000)
            check(icon.inner_text("#forget") == "Unlink this icon" and icon.locator("#home").is_hidden(),
                  "the Autopilot page works inside the icon and offers to unlink the icon, not to add another one")
            icon.goto("about:blank")
            icon.goto(icon_address)  # every later start opens the same address
            icon.wait_for_selector(".row", timeout=40000)
            check(icon.url == site, "the icon opens the mail again at its next start")

            safari.evaluate("window.dispatchEvent(new Event('pagehide'))")
            check(safari.url == site + "home.html", "leaving the Home Screen page takes the key out of its address")
            safari.goto(site + "home.html")  # back on it: this browser kept the key, so the page is ready again
            safari.wait_for_selector("#ready:not([hidden])")
            check(carried(safari.url) == the_key, "opened again in the same browser, the page puts the key it kept back in its address")
            safari.click("#skip")
            safari.wait_for_selector(".row", timeout=40000)
            check(safari.url == site, "Not now opens the mail app in the browser, with no key in the address")
            safari.click(".tabs a[href='auto.html']")
            safari.wait_for_selector("#feed .entry")
            expect(safari.locator("#home")).to_be_visible()
            safari.click("#home a")
            safari.wait_for_selector("#ready:not([hidden])")
            check(safari.url.startswith(site + "home.html?from=auto#r=") and carried(safari.url) == the_key,
                  "the Autopilot page in the iPhone browser leads back to the Home Screen page")
            safari_context.close()

            late_context, late = iphone(False, "iphone browser, five minutes")
            late.clock.install()
            late.goto(site + "auto.html#r=" + real_ticket)  # the code can also open the Autopilot page
            late.wait_for_selector("#ready:not([hidden])")
            check(late.url == site + "home.html?from=auto#r=" + real_ticket,
                  "a code that opens the Autopilot page takes an iPhone to the Home Screen page too")
            auto_icon_address = late.url
            late.clock.fast_forward(301000)
            late.wait_for_url(site + "auto.html", timeout=20000)
            late.wait_for_selector("#feed .entry", timeout=40000)
            check("#" not in late.url, "after five minutes the page gives the key up and opens Titip in the browser")
            late.goto(site + "home.html?from=auto")
            late.wait_for_selector("#ready:not([hidden])")
            late.goto(site + "?demo")
            late.wait_for_selector(".row")
            late_context.close()

            auto_icon_context, auto_icon = iphone(True, "iphone icon, autopilot")
            auto_icon.goto(auto_icon_address)
            auto_icon.wait_for_selector("#feed .entry", timeout=40000)
            check(auto_icon.url == site + "auto.html" and "relay" in auto_icon.inner_text("#far"),
                  "an icon made from there opens the Autopilot page, through the relay")
            auto_icon_context.close()

            bare_context, bare = iphone(True, "iphone icon without a key")
            bare.goto(site)  # an icon made the old way: the mail app's address, nothing in storage
            bare.wait_for_url(site + "auto.html")
            expect(bare.locator("#headline")).to_have_text("This icon has no key to your PC")
            check("Delete this icon" in bare.inner_text("#line") and bare.locator(".letter, .entry").count() == 0
                  and bare.locator("#demoNote").is_hidden() and bare.locator(".tabs").is_hidden() and bare.locator("#relink").is_hidden(),
                  "an icon that brought no key says so and what to do, instead of showing invented mail")
            check(overflow(bare) <= 0, "that notice does not overflow sideways at 390")
            bare.screenshot(path=SHOTS / "iphone-icon-no-key.png", full_page=True)
            bare.goto(site + "home.html")
            bare.wait_for_url(site + "auto.html")
            expect(bare.locator("#headline")).to_have_text("This icon has no key to your PC")
            bare_context.close()

            unlinked_context, unlinked = iphone(False, "iphone browser, not linked")
            unlinked.goto(site + "home.html")
            unlinked.wait_for_selector("#none:not([hidden])")
            check("not linked yet" in unlinked.inner_text("#none") and unlinked.locator("#ready").is_hidden(),
                  "the Home Screen page in a browser that never scanned the code says to scan it first")
            unlinked_context.close()

            icon.click(".tabs a[href='auto.html']")
            icon.wait_for_selector("#feed .entry")
            said: list[str] = []
            kept_keys = "Object.keys(localStorage).filter(k => /^titip\\.(list\\.|people|me)/.test(k)).length"
            kept_before = icon.evaluate(kept_keys)
            icon.once("dialog", lambda dialog: (said.append(dialog.message), dialog.accept()))
            icon.click("#forget")
            expect(icon.locator("#headline")).to_have_text("You unlinked this icon")
            check(kept_before >= 3 and icon.evaluate(kept_keys) == 0,
                  f"Unlink also clears the lists and names the phone kept ({kept_before} entries before, none after)")
            check(len(said) == 1 and "Delete it from the Home Screen" in said[0], "Unlink inside the icon says the icon has to be deleted too")
            icon.goto("about:blank")
            icon.goto(icon_address)  # the icon's address still carries the key
            icon.wait_for_url(site + "auto.html")
            expect(icon.locator("#headline")).to_have_text("You unlinked this icon")
            check(icon.locator(".letter, .entry").count() == 0 and icon.locator("#relink").is_visible(),
                  "an unlinked icon stays unlinked at its next start, although its address still carries the key")
            icon.screenshot(path=SHOTS / "iphone-icon-unlinked.png", full_page=True)
            icon.click("#relink")
            icon.wait_for_selector("#feed .entry", timeout=40000)
            icon.goto("about:blank")
            icon.goto(icon_address)
            icon.wait_for_selector(".row", timeout=40000)
            check(True, "Link again brings the icon back, for this start and the next")
            icon_context.close()

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
            check(desk_page.locator(".primary:visible").count() == 1 and desk_page.locator("#far.primary").count() == 1,
                  "with a relay, the code for any network is the one main button on the PC page")
            desk_page.click("#far")
            desk_page.wait_for_selector("#farQr svg")
            expect(desk_page.locator("#farOpens")).to_contain_text("only the Autopilot page")
            check(desk("/desk/far", post=True)["url"].startswith(remote.PAGE + "#r="),
                  "with Mail from anywhere off, the code leads to the Autopilot page and the PC page says so")
            first_code = desk_page.inner_html("#farQr")
            desk_page.once("dialog", lambda dialog: dialog.accept())
            desk_page.click("#mailFar")
            expect(desk_page.locator("#farOpens")).to_contain_text("opens your Outlook mail")
            expect(desk_page.locator("#farQr svg")).to_be_visible()
            for _ in range(50):
                if desk_page.inner_html("#farQr") != first_code:
                    break
                desk_page.wait_for_timeout(100)
            check(desk_page.inner_html("#farQr") != first_code and desk("/desk/status")["far"]["mail"] is True,
                  "switching it on redraws the code on screen, which now leads to the mail app")
            expect(desk_page.locator("#updateLine")).to_contain_text("not installed with git")
            check(desk_page.locator("#updateSwitch").is_disabled() and desk_page.locator("#updateNow").is_disabled(),
                  "the demo never updates itself, and the PC page says so")
            desk_page.click("#pair")
            desk_page.wait_for_selector("#qr svg")
            check("Browser" in desk_page.inner_text("#devices"), "desk page lists the paired device")
            desk_page.evaluate("window.scrollTo(0, 0)")  # the masks land where the page is scrolled to, so start at the top
            # the screenshot hides this PC's network address, so it is safe to share
            desk_page.screenshot(
                path=SHOTS / "desk.png", full_page=True, mask_color="#e1dbcf",
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
