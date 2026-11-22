"""0.14.0: a phone is told about new mail and Outlook's reminders while Titip is closed (Web Push).

The sealing is checked against the worked example of RFC 8291 (its Appendix A), and by a receiver
written here on its own; the proof of who sends against RFC 8292; the rest against stand-ins."""
import datetime as dt
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from tests import test_server
from titip import mailbox, push, remote
from titip.mailbox import DemoMailbox, MailError, OutlookBusy, OutlookMailbox


def raw(text: str) -> bytes:
    return remote.unb64("".join(text.split()))


def key_of(text: str):
    return ec.derive_private_key(int.from_bytes(raw(text), "big"), ec.SECP256R1())


def a_phone(host="web.push.apple.com"):
    """A browser that signed up: its key pair, its secret, and what it hands to the PC."""
    private = ec.generate_private_key(ec.SECP256R1())
    auth = bytes(range(16))
    handed = {"endpoint": f"https://{host}/QGk3xyz", "keys": {"p256dh": remote.b64(push.public_bytes(private)), "auth": remote.b64(auth)}}
    return private, auth, handed


def opened(sealed: bytes, private, auth: bytes) -> bytes:
    """What the phone does with what arrives (RFC 8291, the receiving side), written without titip.push."""
    salt, size, length = sealed[:16], struct.unpack(">I", sealed[16:20])[0], sealed[20]
    sent_by, cipher = sealed[21:21 + length], sealed[21 + length:]
    mine = push.public_bytes(private)
    shared = private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sent_by))
    start = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + mine + sent_by).derive(shared)
    key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(start)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(start)
    plain = AESGCM(key).decrypt(nonce, cipher, None)
    assert size == 4096 and plain.endswith(b"\x02"), "one record, closed by the sign for the last one"
    return plain[:-1]


