"""The link for any network: what leaves the PC is sealed, and only fresh, known orders are carried out."""
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from titip import remote
from titip.remote import Remote, b64, seal, unb64, unseal


class Pilot:
    def __init__(self):
        self.orders = []
        self.events = [{"id": "e1", "kind": "sent", "text": "Hello Tomas"}]

    def snapshot(self):
        return {"on": True, "held": [], "events": list(self.events), "now": int(time.time())}

    def command(self, name, **details):
        if name == "send" and details.get("id") == "gone":
            raise ValueError("That mail is no longer waiting.")
        self.orders.append((name, details))
        return self.snapshot()


class Relay:
    """The worker's rules, in a few lines: the first writer owns the channel, orders pile up until taken."""

    def __init__(self):
        self.owner, self.state, self.orders, self.writes = {}, {}, {}, 0
        self.asks, self.answers = {}, {}

    def send(self, method, url, body=None, headers=None):
        parts = urlsplit(url).path.split("/")
        channel, what = parts[2], "/".join(parts[3:])
        token = (headers or {}).get("X-Titip-Write", "")
        if what == "state" and method == "PUT":
            if self.owner.setdefault(channel, token) != token:
                return 403, b""
            self.state[channel] = body.decode("ascii")
            self.writes += 1
            return 200, b"ok"
        if what == "state" and method == "GET":
            return (200, self.state[channel].encode()) if channel in self.state else (404, b"")
        if what == "orders" and method == "POST":
            self.orders.setdefault(channel, []).append(body.decode("ascii"))
            return 200, b"ok"
        if what == "orders/take":
            if self.owner.get(channel) != token:
                return 404, b""
            return 200, json.dumps({"orders": self.orders.pop(channel, [])}).encode()
        if what == "" and method == "DELETE":
            self.state.pop(channel, None)
            return 200, b"ok"
        if what.startswith("ask/") and method == "POST":
            self.asks.setdefault(channel, []).append({"id": parts[4], "body": body.decode("ascii")})
            return 200, b"ok"
        if what == "asks/take":
            if self.owner.get(channel) != token:
                return 404, b""
            return 200, json.dumps({"asks": self.asks.pop(channel, [])}).encode()
        if what.startswith("answer/") and method == "PUT":
            if self.owner.get(channel) != token:
                return 404, b""
            self.answers[parts[4]] = body.decode("ascii")
            return 200, b"ok"
        return 404, b""


class Sealing(unittest.TestCase):
    def test_round_trip_and_tamper(self):
        key = os.urandom(32)
        sealed = seal(key, {"text": "Halo, apa kabar? \u2713"})
        self.assertEqual(unseal(key, sealed), {"text": "Halo, apa kabar? \u2713"})
        self.assertNotIn("Halo", sealed)
        self.assertIsNone(unseal(os.urandom(32), sealed))
        raw = bytearray(unb64(sealed))
        raw[-1] ^= 1
        self.assertIsNone(unseal(key, b64(bytes(raw))))
        self.assertIsNone(unseal(key, "not base64 at all"))
        self.assertNotEqual(seal(key, {"a": 1}), seal(key, {"a": 1}))  # a fresh nonce every time

    def test_long_texts_and_old_events_are_left_out(self):
        snapshot = {"events": [{"text": "x" * 5000}] * 100}
        small = remote.slim(snapshot)
        self.assertEqual(len(small["events"]), remote.MAX_EVENTS)
        self.assertLess(len(small["events"][0]["text"]), remote.MAX_TEXT + 10)


