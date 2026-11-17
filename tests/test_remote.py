"""The link for any network: what leaves the PC is sealed, and only fresh, known orders are carried out."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
