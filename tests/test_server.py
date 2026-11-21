"""The two listeners over real sockets (HTTPS to the phone), with the demo mailbox behind them."""
import http.client
import json
import re
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

from titip import server
from titip.mailbox import DemoMailbox, OutlookBusy
from titip.pairing import COOKIE
from titip.server import Titip

X = {"X-Titip": "1", "Content-Type": "application/json"}


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        cls.app = Titip(DemoMailbox(), Path(cls.home.name), phone_port=0, desk_port=0, phone_host="127.0.0.1")
        cls.app.start()
        cls.desk_port = cls.app.desk.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.home.cleanup()

    def phone(self, method, path, body=None, headers=None, cookie=None):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", self.app.phone_port, context=context, timeout=10)
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = f"{COOKIE}={cookie}"
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        reply = connection.getresponse()
        data = reply.read()
        connection.close()
        return reply, data

    def desk(self, method, path, headers=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.desk_port, timeout=10)
        headers = dict(headers or {})
        if host:
            headers["Host"] = host
        connection.request(method, path, "{}" if method == "POST" else None, headers)
        reply = connection.getresponse()
        data = reply.read()
        connection.close()
        return reply, data

    def pair(self):
        code = self.app.new_pairing()["url"].split("#")[1]
        reply, _ = self.phone("POST", "/api/pair", {"code": code}, X)
        self.assertEqual(reply.status, 200)
        return reply.getheader("Set-Cookie").split(";")[0].split("=", 1)[1], code, reply.getheader("Set-Cookie")


class Pairing(ServerCase):
    def test_phone_link_is_https_and_code_stays_in_the_fragment(self):
        pairing = self.app.new_pairing()
        self.assertTrue(pairing["url"].startswith("https://"))
        self.assertRegex(pairing["url"], r"/pair#[\w-]{20,}$")
        self.assertTrue(self.app.https)

    def test_unpaired_phone_gets_no_mail_and_no_app(self):
        for path in ("/api/state", "/api/mail?folder=inbox", "/api/item?id=demo.0000000000000000", "/api/attachment?id=x&index=1"):
            reply, data = self.phone("GET", path)
            self.assertEqual(reply.status, 401, path)
            self.assertNotIn(b"Hinge", data)
        for path in ("/", "/app.js", "/demo.json"):
            reply, data = self.phone("GET", path)
            self.assertEqual(reply.status, 401, path)
            self.assertIn(b"not paired", data)
        reply, _ = self.phone("POST", "/api/send", {"mode": "new", "to": "a@example.com", "body": "x"}, X)
        self.assertEqual(reply.status, 401)
        reply, _ = self.phone("GET", "/api/state", cookie="guessed-token")
        self.assertEqual(reply.status, 401)

    def test_pairing_page_and_its_files_are_public(self):
        for path in ("/pair", "/pair.js", "/styles.css", "/icon.svg"):
            reply, _ = self.phone("GET", path)
            self.assertEqual(reply.status, 200, path)

    def test_the_typefaces_the_style_sheet_names_are_served_on_both_doors(self):
        """styles.css asks for three woff2 files; a page that cannot get them falls back to another look."""
        sheet = (server.WEB / "styles.css").read_text(encoding="utf-8")
        asked = re.findall(r'url\("([a-z-]+\.woff2)"\)', sheet)
        self.assertEqual(sorted(asked), sorted(name.lstrip("/") for name in server.FONTS))
        for name in asked:
            for door in (self.phone, self.desk):
                reply, data = door("GET", "/" + name)
                self.assertEqual(reply.status, 200, name)
                self.assertEqual(reply.getheader("Content-Type"), "font/woff2")
                self.assertEqual(data[:4], b"wOF2", name)

    def test_code_works_once_and_cookie_is_locked_down(self):
        token, code, cookie = self.pair()
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Max-Age="):
            self.assertIn(flag, cookie)
        reply, _ = self.phone("POST", "/api/pair", {"code": code}, X)
        self.assertEqual(reply.status, 403)
        reply, _ = self.phone("POST", "/api/pair", {"code": "made-up"}, X)
        self.assertEqual(reply.status, 403)
        stored = (Path(self.home.name) / "devices.json").read_text(encoding="utf-8")
        self.assertNotIn(token, stored)  # only the hash is on disk

    def test_expired_code_is_refused(self):
        self.app.codes.seconds = -1
        try:
            code = self.app.new_pairing()["url"].split("#")[1]
        finally:
            self.app.codes.seconds = 300
        reply, _ = self.phone("POST", "/api/pair", {"code": code}, X)
        self.assertEqual(reply.status, 403)

    def test_posts_without_the_header_are_refused(self):
        token, _, _ = self.pair()
        code = self.app.new_pairing()["url"].split("#")[1]
        reply, _ = self.phone("POST", "/api/pair", {"code": code})
        self.assertEqual(reply.status, 403)
        before = self.app.mailbox.state()["counts"]["sent"]
        reply, _ = self.phone("POST", "/api/send", {"mode": "new", "to": "a@example.com", "body": "x"}, cookie=token)
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.app.mailbox.state()["counts"]["sent"], before)

    def test_unpair_all_cuts_the_phone_off(self):
        token, _, _ = self.pair()
        self.assertEqual(self.phone("GET", "/api/state", cookie=token)[0].status, 200)
        reply, _ = self.desk("POST", "/desk/unpair", {"X-Titip": "1"})
        self.assertEqual(reply.status, 200)
        self.assertEqual(self.phone("GET", "/api/state", cookie=token)[0].status, 401)