class Link(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name)
        self.now = 1_800_000_000.0
        self.relay, self.pilot = Relay(), Pilot()

    def make(self, relay="https://relay.example"):
        if relay:
            (self.home / "remote.json").write_text(json.dumps({"relay": relay}), encoding="utf-8")
        return Remote(self.home, self.pilot, send=self.relay.send, clock=lambda: self.now)

    def order(self, link, **order):
        sealed = seal(link.key, {"nonce": os.urandom(6).hex(), "at": self.now, **order})
        self.relay.send("POST", link._url("orders"), sealed.encode("ascii"))

    def test_without_a_relay_address_nothing_happens(self):
        link = self.make(relay="")
        self.assertFalse(link.ready)
        self.assertEqual(link.link(), "")
        link.start()
        self.assertIsNone(link._thread)

    def test_the_state_reaches_the_phone_only_as_ciphertext(self):
        link = self.make()
        link.step()
        stored = self.relay.state[link.settings["channel"]]
        self.assertNotIn("Tomas", stored)
        self.assertEqual(unseal(link.key, stored)["events"][0]["text"], "Hello Tomas")

    def test_the_code_carries_the_key_after_the_hash_and_survives_a_restart(self):
        link = self.make()
        address, _, ticket = link.link().partition("#r=")
        self.assertEqual(address, remote.PAGE)
        self.assertNotIn(link.settings["key"], address)
        told = json.loads(unb64(ticket))
        self.assertEqual(told, {"u": "https://relay.example", "c": link.settings["channel"], "k": link.settings["key"]})
        self.assertNotIn(link.settings["write"], link.link())  # the phone can never overwrite the state
        self.assertEqual(self.make(relay="").link(), link.link())

    def test_the_code_opens_the_mail_app_only_while_mail_from_anywhere_is_on(self):
        link = self.make()
        link.mail = lambda route, params: (200, {})
        autopilot_page = link.link()
        link.set_mail(True)
        address, _, ticket = link.link().partition("#r=")
        self.assertEqual(address, remote.PAGE.rsplit("/", 1)[0] + "/")
        self.assertEqual(ticket, autopilot_page.partition("#r=")[2], "the same code: only the page it opens differs")
        link.set_mail(False)
        self.assertEqual(link.link(), autopilot_page)
        link.mail = None  # a Titip without a mailbox has no mail app to show
        link.set_mail(True)
        self.assertEqual(link.link(), autopilot_page)

    def test_nothing_is_written_twice_and_bursts_wait(self):
        link = self.make()
        link.step()
        link.touch()
        link.step()  # touched, but nothing changed and the gap is not over
        self.assertEqual(self.relay.writes, 1)
        self.pilot.events.append({"id": "e2", "kind": "held", "text": "new"})
        link.touch()
        link.step()
        self.assertEqual(self.relay.writes, 1)
        self.now += remote.PUBLISH_GAP_S
        link.step()
        self.assertEqual(self.relay.writes, 2)
        self.now += remote.HEARTBEAT_S
        link.step()  # the heartbeat: the phone can tell the PC is alive
        self.assertEqual(self.relay.writes, 3)

    def test_orders_from_the_phone_are_carried_out_once(self):
        link = self.make()
        link.step()
        sealed = seal(link.key, {"nonce": "n1", "at": self.now, "name": "switch", "on": False})
        for _ in range(2):  # the same order replayed
            self.relay.send("POST", link._url("orders"), sealed.encode("ascii"))
            link.step()
        self.order(link, name="send", id="h1", text="Hi", command="send")
        link.step()
        self.assertEqual(self.pilot.orders, [("switch", {"on": False}), ("send", {"id": "h1", "text": "Hi"})])

    def test_stale_forged_and_unknown_orders_are_dropped(self):
        link = self.make()
        link.step()
        self.order(link, name="switch", on=True, at=self.now - remote.ORDER_MAX_AGE_S - 1)
        self.order(link, name="forward", to="thief@mail.example")
        self.order(link, name="send", id="gone", text="Hi")  # refused by the autopilot: no crash
        self.relay.send("POST", link._url("orders"), seal(os.urandom(32), {"nonce": "x", "at": self.now, "name": "switch", "on": True}).encode())
        self.relay.send("POST", link._url("orders"), b"garbage")
        link.step()
        self.assertEqual(self.pilot.orders, [])

    def test_a_new_code_cuts_the_old_one_off(self):
        link = self.make()
        link.step()
        old = dict(link.settings)
        link.renew()
        self.assertNotEqual((old["channel"], old["key"]), (link.settings["channel"], link.settings["key"]))
        self.assertNotIn(old["channel"], self.relay.state)
        self.now += remote.PUBLISH_GAP_S
        link.step()
        self.assertIn(link.settings["channel"], self.relay.state)

    def test_a_relay_that_is_down_is_an_error_not_a_crash(self):
        link = self.make()
        link.send = lambda *args, **kwargs: (503, b"")
        with self.assertRaises(OSError):
            link.step()