class Sealing(unittest.TestCase):
    def test_the_worked_example_of_rfc_8291_comes_out_byte_for_byte(self):
        sealed = push.seal(
            raw("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
            raw("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcx aOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
            raw("BTBZMqHH6r4Tts7J_aSIgg"),
            sender=key_of("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), salt=raw("DGv6ra1nlYgDCS1FRnbzlw"))
        header = raw("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z 9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml mlMoZIIgDll6e3vCYLocInmYWAmS6Tlz AC8wEqKK6PBru3jl7A8")
        text = raw("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrd irN6GcG7sFz1y1sqLgVi1VhjVkHsUoEs bI_0LpXMuGvnzQ")
        self.assertEqual((len(header), sealed[:86], sealed[86:]), (86, header, text))

    def test_the_phone_it_was_sealed_for_opens_it_and_no_other_does(self):
        private, auth, handed = a_phone()
        words = push.as_text({"title": "Priya Nair", "body": "RE: Friday design review agenda", "tag": "mail.1", "url": "#/inbox/s1.AB"})
        sealed = push.seal(words, raw(handed["keys"]["p256dh"]), auth)
        self.assertEqual(json.loads(opened(sealed, private, auth)),
                         {"title": "Priya Nair", "body": "RE: Friday design review agenda", "tag": "mail.1", "url": "#/inbox/s1.AB"})
        self.assertNotIn(b"Priya", sealed)
        self.assertNotIn(b"Friday", sealed)
        other, _, _ = a_phone()
        with self.assertRaises(Exception):
            opened(sealed, other, auth)
        with self.assertRaises(Exception):
            opened(sealed, private, bytes(16))
        self.assertNotEqual(sealed, push.seal(words, raw(handed["keys"]["p256dh"]), auth), "a new key and salt for every notice")

    def test_a_notice_is_kept_short_enough_for_every_push_service(self):
        long = push.as_text({"title": "T" * 500, "body": "line one\n\n   line two " + "b" * 5000, "tag": "x" * 500, "url": "#/inbox/" + "A" * 5000})
        said = json.loads(long)
        self.assertEqual((len(said["title"]), len(said["body"]), said["body"][:17]), (80, 240, "line one line two"))
        self.assertLess(len(long), 1000)
        self.assertEqual(json.loads(push.as_text({}))["title"], "Titip")


class WhoSends(unittest.TestCase):
    def test_the_proof_names_the_service_and_is_signed_with_this_pcs_key(self):
        key = ec.generate_private_key(ec.SECP256R1())
        header = push.proof(key, "https://web.push.apple.com/QGk3xyz?x=1", now=1_800_000_000)
        self.assertTrue(header.startswith("vapid t="))
        token, public = header[len("vapid t="):].split(", k=")
        self.assertEqual(remote.unb64(public), push.public_bytes(key))
        head, said, signature = token.split(".")
        self.assertEqual(json.loads(remote.unb64(head)), {"typ": "JWT", "alg": "ES256"})
        self.assertEqual(json.loads(remote.unb64(said)), {"aud": "https://web.push.apple.com", "exp": 1_800_000_000 + 12 * 3600, "sub": push.SUBJECT})
        r, s = (int.from_bytes(part, "big") for part in (remote.unb64(signature)[:32], remote.unb64(signature)[32:]))
        self.assertEqual(len(remote.unb64(signature)), 64)
        key.public_key().verify(encode_dss_signature(r, s), f"{head}.{said}".encode("ascii"), ec.ECDSA(hashes.SHA256()))  # raises when it does not fit

    def test_the_key_is_made_once_and_kept(self):
        with tempfile.TemporaryDirectory() as home:
            first = push.public_bytes(push.own_key(Path(home)))
            self.assertEqual((len(first), first[0]), (65, 4))
            self.assertEqual(push.public_bytes(push.own_key(Path(home))), first)
            self.assertEqual(remote.unb64(push.Push(Path(home)).public()), first)
            self.assertIn(b"PRIVATE KEY", (Path(home) / "push-key.pem").read_bytes())


class WhereANoticeMayGo(unittest.TestCase):
    def test_only_to_the_push_services_of_the_browsers(self):
        _, _, handed = a_phone()
        for host in ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com", "db5p.notify.windows.com"):
            self.assertEqual(push.clean_phone({**handed, "endpoint": f"https://{host}/send/abc"})["endpoint"], f"https://{host}/send/abc")
        for bad in ("http://web.push.apple.com/x", "https://192.168.1.5/x", "https://localhost:47779/api/send", "https://push.apple.com.example.net/x",
                    "https://evilpush.apple.com/x", "https://example.com/web.push.apple.com", "ftp://web.push.apple.com/x", "", None, "https://web.push.apple.com/" + "a" * 3000):
            with self.assertRaises(MailError, msg=bad):
                push.clean_phone({**handed, "endpoint": bad})

    def test_keys_that_are_not_keys_are_refused(self):
        _, _, handed = a_phone()
        for keys in ({}, {"p256dh": handed["keys"]["p256dh"]}, {"p256dh": "AAAA", "auth": handed["keys"]["auth"]},
                     {"p256dh": handed["keys"]["p256dh"], "auth": "AAAA"}, {"p256dh": "not base64!", "auth": "@@"}, "keys"):
            with self.assertRaises(MailError, msg=keys):
                push.clean_phone({"endpoint": handed["endpoint"], "keys": keys})
        with self.assertRaises(MailError):
            push.clean_phone("subscription")


class Posted:
    """Stands in for the way out: remembers what was posted, and answers as told."""

    def __init__(self, status=201):
        self.status, self.calls = status, []

    def __call__(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body, headers))
        if isinstance(self.status, Exception):
            raise self.status
        return self.status, b""


class Telling(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)

    def test_a_notice_is_posted_sealed_with_the_proof_and_only_the_phone_can_read_it(self):
        private, auth, handed = a_phone()
        out = Posted()
        teller = push.Push(Path(self.home.name), send=out)
        teller.add(handed)
        self.assertEqual(teller.tell([{"kind": "mail", "title": "Priya Nair", "body": "Agenda", "tag": "mail.1", "url": "#/inbox/1"}]),
                         {"phones": 1, "reached": 1, "why": ""})
        method, url, body, headers = out.calls[0]
        self.assertEqual((method, url, headers["Content-Encoding"], headers["TTL"]), ("POST", handed["endpoint"], "aes128gcm", str(push.TTL)))
        self.assertTrue(headers["Authorization"].startswith("vapid t=") and headers["Authorization"].endswith(", k=" + teller.public()))
        self.assertEqual(json.loads(opened(body, private, auth))["title"], "Priya Nair")
        self.assertIn("Told 1 phone at", teller.last)

    def test_the_phones_are_kept_over_a_restart_and_the_oldest_gives_way(self):
        teller = push.Push(Path(self.home.name), send=Posted())
        phones = [a_phone()[2] for _ in range(push.PHONES_MOST + 2)]
        for number, handed in enumerate(phones):
            handed["endpoint"] += str(number)
            teller.add(handed)
        teller.add(phones[-1])  # the same phone again is the same phone
        again = push.Push(Path(self.home.name), send=Posted())
        self.assertEqual([one["endpoint"] for one in again.phones], [one["endpoint"] for one in phones[2:]])
        self.assertTrue(again.knows(phones[-1]["endpoint"]) and not again.knows(phones[0]["endpoint"]))
        self.assertTrue(again.remove(phones[-1]["endpoint"]) and not again.remove(phones[-1]["endpoint"]))
        self.assertEqual(len(push.Push(Path(self.home.name)).phones), push.PHONES_MOST - 1)
        (Path(self.home.name) / "push.json").write_text("not json")
        self.assertEqual(push.Push(Path(self.home.name)).phones, [])

    def test_a_phone_that_signed_off_is_forgotten_and_one_that_cannot_be_reached_is_said(self):
        for status in (404, 410):
            out = Posted(status)
            teller = push.Push(Path(self.home.name), send=out)
            teller.add(a_phone()[2])
            told = teller.tell([{"title": "x"}])
            self.assertEqual((told["reached"], teller.phones, told["why"]), (0, [], "The phone has switched notices off."))
        for trouble, said in ((OSError("no way out"), "no way out"), (403, "the push service answered 403")):
            teller = push.Push(Path(self.home.name), send=Posted(trouble))
            teller.add(a_phone()[2])
            told = teller.tell([{"title": "x"}])
            self.assertEqual((told["reached"], len(teller.phones)), (0, 1), "it stays signed up: the next notice may get through")
            self.assertIn(said, told["why"])

    def test_many_mails_at_once_are_one_line_and_reminders_still_come_each(self):
        private, auth, handed = a_phone()
        out = Posted()
        teller = push.Push(Path(self.home.name), send=out)
        teller.add(handed)
        mails = [{"kind": "mail", "title": name, "body": "x", "tag": f"mail.{n}"} for n, name in enumerate(["Priya Nair", "Jonas Weber", "Priya Nair", "Ada Okafor"])]
        teller.tell(mails + [{"kind": "reminder", "title": "Reminder", "body": "Stand-up, 09:30", "tag": "remind.1"}])
        said = [json.loads(opened(body, private, auth)) for _, _, body, _ in out.calls]
        self.assertEqual([(one["title"], one["body"]) for one in said], [("Reminder", "Stand-up, 09:30"), ("4 new mails", "Priya Nair, Jonas Weber, Ada Okafor")])
        out.calls.clear()
        teller.tell(mails)
        self.assertEqual(len(out.calls), 4, "up to four, each on its own")

    def test_one_look_tells_what_the_mailbox_says_is_new_and_a_mailbox_that_cannot_say_is_asked_again(self):
        box = DemoMailbox()
        out = Posted()
        teller = push.Push(Path(self.home.name), mailbox=box, send=out, every=0.01)
        teller.add(a_phone()[2])
        self.assertIsNone(teller.look())
        box.arrived = [{"kind": "mail", "title": "Priya Nair", "body": "Agenda", "tag": "mail.9"}]
        self.assertEqual(teller.look()["reached"], 1)
        self.assertIsNone(teller.look(), "told once")
        box.news = lambda: 1 / 0
        self.assertIsNone(teller.look())
        teller.stop()

    def test_outlook_is_looked_at_only_while_a_phone_wants_to_be_told(self):
        box = DemoMailbox()
        looks = []
        box.news = lambda: looks.append(1) or []
        teller = push.Push(Path(self.home.name), mailbox=box, send=Posted(), every=0.01)
        teller.start()
        self.assertIsNone(teller._thread, "nobody to tell: no thread, no look")
        handed = a_phone()[2]
        teller.add(handed)
        for _ in range(200):
            if len(looks) >= 2:
                break
            __import__("time").sleep(0.01)
        self.assertGreaterEqual(len(looks), 2)
        teller.remove(handed["endpoint"])
        teller._thread.join(2)
        self.assertFalse(teller._thread.is_alive(), "the last phone signed off: the looking stops")
        teller.stop()


def row(entry, time, unread=True, sender="Priya Nair", subject="Agenda"):
    return {"id": f"s1.{entry}", "time": time, "unread": unread, "from": sender, "subject": subject}


class Reminder:
    def __init__(self, caption, due, visible, start=None):
        self.Caption, self.NextReminderDate, self.IsVisible = caption, due, visible
        self.Item = type("Item", (), {"Start": start})() if start else type("Item", (), {})()


class Reminders(list):
    Count = property(len)

    def Item(self, index):
        return self[index - 1]


class WhatIsNew(unittest.TestCase):
    def box(self, rows, reminders=()):
        box = OutlookMailbox(read_ahead=False)
        box.rows, box.reminders = list(rows), Reminders(reminders)
        box._list = lambda folder, limit, offset, query: {"mail": list(box.rows)[:limit]}
        box._app = type("App", (), {"Reminders": box.reminders})()
        return box

    def test_mail_that_came_since_the_last_look_is_told_once_and_only_while_unread(self):
        box = self.box([row("A1", "2026-11-22T01:00:00Z"), row("A0", "2026-11-22T00:00:00Z", unread=False)])
        self.assertEqual(box._news_now(), [], "the first look: what is there already is not news")
        box.rows[:0] = [row("A3", "2026-11-22T01:20:00Z", sender="Jonas Weber", subject="Drop test"), row("A2", "2026-11-22T01:10:00Z", unread=False)]
        self.assertEqual(box._news_now(), [{"kind": "mail", "title": "Jonas Weber", "body": "Drop test", "tag": "mail.s1.A3", "url": "#/inbox/s1.A3"}],
                         "the one you read on the PC meanwhile is not announced")
        self.assertEqual(box._news_now(), [])
        box.rows[:0] = [row("A5", "2026-11-22T02:00:00Z", sender=""), row("A4", "2026-11-22T01:30:00Z")]
        self.assertEqual([(one["title"], one["tag"]) for one in box._news_now()], [("Priya Nair", "mail.s1.A4"), ("New mail", "mail.s1.A5")], "the oldest first")

    def test_a_reminder_outlook_is_showing_is_told_once_with_when_it_starts(self):
        showing = Reminder("Old one", dt.datetime(2026, 11, 21, 9, 0), True)
        waiting = Reminder("Stand-up,  hinge team", dt.datetime(2026, 11, 22, 9, 15), False, start=dt.datetime(2026, 11, 22, 9, 30))
        box = self.box([], [showing, waiting])
        self.assertEqual(box._news_now(), [], "what was showing when the first phone signed up is not news")
        self.assertEqual(box._news_now(), [])
        waiting.IsVisible = True
        told = box._news_now()
        self.assertEqual([(one["kind"], one["title"], one["body"], one["url"]) for one in told], [("reminder", "Reminder", "Stand-up, hinge team, 09:30", "#/calendar")])
        self.assertTrue(told[0]["tag"].startswith("remind."))
        self.assertEqual(box._news_now(), [], "it keeps showing in Outlook; the phone is told once")
        waiting.NextReminderDate = dt.datetime(2026, 11, 22, 9, 25)  # snoozed on the PC, and due again
        self.assertEqual(len(box._news_now()), 1)
        box.reminders.append(Reminder("A task without a start", dt.datetime(2026, 11, 22, 10, 0), True))
        self.assertEqual(box._news_now()[0]["body"], "A task without a start")

    def test_an_outlook_without_reminders_or_one_that_is_broken_still_tells_the_mail(self):
        box = self.box([row("A1", "2026-11-22T01:00:00Z")])
        box._app = type("App", (), {})()
        box._news_now()
        box.rows.insert(0, row("A2", "2026-11-22T01:05:00Z"))
        self.assertEqual(len(box._news_now()), 1)

        class Broken:
            IsVisible = property(lambda self: 1 / 0)

        box = self.box([], [Broken(), Reminder("Fine", dt.datetime(2026, 11, 22, 9, 0), False)])
        box._news_now()
        box.reminders[1].IsVisible = True
        self.assertEqual([one["body"] for one in box._news_now()], ["Fine"])

    def test_an_outlook_that_turns_busy_at_the_reminders_still_hands_back_the_mail_it_found(self):
        busy = type("Busy", (Exception,), {"hresult": mailbox.RPC_BUSY[0]})

        class Turned:
            IsVisible = property(lambda self: (_ for _ in ()).throw(busy()))

        box = self.box([row("A1", "2026-11-22T01:00:00Z")], [Reminder("Showing", dt.datetime(2026, 11, 22, 9, 0), True)])
        box._news_now()
        box.rows.insert(0, row("A2", "2026-11-22T01:05:00Z"))
        box.reminders.insert(0, Turned())
        self.assertEqual([one["tag"] for one in box._news_now()], ["mail.s1.A2"], "the mail is not lost with the reminders")
        box.reminders.pop(0)
        box.reminders.append(Reminder("Later", dt.datetime(2026, 11, 22, 10, 0), True))
        self.assertEqual([one["body"] for one in box._news_now()], ["Later"])

    def test_outlook_is_never_started_for_it_and_a_busy_one_is_no_news(self):
        box = self.box([])
        box._do = lambda fn, *args, **more: self.fail("Outlook was asked")
        with mock.patch.object(mailbox, "outlook_running", lambda: False):
            self.assertEqual(box.news(), [])
        with mock.patch.object(mailbox, "outlook_running", lambda: True):
            box._do = lambda fn, *args, **more: (_ for _ in ()).throw(OutlookBusy())
            self.assertEqual(box.news(), [])
            box._do = lambda fn, *args, timeout=0, first=False: fn(*args)
            self.assertEqual(box.news(), [])


class OverTheWire(test_server.ServerCase):
    def setUp(self):
        self.token = self.pair()[0]
        self.out = Posted()
        self.app.push.send = self.out

    def get(self, path, cookie=True):
        reply, data = self.phone("GET", path, cookie=self.token if cookie else None)
        return reply.status, json.loads(data) if reply.status == 200 else None

    def post(self, path, body, cookie=True):
        reply, data = self.phone("POST", path, body, test_server.X, self.token if cookie else None)
        return reply.status, json.loads(data)

    def test_a_paired_phone_signs_up_is_sent_a_test_and_signs_off(self):
        private, auth, handed = a_phone()
        status, state = self.get("/api/push")
        self.assertEqual((status, state["on"], len(remote.unb64(state["key"]))), (200, False, 65))
        self.assertEqual(self.post("/api/pushset", {"what": "on", "subscription": handed}), (200, {"ok": True, "on": True, "reached": True, "why": ""}))
        self.assertEqual(json.loads(opened(self.out.calls[0][2], private, auth))["body"], "Notices from your PC reach this phone.")
        from urllib.parse import quote

        self.assertTrue(self.get("/api/push?endpoint=" + quote(handed["endpoint"], safe=""))[1]["on"])
        self.assertEqual(self.app.mail_call("push", {"endpoint": handed["endpoint"]})[1]["on"], True)  # what the relay is answered with
        self.assertEqual(self.post("/api/pushset", {"what": "test", "endpoint": handed["endpoint"]})[1]["reached"], True)
        self.out.status = OSError("the proxy wants a password")
        status, answer = self.post("/api/pushset", {"what": "test", "endpoint": handed["endpoint"]})
        self.assertEqual((status, answer["reached"]), (200, False))
        self.assertIn("could not reach the phone's push service", answer["why"])
        self.assertEqual(self.post("/api/pushset", {"what": "off", "endpoint": handed["endpoint"]}), (200, {"ok": True, "on": False}))
        self.assertIn("not signed up", self.post("/api/pushset", {"what": "test", "endpoint": handed["endpoint"]})[1]["why"])
        self.assertEqual(self.app.push.phones, [])

    def test_a_stranger_or_an_address_inside_the_network_gets_nowhere(self):
        _, _, handed = a_phone()
        status, answer = self.post("/api/pushset", {"what": "on", "subscription": {**handed, "endpoint": "https://192.168.1.1/admin"}})
        self.assertEqual((status, answer["error"]), (400, "That is not the address of a push service Titip knows."))
        self.assertEqual(self.post("/api/pushset", {"what": "burn"})[0], 400)
        self.assertEqual(self.get("/api/push", cookie=False)[0], 401)
        self.assertEqual(self.post("/api/pushset", {"what": "on", "subscription": handed}, cookie=False)[0], 401)
        self.assertEqual(self.phone("POST", "/api/pushset", {"what": "on", "subscription": handed}, cookie=self.token)[0].status, 403)
        self.assertEqual((self.out.calls, self.app.push.phones), ([], []))
        self.assertTrue({"push", "pushset"} <= set(remote.ASK_ROUTES) and "pushset" in remote.ONCE_ROUTES and "push" not in remote.ONCE_ROUTES)

    def test_the_worker_is_served_to_the_phone_like_the_rest_of_the_page(self):
        reply, data = self.phone("GET", "/sw.js", cookie=self.token)
        self.assertEqual((reply.status, reply.getheader("Content-Type")), (200, "text/javascript; charset=utf-8"))
        self.assertIn(b"showNotification", data)
        self.assertNotIn(b"fetch", data, "it answers no request: the app loads as it does without it")


if __name__ == "__main__":
    unittest.main()