class Mail(ServerCase):
    def setUp(self):
        self.token = self.pair()[0]

    def get(self, path):
        reply, data = self.phone("GET", path, cookie=self.token)
        return reply, json.loads(data)

    def test_read_reply_and_find_it_in_sent(self):
        reply, state = self.get("/api/state")
        self.assertTrue(state["ok"])
        self.assertEqual(reply.getheader("Cache-Control"), "no-store")
        _, page = self.get("/api/mail?folder=inbox&limit=3")
        self.assertEqual(len(page["mail"]), 3)
        self.assertTrue(page["more"])
        first = page["mail"][0]
        _, item = self.get("/api/item?id=" + first["id"])
        self.assertEqual(item["subject"], first["subject"])
        sent, data = self.phone("POST", "/api/send", {"mode": "reply", "id": first["id"], "body": "Run the rest."}, X, self.token)
        self.assertEqual((sent.status, json.loads(data)), (200, {"ok": True, "sent": True}))
        _, sent_page = self.get("/api/mail?folder=sent&limit=1")
        self.assertEqual(sent_page["mail"][0]["subject"], "RE: " + first["subject"])

    def test_the_composer_gets_names_and_addresses_to_suggest(self):
        reply, got = self.get("/api/people")
        self.assertEqual((reply.status, reply.getheader("Cache-Control")), (200, "no-store"))
        self.assertGreaterEqual(len(got["people"]), 8)
        self.assertEqual(set(got["people"][0]), {"name", "address"})
        self.assertEqual(self.app.mail_call("people", {}), (200, got))  # what the relay is answered with
        self.assertEqual(self.phone("GET", "/api/people")[0].status, 401, "not to a phone that is not paired")

    def test_a_name_being_typed_is_looked_up_in_the_address_book(self):
        reply, got = self.get("/api/people?q=pri")
        self.assertEqual((reply.status, got["book"]), (200, True))
        self.assertGreaterEqual(len(got["people"]), 2)
        self.assertTrue(all(person["name"].lower().startswith("pri") and "@" in person["address"] for person in got["people"]))
        self.assertEqual(self.app.mail_call("people", {"q": "pri"}), (200, got))  # what the relay is answered with
        self.assertEqual(self.get("/api/people?q=p")[1], {"people": [], "book": True}, "one letter is not looked up")
        self.assertNotIn("book", self.get("/api/people?q=")[1], "nothing typed: the usual list")
        self.assertEqual(self.phone("GET", "/api/people?q=pri")[0].status, 401, "not to a phone that is not paired")

    def test_problems_come_back_as_plain_messages(self):
        reply, data = self.phone("POST", "/api/send", {"mode": "new", "to": "", "body": "x"}, X, self.token)
        self.assertEqual(reply.status, 400)
        self.assertEqual(json.loads(data)["error"], "Add at least one recipient.")
        reply, body = self.get("/api/item?id=demo.FFFF")
        self.assertEqual(reply.status, 502)
        self.assertIn("moved or deleted", body["error"])
        self.assertEqual(self.get("/api/mail?folder=nope")[0].status, 502)
        self.assertEqual(self.get("/api/mail?limit=abc")[0].status, 400)

    def test_a_busy_outlook_is_marked_so_the_phone_asks_again(self):
        real_list = self.app.mailbox.list

        def busy(*args):
            raise OutlookBusy()

        self.app.mailbox.list = busy
        try:
            reply, body = self.get("/api/mail?folder=inbox")
        finally:
            self.app.mailbox.list = real_list
        self.assertEqual((reply.status, body["busy"]), (503, True))
        self.assertIn("busy", body["error"])
        self.assertEqual(self.get("/api/mail?folder=inbox")[0].status, 200)

    def test_the_phones_counts_never_wait_for_a_busy_outlook(self):
        box = self.app.mailbox
        real_state, real_quick = box.state, box.quick_state
        box.state = lambda *args, **kwargs: (_ for _ in ()).throw(OutlookBusy())
        box.quick_state = lambda: {"ok": True, "busy": True, "counts": {"inbox": 3, "sent": 1, "drafts": 2}}
        try:
            reply, body = self.get("/api/state")
        finally:
            box.state, box.quick_state = real_state, real_quick
        self.assertEqual((reply.status, body["counts"]["drafts"], body["busy"]), (200, 2, True))

    def test_attachment_download_and_no_file_access_outside_the_app(self):
        _, page = self.get("/api/mail?folder=inbox&q=hinge")
        reply, data = self.phone("GET", f"/api/attachment?id={page['mail'][0]['id']}&index=1", cookie=self.token)
        self.assertEqual(reply.status, 200)
        self.assertIn("hinge-B-torque.xlsx", reply.getheader("Content-Disposition"))
        for path in ("/../titip/server.py", "/..%2f..%2fREADME.md", "/desk.html", "/desk/status", "/pair.html"):
            self.assertEqual(self.phone("GET", path, cookie=self.token)[0].status, 404, path)

    def test_app_page_forbids_inline_script_and_remote_content(self):
        reply, _ = self.phone("GET", "/", cookie=self.token)
        policy = reply.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", policy)
        self.assertIn("default-src 'self'", policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertEqual(reply.getheader("X-Content-Type-Options"), "nosniff")


class MoreOfOutlook(ServerCase):
    """0.9.0: files that go along, the calendar, invitations, delete. All of it through mail_call, so the relay gets it too."""

    def setUp(self):
        self.token = self.pair()[0]

    def get(self, path):
        reply, data = self.phone("GET", path, cookie=self.token)
        return reply, json.loads(data)

    def post(self, path, body, cookie=True, headers=X):
        reply, data = self.phone("POST", path, body, headers, self.token if cookie else None)
        return reply.status, json.loads(data)

    def piece(self, identity, index, data, last=False, name="notes.txt"):
        import base64

        return {"upload": identity, "index": index, "last": last, "name": name, "data": base64.b64encode(data).decode("ascii")}

    def test_a_file_handed_over_in_pieces_leaves_with_the_mail(self):
        self.assertEqual(self.post("/api/upload", self.piece("file-from-the-phone", 1, b" world", last=True))[0], 200)
        self.assertTrue(self.app.busy(), "no restart for an update while a file is on its way")
        status, got = self.post("/api/upload", self.piece("file-from-the-phone", 0, b"hello"))
        self.assertEqual((status, got["whole"]), (200, True))
        status, sent = self.post("/api/send", {"mode": "new", "to": ["a@mail.example"], "subject": "With a file", "body": "See attached.",
                                               "files": ["file-from-the-phone"]})
        self.assertEqual((status, sent), (200, {"ok": True, "sent": True}))
        _, page = self.get("/api/mail?folder=sent&q=With%20a%20file")
        _, item = self.get("/api/item?id=" + page["mail"][0]["id"])
        self.assertEqual((item["subject"], item["attachments"]), ("With a file", [{"index": 1, "name": "notes.txt", "size": 11}]))
        self.assertFalse(self.app.busy(), "the file is gone from memory once the mail took it")

    def test_a_mail_whose_file_never_arrived_is_not_sent_without_it(self):
        _, before = self.get("/api/state")
        status, answer = self.post("/api/send", {"mode": "new", "to": ["a@mail.example"], "body": "See attached.", "files": ["never-handed-over"]})
        self.assertEqual((status, answer["filesGone"]), (400, True))
        self.assertIn("nothing was sent", answer["error"])
        self.assertEqual(self.get("/api/state")[1]["counts"]["sent"], before["counts"]["sent"])
        status, answer = self.app.mail_call("send", {"mode": "new", "to": ["a@mail.example"], "body": "x", "files": [{"name": "C:/secret", "data": "x"}]})
        self.assertEqual(status, 400, "a phone names uploads; it cannot hand a path or bytes of its own to the mailbox")

    def test_only_a_paired_phone_with_the_header_can_hand_over_a_piece(self):
        piece = self.piece("file-of-a-stranger", 0, b"x", last=True)
        self.assertEqual(self.post("/api/upload", piece, cookie=False)[0], 401)
        self.assertEqual(self.post("/api/upload", piece, headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.post("/api/upload", {**piece, "data": "not base64 !!"})[0], 400)
        self.assertEqual(self.post("/api/upload", {**piece, "upload": "x"})[0], 400)

    def test_a_piece_fits_in_one_request_on_your_own_network(self):
        import os

        from titip import uploads

        self.assertLess(uploads.MAX_PIECE * 4 / 3 + 400, server.MAX_POST)
        script = (Path(server.WEB) / "app.js").read_text(encoding="utf-8")
        own, far = (int(number) for number in re.findall(r"PIECE: (\d+), LANES: \d, FILES_BYTES: \d+   //", script))
        self.assertLessEqual(own, uploads.MAX_PIECE)
        # through the relay a question holds 200,000 characters after it was sealed (worker/titip-relay.js: MAX_ASK)
        from titip.remote import seal

        sealed = seal(os.urandom(32), {"id": "x" * 16, "at": 1_800_000_000, "route": "upload", "params": self.piece("u" * 24, 399, os.urandom(far), True, "n" * 120)})
        self.assertLess(len(sealed), 200_000)
        status, got = self.post("/api/upload", self.piece("a-large-piece-of-file", 0, os.urandom(own), last=True))
        self.assertEqual((status, got["whole"]), (200, True))
        self.assertEqual(len(self.app.uploads.take(["a-large-piece-of-file"])[0]["data"]), own)

    def test_the_calendar_and_one_entry(self):
        reply, page = self.get("/api/calendar?offset=0&days=14")
        self.assertEqual((reply.status, reply.getheader("Cache-Control")), (200, "no-store"))
        self.assertGreaterEqual(len(page["events"]), 6)
        reply, event = self.get("/api/event?id=" + page["events"][0]["id"])
        self.assertEqual((reply.status, event["subject"]), (200, page["events"][0]["subject"]))
        self.assertEqual(self.app.mail_call("calendar", {"offset": 0, "days": 14}), (200, page))  # what the relay is answered with
        self.assertEqual(self.get("/api/calendar?offset=abc")[0].status, 400)
        self.assertEqual(self.get("/api/event?id=demo.FFFF")[0].status, 502)
        self.assertEqual(self.phone("GET", "/api/calendar")[0].status, 401, "not to a phone that is not paired")

    def test_an_invitation_is_answered_and_a_message_deleted(self):
        _, page = self.get("/api/mail?folder=inbox&limit=100")
        invitation = next(mail for mail in page["mail"] if mail["kind"] == "meeting")
        self.assertEqual(self.post("/api/act", {"what": "respond", "id": invitation["id"], "answer": "accept"}),
                         (200, {"ok": True, "responded": "accept", "told": True}))
        self.assertEqual(self.get("/api/item?id=" + invitation["id"])[1]["invite"]["response"], "accepted")
        _, outbox = self.get("/api/mail?folder=outbox")
        self.assertEqual(self.post("/api/act", {"what": "delete", "id": outbox["mail"][0]["id"]}), (200, {"ok": True, "deleted": True}))
        self.assertEqual(self.get("/api/mail?folder=outbox")[1]["mail"], [])
        self.assertEqual(self.get("/api/mail?folder=deleted")[1]["mail"][0]["id"], outbox["mail"][0]["id"])
        status, answer = self.post("/api/act", {"what": "delete", "id": outbox["mail"][0]["id"]})
        self.assertEqual(status, 400)
        self.assertIn("for good", answer["error"])
        self.assertEqual(self.post("/api/act", {"what": "empty-the-mailbox", "id": "x"})[0], 400)
        self.assertEqual(self.post("/api/act", {"what": "delete", "id": invitation["id"]}, cookie=False)[0], 401)

    def test_your_own_folders_the_signature_and_a_search_in_the_text(self):
        reply, own = self.get("/api/folders")
        self.assertEqual((reply.status, own["folders"][0]["name"], own["categories"][0]), (200, "Hinge project", "Customer"))
        self.assertEqual(len(self.get("/api/mail?folder=" + own["folders"][0]["id"])[1]["mail"]), 2)
        self.assertEqual(self.get("/api/mail?folder=f.nothing.here")[0].status, 502)
        self.assertTrue(self.get("/api/signature")[1]["new"].startswith("Rin Aoki"))
        self.assertEqual(self.get("/api/mail?folder=inbox&q=shore")[1]["mail"], [])
        self.assertEqual(len(self.get("/api/mail?folder=inbox&q=shore&deep=1")[1]["mail"]), 1)
        self.assertEqual(self.app.mail_call("mail", {"folder": "inbox", "q": "shore", "deep": "1"})[1]["mail"][0]["subject"][:5], "Quote")
        for path in ("/api/folders", "/api/signature"):
            self.assertEqual(self.phone("GET", path)[0].status, 401, path)

    def test_a_message_is_flagged_labelled_marked_and_moved(self):
        _, page = self.get("/api/mail?folder=inbox&q=laptop")
        mail_id = page["mail"][0]["id"]
        own = self.get("/api/folders")[1]["folders"]
        for order, answer in (
            ({"what": "flag"}, {"ok": True, "flagged": True}),
            ({"what": "categorize", "categories": ["To read"]}, {"ok": True, "categories": ["To read"]}),
            ({"what": "unread"}, {"ok": True, "unread": True}),
            ({"what": "read"}, {"ok": True, "unread": False}),
            ({"what": "move", "to": own[2]["id"]}, {"ok": True, "moved": True}),
        ):
            self.assertEqual(self.post("/api/act", {"id": mail_id, **order}), (200, answer), order)
        moved = self.get("/api/mail?folder=" + own[2]["id"])[1]["mail"][0]
        self.assertEqual((moved["id"], moved["flagged"], self.get("/api/item?id=" + mail_id)[1]["categories"]), (mail_id, True, ["To read"]))
        self.assertEqual(self.post("/api/act", {"what": "move", "id": mail_id, "to": "outbox"})[0], 400)

    def test_a_busy_outlook_does_not_keep_a_tap_waiting_for_the_mark_as_read(self):
        real = self.app.mailbox.act
        self.app.mailbox.act = lambda payload: (_ for _ in ()).throw(OutlookBusy())
        try:
            status, answer = self.post("/api/act", {"what": "read", "id": "x"})
        finally:
            self.app.mailbox.act = real
        self.assertEqual((status, answer["busy"]), (503, True))

    def test_a_calendar_entry_is_made_changed_and_taken_out(self):
        import datetime as dt

        start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        order = {"what": "save", "subject": "Gasket fit check", "location": "Lab 2", "with": ["priya.nair@lumenworks.example"],
                 "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": (start + dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.assertEqual(self.post("/api/plan", order), (200, {"ok": True, "saved": True, "told": True}))
        entry = next(event for event in self.get("/api/calendar")[1]["events"] if event["subject"] == "Gasket fit check")
        self.assertEqual((entry["start"], entry["mine"], entry["meeting"]), (order["start"], True, True))
        self.assertEqual(self.post("/api/plan", {**order, "id": entry["id"], "subject": "Gasket fit check, Lab 3"})[0], 200)
        self.assertEqual(self.get("/api/event?id=" + entry["id"])[1]["subject"], "Gasket fit check, Lab 3")
        self.assertEqual(self.post("/api/plan", {"what": "remove", "id": entry["id"]}), (200, {"ok": True, "removed": True, "told": True}))
        status, answer = self.post("/api/plan", {**order, "subject": ""})
        self.assertEqual((status, answer["error"]), (400, "Give it a title first."))
        self.assertEqual(self.post("/api/plan", order, cookie=False)[0], 401)

    def test_who_an_answer_goes_to_is_shown_and_then_exactly_that_is_sent(self):
        _, page = self.get("/api/mail?folder=inbox&q=hinge")
        _, item = self.get("/api/item?id=" + page["mail"][0]["id"])
        plan = item["answer"]["replyAll"]
        self.assertEqual(([one["name"] for one in plan["to"]], [one["name"] for one in plan["cc"]]), (["Mateo Silva"], ["Priya Nair"]))
        status, _ = self.post("/api/send", {"mode": "replyAll", "id": item["id"], "recipients": "exact", "body": "Only to you.",
                                            "to": [plan["to"][0]["address"]], "cc": []})
        self.assertEqual(status, 200)
        _, sent = self.get("/api/mail?folder=sent&q=hinge")
        _, answer = self.get("/api/item?id=" + sent["mail"][0]["id"])
        self.assertEqual((answer["to"], answer["cc"]), ("mateo.silva@lumenworks.example", ""))


class Desk(ServerCase):
    def test_desk_answers_only_this_pc_and_never_serves_mail(self):
        reply, data = self.desk("GET", "/desk/status")
        status = json.loads(data)
        self.assertEqual(reply.status, 200)
        self.assertTrue(status["mail"]["ok"] and status["https"])
        self.assertEqual(self.desk("GET", "/desk/status", host="evil.example:80")[0].status, 403)
        self.assertEqual(self.desk("POST", "/desk/pair", {"X-Titip": "1"}, host="evil.example")[0].status, 403)
        self.assertEqual(self.desk("POST", "/desk/pair")[0].status, 403)  # no header: a cross-site form
        for path in ("/api/mail?folder=inbox", "/api/state", "/app.js"):
            self.assertEqual(self.desk("GET", path)[0].status, 404, path)

    def test_desk_listens_on_loopback_only(self):
        self.assertEqual(self.app.desk.server_address[0], "127.0.0.1")


    def test_stop_button_needs_the_header_and_only_then_stops(self):
        self.assertFalse(self.app.quit.is_set())
        reply, _ = self.desk("POST", "/desk/quit")
        self.assertEqual(reply.status, 403)
        reply, _ = self.desk("POST", "/desk/quit", X, host="evil.example")
        self.assertEqual(reply.status, 403)
        self.assertFalse(self.app.quit.is_set())
        reply, data = self.desk("POST", "/desk/quit", X)
        self.assertEqual((reply.status, json.loads(data)), (200, {"stopping": True}))
        self.assertTrue(self.app.quit.wait(10))  # set just after the answer goes out; a full PC takes a moment
        self.app.quit.clear()

    def test_desk_status_does_not_wait_for_a_silent_mailbox(self):
        import time

        class Silent(DemoMailbox):
            def quick_state(self):
                return {"ok": False, "busy": True, "busyFor": 180, "error": "busy"}

        real, self.app.mailbox = self.app.mailbox, Silent()
        try:
            started = time.monotonic()
            reply, data = self.desk("GET", "/desk/status")
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(json.loads(data)["mail"]["busyFor"], 180)
        finally:
            self.app.mailbox = real


class AutopilotRoutes(ServerCase):
    def test_the_autopilot_page_needs_a_paired_phone(self):
        for path in ("/auto.html", "/auto.js", "/api/auto"):
            reply, _ = self.phone("GET", path)
            self.assertEqual(reply.status, 401, path)
        reply, _ = self.phone("POST", "/api/auto", {"command": "switch", "on": True}, X)
        self.assertEqual(reply.status, 401)

    def test_a_paired_phone_reads_the_state_and_switches(self):
        token, _, _ = self.pair()
        reply, data = self.phone("GET", "/auto.html", cookie=token)
        self.assertEqual(reply.status, 200)
        self.assertIn(b"Autopilot", data)
        reply, data = self.phone("GET", "/api/auto", cookie=token)
        state = json.loads(data)
        self.assertEqual((reply.status, state["available"], state["on"]), (200, False, False))  # no mailbox in this test
        reply, data = self.phone("POST", "/api/auto", {"command": "mode", "mode": "review"}, X, cookie=token)
        self.assertEqual((reply.status, json.loads(data)["mode"]), (200, "review"))
        reply, data = self.phone("POST", "/api/auto", {"command": "send", "id": "nope"}, X, cookie=token)
        self.assertEqual(reply.status, 400)
        reply, _ = self.phone("POST", "/api/auto", {"command": "mode", "mode": "auto"}, {"Content-Type": "application/json"}, cookie=token)
        self.assertEqual(reply.status, 403)  # no X-Titip header: a forged form cannot switch anything

    def test_the_pc_page_shows_autopilot_and_refuses_strangers(self):
        reply, data = self.desk("GET", "/auto.html")
        self.assertEqual(reply.status, 200)
        self.assertIn(b">This PC</a>", data)
        reply, data = self.desk("GET", "/desk/status")
        status = json.loads(data)
        self.assertEqual(status["autopilot"]["waiting"], 0)
        self.assertFalse(status["far"]["ready"])
        reply, data = self.desk("POST", "/desk/far", X)
        self.assertEqual((reply.status, json.loads(data)["url"]), (200, ""))  # no relay address: no code
        reply, _ = self.desk("GET", "/api/auto", host="evil.example")
        self.assertEqual(reply.status, 403)


class OneMailFunction(ServerCase):
    """Titip.mail_call answers a paired phone and the relay alike; the routes above already go through it."""

    def test_the_same_answers_as_the_phone_listener(self):
        status, state = self.app.mail_call("state", {})
        self.assertEqual((status, state["ok"], state["version"]), (200, True, self.app.status()["version"]))
        status, page = self.app.mail_call("mail", {"folder": "inbox", "limit": 3, "offset": 0, "q": ""})
        self.assertEqual((status, len(page["mail"])), (200, 3))
        status, item = self.app.mail_call("item", {"id": page["mail"][0]["id"]})
        self.assertEqual((status, item["subject"]), (200, page["mail"][0]["subject"]))
        status, sent = self.app.mail_call("send", {"mode": "reply", "id": item["id"], "body": "Noted."})
        self.assertEqual((status, sent), (200, {"ok": True, "sent": True}))

    def test_problems_are_statuses_never_exceptions(self):
        self.assertEqual(self.app.mail_call("send", {"mode": "new", "to": "", "body": "x"}), (400, {"error": "Add at least one recipient."}))
        self.assertEqual(self.app.mail_call("item", {"id": "demo.FFFF"})[0], 502)
        self.assertEqual(self.app.mail_call("mail", {"limit": "abc"})[0], 400)
        self.assertEqual(self.app.mail_call("mail", {"limit": None})[0], 400)
        for route in ("desk/quit", "auto", "pair", ""):
            self.assertEqual(self.app.mail_call(route, {}), (404, {"error": "Not found."}), route)
        real = self.app.mailbox.get

        def busy(*args):
            raise OutlookBusy()

        self.app.mailbox.get = busy
        try:
            status, answer = self.app.mail_call("item", {"id": "x"})
        finally:
            self.app.mailbox.get = real
        self.assertEqual((status, answer["busy"]), (503, True))
        self.assertFalse(self.app.busy())  # every call was counted out again
        self.app.mailbox.get = lambda *args: 1 / 0  # something nobody expected
        try:
            status, answer = self.app.mail_call("item", {"id": "x"})
        finally:
            self.app.mailbox.get = real
        self.assertEqual((status, answer["error"]), (502, "Titip on your PC could not read that (ZeroDivisionError)."))
        real_act = self.app.mailbox.act
        self.app.mailbox.act = lambda payload: {}["missing"]
        try:
            status, answer = self.app.mail_call("act", {"what": "delete", "id": "x"})
        finally:
            self.app.mailbox.act = real_act
        self.assertEqual(status, 502)
        self.assertIn("Look in Outlook before you try it again", answer["error"])
        self.assertFalse(self.app.busy())

    def test_an_attachment_for_the_relay_is_base64_and_a_large_one_is_refused(self):
        import base64

        from titip import server

        _, page = self.app.mail_call("mail", {"folder": "inbox", "q": "hinge"})
        mail_id = page["mail"][0]["id"]
        status, got = self.app.mail_call("attachment", {"id": mail_id, "index": 1})
        self.assertEqual((status, got["name"]), (200, "hinge-B-torque.xlsx"))
        self.assertEqual(len(base64.b64decode(got["data"])), got["size"])
        self.assertEqual(self.app.attachment(mail_id, 1)[1], base64.b64decode(got["data"]))
        real, server.MAX_FAR_FILE = server.MAX_FAR_FILE, 10
        try:
            status, answer = self.app.mail_call("attachment", {"id": mail_id, "index": 1})
        finally:
            server.MAX_FAR_FILE = real
        self.assertEqual(status, 502)
        self.assertIn("too large for the relay", answer["error"])
        self.assertEqual(self.app.mail_call("attachment", {"id": mail_id, "index": 99})[0], 502)

    def test_a_restart_waits_for_a_mail_request_under_way(self):
        import threading

        entered, release = threading.Event(), threading.Event()
        real = self.app.mailbox.get

        def slow(mail_id):
            entered.set()
            release.wait(10)
            return real(mail_id)

        self.app.mailbox.get = slow
        try:
            _, page = self.app.mail_call("mail", {"folder": "inbox", "limit": 1})
            worker = threading.Thread(target=self.app.mail_call, args=("item", {"id": page["mail"][0]["id"]}))
            worker.start()
            self.assertTrue(entered.wait(10))
            self.assertTrue(self.app.busy())
            release.set()
            worker.join(10)
        finally:
            self.app.mailbox.get = real
        self.assertFalse(self.app.busy())


class DeskSwitches(ServerCase):
    def post(self, path, body, headers=X):
        connection = http.client.HTTPConnection("127.0.0.1", self.desk_port, timeout=10)
        connection.request("POST", path, json.dumps(body), dict(headers))
        reply = connection.getresponse()
        data = reply.read()
        connection.close()
        return reply.status, json.loads(data)

    def test_mail_from_anywhere_is_off_and_only_the_pc_page_can_switch_it(self):
        status = json.loads(self.desk("GET", "/desk/status")[1])
        self.assertFalse(status["far"]["mail"])
        self.assertEqual(self.post("/desk/mailfar", {"on": True}, {"Content-Type": "application/json"})[0], 403)
        token = self.pair()[0]
        reply, _ = self.phone("POST", "/desk/mailfar", {"on": True}, X, token)  # a paired phone cannot
        self.assertEqual(reply.status, 404)
        self.assertFalse(self.app.remote.mail_on)
        code, status = self.post("/desk/mailfar", {"on": True})
        self.assertEqual((code, status["far"]["mail"]), (200, True))
        self.assertTrue(json.loads((Path(self.home.name) / "remote.json").read_text(encoding="utf-8"))["mail"])
        code, status = self.post("/desk/mailfar", {"on": "yes"})  # anything but true switches it off
        self.assertEqual((code, status["far"]["mail"]), (200, False))

    def test_the_update_switch_and_its_status(self):
        status = json.loads(self.desk("GET", "/desk/status")[1])
        self.assertEqual((status["update"]["available"], status["update"]["state"]), (False, "unavailable"))  # no checkout here
        code, status = self.post("/desk/update", {"on": False})
        self.assertEqual((code, status["update"]["on"]), (200, False))
        self.assertFalse(json.loads((Path(self.home.name) / "update.json").read_text(encoding="utf-8"))["on"])
        code, status = self.post("/desk/update", {"on": True, "check": True})
        self.assertEqual((code, status["update"]["on"]), (200, True))

    def test_an_update_stops_the_program_the_same_way_the_stop_button_does(self):
        self.assertFalse(self.app.restarting)
        self.app.restart_for_update()
        self.assertTrue(self.app.restarting and self.app.quit.is_set())
        self.app.restarting = False
        self.app.quit.clear()

    def test_the_phone_gets_the_relay_script_only_when_paired(self):
        self.assertEqual(self.phone("GET", "/far.js")[0].status, 401)
        reply, data = self.phone("GET", "/far.js", cookie=self.pair()[0])
        self.assertEqual(reply.status, 200)
        self.assertIn(b"TitipFar", data)


class StartsReadingAhead(unittest.TestCase):
    """After a restart the phone's first look should not wait for Outlook, but only if a phone can ask at all."""

    class Box(DemoMailbox):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()

        def read_ahead_now(self):
            self.started.set()

    def app(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        app = Titip(self.Box(), Path(home.name), phone_port=0, desk_port=0, phone_host="127.0.0.1", https=False)
        self.addCleanup(app.stop)
        return app

    def test_not_while_no_phone_is_paired_and_mail_from_anywhere_is_off(self):
        app = self.app()
        app.start()
        self.assertFalse(app.mailbox.started.wait(0.3))

    def test_with_a_paired_phone(self):
        app = self.app()
        app.devices.add("iPhone")
        app.start()
        self.assertTrue(app.mailbox.started.wait(5))

    def test_with_mail_from_anywhere_on(self):
        app = self.app()
        app.remote.mail_on = True
        app.start()
        self.assertTrue(app.mailbox.started.wait(5))

    def test_a_mailbox_that_cannot_read_ahead_is_left_alone(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        app = Titip(DemoMailbox(), Path(home.name), phone_port=0, desk_port=0, phone_host="127.0.0.1", https=False)
        self.addCleanup(app.stop)
        app.devices.add("iPhone")
        app.start()  # must not raise


if __name__ == "__main__":
    unittest.main()