class Mailbox:
    """Stands where Titip.mail_call stands: (route, params) -> (status, data)."""

    def __init__(self):
        self.calls = []
        self.reply = None

    def __call__(self, route, params):
        self.calls.append((route, params))
        if self.reply is not None:
            return self.reply
        if route == "send":
            return 200, {"ok": True, "sent": True}
        return 200, {"folder": params.get("folder", ""), "mail": [{"subject": "Quarter figures for Tomas"}]}


class MailFromAnywhere(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name)
        self.now = 1_800_000_000.0
        self.relay, self.pilot, self.mailbox = Relay(), Pilot(), Mailbox()

    def make(self, mail_on=True):
        (self.home / "remote.json").write_text(json.dumps({"relay": "https://relay.example", "mail": mail_on}), encoding="utf-8")
        link = Remote(self.home, self.pilot, send=self.relay.send, clock=lambda: self.now, mail=self.mailbox)
        link.step()  # the first state claims the channel
        return link

    def ask(self, link, route, params=None, identity=None, key=None, **more):
        identity = identity or os.urandom(9).hex()
        sealed = seal(key or link.key, {"id": identity, "at": self.now, "route": route, "params": params or {}, **more})
        self.relay.send("POST", link._url(f"ask/{identity}"), sealed.encode("ascii"))
        return identity

    def round(self, link):
        """One look by the PC, with every answer finished. Returns what the phone can then read, opened."""
        link.mail_step()
        for _ in range(200):
            if not link.working:
                break
            time.sleep(0.01)
        return {identity: unseal(link.key, sealed) for identity, sealed in self.relay.answers.items()}

    def test_it_is_off_until_switched_on_and_the_switch_survives_a_restart(self):
        link = self.make(mail_on=False)
        self.assertFalse(link.status()["mail"])
        self.assertFalse(unseal(link.key, self.relay.state[link.settings["channel"]])["mail"])
        link.set_mail(True)
        self.assertTrue(json.loads((self.home / "remote.json").read_text(encoding="utf-8"))["mail"])
        again = Remote(self.home, self.pilot, send=self.relay.send, clock=lambda: self.now, mail=self.mailbox)
        self.assertTrue(again.status()["mail"])
        self.assertEqual(again.link(), link.link())  # the same code keeps working
        self.now += remote.PUBLISH_GAP_S
        again.step()
        self.assertTrue(unseal(again.key, self.relay.state[again.settings["channel"]])["mail"])  # the phone shows its Mail tab
        again.set_mail(False)
        self.assertFalse(Remote(self.home, self.pilot, send=self.relay.send, mail=self.mailbox).status()["mail"])

    def test_a_setting_that_is_not_exactly_true_means_off(self):
        (self.home / "remote.json").write_text(json.dumps({"relay": "https://relay.example", "mail": "yes"}), encoding="utf-8")
        self.assertFalse(Remote(self.home, self.pilot, send=self.relay.send, mail=self.mailbox).mail_on)

    def test_a_question_is_answered_and_only_ciphertext_passes_the_relay(self):
        link = self.make()
        identity = self.ask(link, "mail", {"folder": "inbox", "limit": 40})
        waiting = self.relay.asks[link.settings["channel"]][0]["body"]
        self.assertNotIn("inbox", waiting)
        answers = self.round(link)
        self.assertEqual(self.mailbox.calls, [("mail", {"folder": "inbox", "limit": 40})])
        self.assertEqual(answers[identity]["status"], 200)
        self.assertEqual(answers[identity]["id"], identity)
        self.assertEqual(answers[identity]["data"]["mail"][0]["subject"], "Quarter figures for Tomas")
        self.assertNotIn("Tomas", self.relay.answers[identity])
        self.assertEqual(link.status()["lastAsk"], int(self.now))

    def test_a_send_is_carried_out_once_however_often_it_arrives(self):
        link = self.make()
        self.now += 5
        identity = os.urandom(9).hex()
        sealed = seal(link.key, {"id": identity, "at": self.now, "route": "send", "params": {"mode": "new", "to": "a@mail.example", "body": "Hi"}})
        for _ in range(3):  # the relay, or someone on the way, plays it again
            self.relay.send("POST", link._url(f"ask/{identity}"), sealed.encode("ascii"))
            self.round(link)
        self.relay.send("POST", link._url("ask/another-name"), sealed.encode("ascii"))  # the same ask under a new name
        answers = self.round(link)
        self.assertEqual([route for route, _ in self.mailbox.calls], ["send"])
        self.assertEqual(list(answers), [identity])

    def test_old_asks_and_sends_from_before_the_start_are_refused_with_a_reason(self):
        link = self.make()
        old = self.ask(link, "mail", at=self.now - remote.ASK_MAX_AGE_S - 1)
        early = self.ask(link, "send", {"mode": "new", "to": "a@mail.example", "body": "Hi"}, at=self.now - 30)
        answers = self.round(link)
        self.assertEqual(self.mailbox.calls, [])
        self.assertEqual(answers[old]["status"], 400)
        self.assertIn("clocks", answers[old]["data"]["error"])
        self.assertEqual(answers[early]["status"], 400)
        self.assertIn("nothing was sent", answers[early]["data"]["error"])
        read = self.ask(link, "mail", at=self.now - 30)  # reading again does no harm
        self.assertEqual(self.round(link)[read]["status"], 200)

    def test_forged_unknown_and_garbled_asks_get_no_answer(self):
        link = self.make()
        self.ask(link, "mail", key=os.urandom(32))
        self.ask(link, "delete", {"id": "x"})
        self.ask(link, "desk/quit")
        self.relay.send("POST", link._url("ask/garbage-1"), b"garbage")
        self.relay.asks[link.settings["channel"]].append("not even a pair")
        self.assertEqual(self.round(link), {})
        self.assertEqual(self.mailbox.calls, [])

    def test_a_busy_outlook_and_a_crash_both_reach_the_phone(self):
        link = self.make()
        self.mailbox.reply = (503, {"error": "Outlook is busy.", "busy": True})
        busy = self.ask(link, "item", {"id": "m1"})
        self.assertEqual(self.round(link)[busy], {"id": busy, "status": 503, "data": {"error": "Outlook is busy.", "busy": True}})
        link.mail = lambda route, params: 1 / 0
        crash = self.ask(link, "item", {"id": "m1"})
        self.assertEqual(self.round(link)[crash]["status"], 500)

    def test_a_very_long_formatted_mail_goes_as_plain_text_and_too_much_is_an_error(self):
        link = self.make()
        real, remote.MAX_ANSWER = remote.MAX_ANSWER, 4000
        self.addCleanup(setattr, remote, "MAX_ANSWER", real)
        self.mailbox.reply = (200, {"subject": "Long", "body": "short text", "html": "<p>" + os.urandom(4000).hex() + "</p>"})
        long_mail = self.ask(link, "item", {"id": "m1"})
        answer = self.round(link)[long_mail]
        self.assertEqual((answer["status"], answer["data"]["html"], answer["data"]["body"]), (200, "", "short text"))
        self.mailbox.reply = (200, {"name": "plan.pdf", "data": os.urandom(4000).hex()})
        big_file = self.ask(link, "attachment", {"id": "m1", "index": 1})
        answer = self.round(link)[big_file]
        self.assertEqual(answer["status"], 413)
        self.assertIn("too large", answer["data"]["error"])

    def test_the_pc_looks_often_while_someone_reads_and_seldom_otherwise(self):
        link = self.make()
        self.assertEqual(link.mail_step(), remote.ASK_IDLE_S)
        self.ask(link, "state")
        self.assertEqual(link.mail_step(), remote.ASK_BUSY_S)
        self.now += remote.ASK_BUSY_FOR_S + 1
        self.assertEqual(link.mail_step(), remote.ASK_RECENT_S, "a phone put down is often picked up again")
        self.now += remote.ASK_RECENT_FOR_S
        self.assertEqual(link.mail_step(), remote.ASK_IDLE_S)
        self.assertLess(remote.ASK_BUSY_S, remote.ASK_RECENT_S)
        self.assertLess(remote.ASK_RECENT_S, remote.ASK_IDLE_S)

    def test_looking_for_questions_all_day_stays_well_inside_what_the_relay_allows(self):
        idle_looks = 24 * 3600 / remote.ASK_IDLE_S + 24 * 3600 / remote.POLL_S  # for mail, and for Autopilot's orders
        self.assertLess(idle_looks, 40_000, "the free plan counts 100,000 requests a day")
        ten_pickups = 10 * (remote.ASK_BUSY_FOR_S / remote.ASK_BUSY_S + remote.ASK_RECENT_FOR_S / remote.ASK_RECENT_S)
        self.assertLess(idle_looks + ten_pickups, 60_000)

    def test_switched_off_the_pc_never_asks_the_relay_for_mail(self):
        link = self.make(mail_on=False)
        asked = []
        link.send = lambda method, url, body=None, headers=None: asked.append(url) or self.relay.send(method, url, body, headers)
        self.ask(link, "mail", {"folder": "inbox"})
        asked.clear()
        runner = threading.Thread(target=link._run_mail, daemon=True)
        runner.start()
        time.sleep(0.3)
        self.assertEqual(asked, [])
        self.assertEqual(self.mailbox.calls, [])
        link.set_mail(True)  # the switch wakes it at once
        for _ in range(300):
            if self.mailbox.calls:
                break
            time.sleep(0.01)
        link.stop()
        runner.join(5)
        self.assertEqual(self.mailbox.calls, [("mail", {"folder": "inbox"})])

    def test_a_relay_without_mail_or_out_of_reach_is_not_a_crash(self):
        link = self.make()
        link.send = lambda *args, **kwargs: (404, b"")  # an older relay
        self.assertEqual(link.mail_step(), remote.ASK_IDLE_S)
        link.send = lambda *args, **kwargs: (503, b"")
        with self.assertRaises(OSError):
            link.mail_step()

    def test_an_answer_that_did_not_arrive_is_handed_over_once_more_and_nothing_is_done_twice(self):
        link = self.make()
        patch = mock.patch.object(remote, "ANSWER_AGAIN_S", 0)
        patch.start()
        self.addCleanup(patch.stop)
        lost = []

        def flaky(method, url, body=None, headers=None):
            if "/answer/" in url and len(lost) < self.lose:
                lost.append(url)
                raise OSError("the proxy closed the connection")
            return self.relay.send(method, url, body, headers)

        link.send = flaky
        self.lose = 1
        identity = self.ask(link, "send", {"mode": "new", "to": "a@mail.example", "body": "Hi"})
        answers = self.round(link)
        self.assertEqual(answers[identity]["data"], {"ok": True, "sent": True})
        self.assertEqual([route for route, _ in self.mailbox.calls], ["send"], "only the answer is repeated, never the send")
        self.assertEqual(link.mail_error, "")
        lost.clear()
        self.lose = 2  # the relay stays out of reach: say so on the PC page instead of crashing
        second = self.ask(link, "mail", {"folder": "inbox"})
        self.assertNotIn(second, self.round(link))
        self.assertEqual(len(lost), 2)
        self.assertIn("could not be handed to the relay", link.mail_error)

    def test_the_composer_may_ask_for_people(self):
        self.assertIn("people", remote.ASK_ROUTES)
        link = self.make()
        self.mailbox.reply = (200, {"people": [{"name": "Priya Nair", "address": "priya.nair@lumenworks.example"}]})
        identity = self.ask(link, "people")
        answers = self.round(link)
        self.assertEqual(self.mailbox.calls, [("people", {})])
        self.assertEqual(answers[identity]["data"]["people"][0]["name"], "Priya Nair")
        self.assertNotIn("Priya", self.relay.answers[identity])

    def test_after_a_failure_the_pc_looks_again_soon_only_while_someone_reads(self):
        link = self.make()
        waits = []

        def failing():
            link._stop.set()  # one round is enough
            raise OSError("no way out")

        link.mail_step = failing
        link._stop.wait = waits.append
        link.last_ask = self.now - 5  # someone is reading mail right now
        link._run_mail()
        self.assertIn("could not be reached", link.mail_error)
        link._stop.clear()
        link.last_ask = self.now - remote.ASK_BUSY_FOR_S - 1  # nobody has for a while
        link._run_mail()
        self.assertEqual(waits, [remote.ASK_RETRY_BUSY_S, remote.ASK_RETRY_S])
        self.assertLess(remote.ASK_RETRY_BUSY_S, remote.ASK_RETRY_S)


