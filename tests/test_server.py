"""The two listeners over real sockets (HTTPS to the phone), with the demo mailbox behind them."""
import http.client
import json
import ssl
import tempfile
import unittest
from pathlib import Path

from titip.mailbox import DemoMailbox
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

    def test_problems_come_back_as_plain_messages(self):
        reply, data = self.phone("POST", "/api/send", {"mode": "new", "to": "", "body": "x"}, X, self.token)
        self.assertEqual(reply.status, 400)
        self.assertEqual(json.loads(data)["error"], "Add at least one recipient.")
        reply, body = self.get("/api/item?id=demo.FFFF")
        self.assertEqual(reply.status, 502)
        self.assertIn("moved or deleted", body["error"])
        self.assertEqual(self.get("/api/mail?folder=nope")[0].status, 502)
        self.assertEqual(self.get("/api/mail?limit=abc")[0].status, 400)

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
        self.assertTrue(self.app.quit.is_set())
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


if __name__ == "__main__":
    unittest.main()
