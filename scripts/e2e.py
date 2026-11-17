"""End-to-end check in a real browser against the demo mailbox.

    python scripts/e2e.py [folder-for-screenshots]

Starts Titip with invented mail on spare ports, pairs a browser through the real QR link (HTTPS on this
PC's network address), then reads, answers, undoes, sends, saves a draft and searches, at phone and
desktop sizes, light and dark. Fails on any console error, page error or sideways overflow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


def desk(path: str, post: bool = False) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{DESK_PORT}{path}",
        data=b"{}" if post else None,
        headers={"X-Titip": "1", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as reply:
        return json.load(reply)


def overflow(page) -> int:
    return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def main() -> None:
    from playwright.sync_api import expect, sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    home = tempfile.mkdtemp(prefix="titip-e2e-")
    environment = {k: v for k, v in os.environ.items() if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
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

            # ---- the desk page on the PC
            desk_page = context.new_page()
            desk_page.set_viewport_size({"width": 900, "height": 1000})
            desk_page.on("console", lambda message: errors.append("desk: " + message.text) if message.type == "error" else None)
            desk_page.on("pageerror", lambda error: errors.append("desk: " + str(error)))
            desk_page.goto(f"http://127.0.0.1:{DESK_PORT}/")
            desk_page.wait_for_selector("#mail .good")
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