class WaysOut(unittest.TestCase):
    """A dead proxy or a different network must not cut the PC off from the relay."""

    PROXY, OFFICE, STRAIGHT = {"https": "http://localhost:3128"}, {"https": "http://proxy.office:80"}, {}

    def setUp(self):
        remote._good_way[0] = 0
        self.addCleanup(remote._good_way.__setitem__, 0, 0)
        self.tried, self.requests = [], []

    def ask(self, behaviour, ways=None):
        def open_with(way, request):
            self.tried.append(way)
            self.requests.append(request)
            result = behaviour(way)
            if isinstance(result, Exception):
                raise result
            return result

        return remote.http("GET", "https://relay.example/c/x/state", ways=lambda: ways or [self.PROXY, self.OFFICE, self.STRAIGHT],
                           open_with=open_with)

    def test_the_first_way_is_used_when_it_works(self):
        self.assertEqual(self.ask(lambda way: (200, b"sealed")), (200, b"sealed"))
        self.assertEqual(self.tried, [self.PROXY])

    def test_a_dead_proxy_is_passed_over_and_not_tried_first_again(self):
        refused = ConnectionRefusedError("nothing listens there")
        behaviour = lambda way: refused if way == self.PROXY else (200, b"sealed")  # noqa: E731
        self.assertEqual(self.ask(behaviour), (200, b"sealed"))
        self.assertEqual(self.tried, [self.PROXY, self.OFFICE])
        self.tried.clear()
        self.assertEqual(self.ask(behaviour), (200, b"sealed"))
        self.assertEqual(self.tried, [self.OFFICE])  # the one that worked goes first now

    def test_when_the_remembered_way_stops_working_the_others_are_tried(self):
        remote._good_way[0] = 1
        behaviour = lambda way: (200, b"sealed") if way == self.STRAIGHT else OSError("unknown host")  # noqa: E731
        self.assertEqual(self.ask(behaviour), (200, b"sealed"))
        self.assertEqual(self.tried, [self.OFFICE, self.PROXY, self.STRAIGHT])
        self.assertEqual(remote._good_way[0], 2)

    def test_a_proxy_that_wants_a_password_counts_as_no_way(self):
        import urllib.error

        wants_password = urllib.error.HTTPError("https://relay.example", 407, "Proxy Authentication Required", None, None)
        behaviour = lambda way: wants_password if way == self.OFFICE else OSError("refused") if way == self.PROXY else (200, b"ok")  # noqa: E731
        self.assertEqual(self.ask(behaviour), (200, b"ok"))
        self.assertEqual(len(self.tried), 3)

    def test_what_the_relay_refuses_is_an_answer_not_a_reason_to_go_another_way(self):
        import urllib.error

        refused = urllib.error.HTTPError("https://relay.example", 403, "Forbidden", None, None)
        self.assertEqual(self.ask(lambda way: refused), (403, b""))
        self.assertEqual(self.tried, [self.PROXY])

    def test_no_way_at_all_is_an_error_the_caller_reports(self):
        with self.assertRaises(OSError):
            self.ask(lambda way: OSError("refused"))
        self.assertEqual(len(self.tried), 3)

    def test_every_way_gets_a_request_of_its_own(self):
        # urllib writes the proxy into the request it sends; a second way must not inherit the first one's
        with self.assertRaises(OSError):
            self.ask(lambda way: OSError("refused"))
        self.assertEqual(len({id(request) for request in self.requests}), 3)
        self.assertTrue(all(request.host == "relay.example" for request in self.requests))

    def test_the_ways_are_different_from_each_other_and_end_with_straight_out(self):
        ways = remote.ways_out()
        self.assertEqual(ways[-1], {})
        self.assertEqual(len(ways), len({json.dumps(way, sort_keys=True) for way in ways}))
        self.assertTrue(all(set(way) <= {"http", "https"} for way in ways))


class KeptOpener(unittest.TestCase):
    """Titip asks the relay every few seconds. Reading Windows' certificates for each call kept a processor busy."""

    def setUp(self):
        kept = dict(remote._openers)
        remote._openers.clear()
        self.addCleanup(remote._openers.update, kept)
        self.addCleanup(remote._openers.clear)
        self.contexts = []
        real = remote.ssl.create_default_context
        patch = mock.patch.object(remote.ssl, "create_default_context", side_effect=lambda: self.contexts.append(1) or real())
        patch.start()
        self.addCleanup(patch.stop)

    def serve(self):
        """A local server that answers every GET with the path it was asked for."""
        asked = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append(self.path)
                body = self.path.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", asked

    def test_the_certificates_are_read_once_however_often_the_relay_is_asked(self):
        address, asked = self.serve()
        for number in range(6):
            status, body = remote._open({}, urllib.request.Request(f"{address}/c/x/asks?{number}"))
            self.assertEqual((status, body), (200, f"/c/x/asks?{number}".encode()))
        self.assertEqual((len(asked), len(self.contexts)), (6, 1))

    def test_each_way_out_keeps_its_own_and_still_goes_through_its_proxy(self):
        address, asked = self.serve()  # stands in for a proxy: it is handed the whole address
        through_proxy = {"http": address}
        self.assertEqual(remote._open(through_proxy, urllib.request.Request("http://relay.example/c/x/state"))[0], 200)
        self.assertEqual(remote._open({}, urllib.request.Request(f"{address}/straight"))[0], 200)
        self.assertEqual(remote._open(through_proxy, urllib.request.Request("http://relay.example/c/x/asks"))[0], 200)
        self.assertEqual(asked, ["http://relay.example/c/x/state", "/straight", "http://relay.example/c/x/asks"])
        self.assertEqual((len(remote._openers), len(self.contexts)), (2, 2))

    def test_threads_that_ask_at_the_same_moment_share_one(self):
        found = []
        threads = [threading.Thread(target=lambda: found.append(remote._opener({}))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((len({id(opener) for opener in found}), len(self.contexts)), (1, 1))


class FakeConnection:
    """What _Kept needs of a connection: request, getresponse (status, will_close, read) and close."""

    def __init__(self, status=200, will_close=False, fails=()):
        self.status, self.will_close = status, will_close
        self.fails = list(fails)  # what the next requests raise instead of answering
        self.asked, self.closed = [], False

    def request(self, method, selector, body=None, headers=None):
        if self.fails:
            raise self.fails.pop(0)
        self.asked.append((method, selector))

    def getresponse(self):
        return self

    def read(self, amount):
        return b"sealed"

    def close(self):
        self.closed = True


class KeptConnections(unittest.TestCase):
    """Through an office proxy a new connection takes two seconds, so the one to the relay stays open."""

    def setUp(self):
        self.now = 100.0
        self.made = []
        self.next = []  # connections handed out before plain working ones
        self.kept = remote._Kept(connect=self.connect, clock=lambda: self.now)

    def connect(self, way, host, port):
        connection = self.next.pop(0) if self.next else FakeConnection()
        connection.where = (host, port)
        self.made.append(connection)
        return connection

    def call(self, way=None, path="/c/x/asks/take"):
        return self.kept.open(way or {}, urllib.request.Request("https://relay.example" + path, data=b"", method="POST"))

    def test_the_next_call_goes_over_the_connection_that_is_already_open(self):
        self.assertEqual(self.call(), (200, b"sealed"))
        self.now += remote.KEEP_IDLE_S - 1
        self.assertEqual(self.call(path="/c/x/answer/1"), (200, b"sealed"))
        self.assertEqual(len(self.made), 1)
        self.assertEqual(self.made[0].where, ("relay.example", 443))
        self.assertEqual(self.made[0].asked, [("POST", "/c/x/asks/take"), ("POST", "/c/x/answer/1")])

    def test_one_that_was_silent_too_long_is_closed_and_not_used_again(self):
        self.call()
        self.now += remote.KEEP_IDLE_S
        self.call()
        self.assertEqual(len(self.made), 2)
        self.assertTrue(self.made[0].closed)
        self.assertFalse(self.made[1].closed)

    def test_a_kept_one_that_went_dead_costs_one_more_try_on_a_new_one(self):
        self.call()
        self.made[0].fails = [ConnectionResetError("the proxy dropped it")]
        self.assertEqual(self.call(), (200, b"sealed"))
        self.assertEqual(len(self.made), 2)
        self.assertTrue(self.made[0].closed)
        self.assertEqual(self.made[1].asked, [("POST", "/c/x/asks/take")])

    def test_a_new_one_that_fails_is_an_error_for_the_next_way_out_and_is_not_tried_again(self):
        for failure in (TimeoutError("no answer"), remote.http_client.RemoteDisconnected("closed")):
            self.made.clear()
            self.next = [FakeConnection(fails=[failure]), FakeConnection()]
            with self.assertRaises(OSError):
                self.call()
            self.assertEqual(len(self.made), 1)
            self.assertTrue(self.made[0].closed)

    def test_a_proxy_that_wants_a_password_is_an_error_not_an_answer(self):
        self.next = [FakeConnection(status=407, will_close=True)]
        with self.assertRaises(OSError):
            self.call()
        self.assertTrue(self.made[0].closed)

    def test_one_the_other_side_closes_is_not_kept_and_each_way_out_has_its_own(self):
        self.next = [FakeConnection(will_close=True)]
        self.call()
        self.call()
        self.assertEqual(len(self.made), 2)
        self.assertTrue(self.made[0].closed)
        self.call(way={"https": "http://proxy.office:80"})
        self.assertEqual(len(self.made), 3, "a call through the proxy does not use the straight connection")
        self.kept.close()
        self.assertTrue(all(connection.closed for connection in self.made))

    def test_what_the_relay_refuses_is_still_an_answer(self):
        self.next = [FakeConnection(status=403)]
        self.assertEqual(self.call(), (403, b"sealed"))
        self.call()
        self.assertEqual(len(self.made), 1)


if __name__ == "__main__":
    unittest.main()
