"""Mail logic without Outlook: request checks, HTML building, and the send path against a fake Outlook."""
import threading
import time
import unittest

from titip.mailbox import (
    DemoMailbox,
    MailError,
    OutlookBusy,
    OutlookMailbox,
    clean_send_request,
    insert_into_html,
    split_addresses,
    text_to_html,
)


class FakeRecipient:
    def __init__(self, name):
        self.Name = name
        self.Type = 1
        self.Resolved = "@" in name


class FakeRecipients(list):
    @property
    def Count(self):
        return len(self)

    def Add(self, name):
        recipient = FakeRecipient(name)
        self.append(recipient)
        return recipient

    def ResolveAll(self):
        return all(recipient.Resolved for recipient in self)


class FakeMail:
    def __init__(self, sent=False, recipients=()):
        self.Sent = sent
        self.Subject = ""
        self.HTMLBody = "<html><body lang=EN><p>signature</p></body></html>"
        self.Body = "signature"
        self.Recipients = FakeRecipients(FakeRecipient(name) for name in recipients)
        self.calls = []
        self._to = self._cc = ""

    def _set(self, kind, value):
        self.Recipients[:] = [r for r in self.Recipients if r.Type != kind]
        for name in filter(None, (part.strip() for part in value.split(";"))):
            self.Recipients.Add(name).Type = kind

    To = property(lambda self: self._to, lambda self, value: (setattr(self, "_to", value), self._set(1, value))[0])
    CC = property(lambda self: self._cc, lambda self, value: (setattr(self, "_cc", value), self._set(2, value))[0])


    def Send(self):
        self.calls.append("send")

    def Save(self):
        self.calls.append("save")

    def Close(self, how):
        self.calls.append(f"close{how}")


class FakeSource(FakeMail):
    def __init__(self, **kwargs):
        super().__init__(sent=True, **kwargs)
        self.made = None

    def Reply(self):
        self.made = FakeMail(recipients=["boss@example.com"])
        self.made.HTMLBody = "<html><body><p>signature</p><hr>quoted thread</body></html>"
        return self.made

    ReplyAll = Forward = Reply


class FakeApp:
    def __init__(self):
        self.created = []

    def CreateItem(self, kind):
        self.created.append(FakeMail())
        return self.created[-1]


def outlook_with(source=None):
    box = OutlookMailbox()
    box._app = FakeApp()
    box._item = lambda mail_id: source
    return box


class Helpers(unittest.TestCase):
    def test_typed_text_is_escaped(self):
        block = text_to_html("a <b> & c\nline 2")
        self.assertIn("a &lt;b&gt; &amp; c<br>line 2", block)
        self.assertNotIn("<b>", block)

    def test_text_goes_above_signature_and_thread(self):
        merged = insert_into_html("<html><body class=x><p>sig</p></body></html>", "<div>new</div>")
        self.assertLess(merged.index("<div>new</div>"), merged.index("<p>sig</p>"))
        self.assertTrue(merged.startswith("<html><body class=x>"))
        self.assertEqual(insert_into_html("", "<div>new</div>"), "<div>new</div>")

    def test_addresses_split_on_semicolon_comma_and_newline(self):
        self.assertEqual(split_addresses("a@x.com; b@x.com,c@x.com\n"), ["a@x.com", "b@x.com", "c@x.com"])

    def test_send_needs_recipient_and_text(self):
        with self.assertRaises(MailError):
            clean_send_request({"mode": "new", "to": "", "body": "hello"})
        with self.assertRaises(MailError):
            clean_send_request({"mode": "new", "to": "a@x.com", "body": "  "})
        with self.assertRaises(MailError):
            clean_send_request({"mode": "reply", "body": "hello"})  # no original message id
        with self.assertRaises(MailError):
            clean_send_request({"mode": "delete-everything", "body": "x"})
        self.assertEqual(clean_send_request({"mode": "reply", "id": "s.AB", "body": "ok"})["to"], [])
        self.assertTrue(clean_send_request({"mode": "new", "action": "draft"}))  # an empty draft may be saved


class SendThroughOutlook(unittest.TestCase):
    def send(self, box, **payload):
        return box._send(clean_send_request(payload))

    def test_new_mail_is_sent_once_with_text_above_what_outlook_put_there(self):
        box = outlook_with()
        result = self.send(box, mode="new", to="a@example.com; b@example.com", cc="c@example.com", subject="Hi", body="Hello <team>")
        mail = box._app.created[0]
        self.assertEqual(result, {"ok": True, "sent": True})
        self.assertEqual(mail.calls, ["send"])
        self.assertEqual(mail.Subject, "Hi")
        self.assertEqual([(r.Name, r.Type) for r in mail.Recipients], [("a@example.com", 1), ("b@example.com", 1), ("c@example.com", 2)])
        self.assertLess(mail.HTMLBody.index("Hello &lt;team&gt;"), mail.HTMLBody.index("signature"))

    def test_draft_is_saved_never_sent(self):
        box = outlook_with()
        result = self.send(box, mode="new", action="draft", to="a@example.com", body="later")
        self.assertEqual(result, {"ok": True, "saved": True})
        self.assertEqual(box._app.created[0].calls, ["save"])

    def test_reply_keeps_outlooks_recipients_and_thread_and_only_adds_cc(self):
        source = FakeSource()
        box = outlook_with(source)
        self.send(box, mode="reply", id="s.ABCDEF0123456789", cc="extra@example.com", body="Agreed.")
        reply = source.made
        self.assertEqual([(r.Name, r.Type) for r in reply.Recipients], [("boss@example.com", 1), ("extra@example.com", 2)])
        self.assertIn("quoted thread", reply.HTMLBody)
        self.assertLess(reply.HTMLBody.index("Agreed."), reply.HTMLBody.index("quoted thread"))
        self.assertEqual(reply.calls, ["send"])
        self.assertEqual(source.calls, [])  # the original message is never changed

    def test_unknown_recipient_stops_the_send_and_discards_the_new_mail(self):
        box = outlook_with()
        with self.assertRaises(MailError) as caught:
            self.send(box, mode="new", to="someone-unknown", body="x")
        self.assertIn("someone-unknown", str(caught.exception))
        self.assertEqual(box._app.created[0].calls, ["close1"])

    def test_only_a_real_draft_can_be_edited(self):
        box = outlook_with(FakeSource())  # an already-sent message
        with self.assertRaises(MailError):
            self.send(box, mode="draft", id="s.ABCDEF0123456789", to="a@example.com", body="x")
        draft = FakeMail(sent=False)
        box = outlook_with(draft)
        self.send(box, mode="draft", id="s.ABCDEF0123456789", to="a@example.com", subject="S", body="final")
        self.assertEqual(draft.calls, ["send"])
        self.assertIn("final", draft.HTMLBody)

    def test_draft_sent_unchanged_keeps_its_formatting(self):
        draft = FakeMail(sent=False)
        draft.HTMLBody = "<html><body><p>Dear <b>team</b></p><img src=cid:1></body></html>"
        draft.Body = "Dear team\r\n"
        box = outlook_with(draft)
        self.send(box, mode="draft", id="s.ABCDEF0123456789", to="a@example.com", body="Dear team\n")
        self.assertIn("<b>team</b>", draft.HTMLBody)
        self.assertEqual(draft.calls, ["send"])

    def test_failed_draft_edit_does_not_discard_the_users_draft(self):
        draft = FakeMail(sent=False)
        box = outlook_with(draft)
        with self.assertRaises(MailError):
            self.send(box, mode="draft", id="s.ABCDEF0123456789", to="nobody", body="x")
        self.assertEqual(draft.calls, [])

    def test_bad_ids_never_reach_outlook(self):
        box = OutlookMailbox()
        for bad in ("", "x", "store.not-hex", "store.../../etc"):
            with self.assertRaises(MailError):
                box._item(bad)


class BusyOutlook(unittest.TestCase):
    def test_a_call_that_gave_up_is_never_carried_out_later(self):
        from titip.mailbox import _ComThread

        thread = _ComThread()
        gate = threading.Event()
        ran = []
        blocker = threading.Thread(target=lambda: thread.call(gate.wait, 5, timeout=5))
        blocker.start()  # Outlook is "busy" with this one
        time.sleep(0.1)
        with self.assertRaises(OutlookBusy) as caught:
            thread.call(ran.append, "sent twice", timeout=0.2)
        self.assertFalse(caught.exception.started)
        gate.set()
        blocker.join()
        self.assertEqual(thread.call(lambda: "next call works"), "next call works")
        self.assertEqual(ran, [])

    def test_giving_up_on_a_call_already_running_says_so(self):
        from titip.mailbox import _ComThread

        thread = _ComThread()
        gate = threading.Event()
        with self.assertRaises(OutlookBusy) as caught:
            thread.call(gate.wait, 2, timeout=0.2)
        self.assertTrue(caught.exception.started)
        gate.set()

    def test_a_send_that_may_have_gone_out_warns_before_a_second_try(self):
        box = OutlookMailbox()

        def busy(fn, *args, timeout=20):
            raise OutlookBusy(started=True)

        box._do = busy
        with self.assertRaises(MailError) as caught:
            box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        self.assertIn("before you send it again", str(caught.exception))
        box._do = lambda fn, *args, timeout=20: (_ for _ in ()).throw(OutlookBusy(started=False))
        with self.assertRaises(MailError) as caught:
            box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        self.assertIn("nothing was sent", str(caught.exception))

    def test_pc_page_gets_the_last_known_state_while_outlook_is_busy(self):
        box = OutlookMailbox()
        box._do = lambda fn, *args, timeout=20: {"ok": True, "counts": {"inbox": 3}}
        self.assertEqual(box.quick_state()["counts"]["inbox"], 3)
        box._state_cache = (0.0, box._state_cache[1])  # long ago

        def busy(fn, *args, timeout=20):
            raise OutlookBusy()

        box._do = busy
        state = box.quick_state()
        self.assertTrue(state["busy"] and state["ok"])
        box._state_cache = None
        self.assertEqual(box.quick_state()["ok"], False)

    def test_pc_page_never_waits_for_a_silent_outlook(self):
        box = OutlookMailbox()
        gate = threading.Event()
        asked = []

        def silent(fn, *args, timeout=20):
            asked.append(timeout)
            gate.wait(5)
            return {"ok": True, "counts": {"inbox": 7}}

        box._do = silent
        started = time.monotonic()
        first = box.quick_state(wait=0.1)
        second = box.quick_state(wait=0.1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(first["busy"] and second["busy"])
        self.assertEqual(len(asked), 1, "one question to Outlook, not one per page refresh")
        gate.set()
        box._refresh.join(2)
        self.assertEqual(box.quick_state(wait=0.1)["counts"]["inbox"], 7)

    def test_pc_page_shows_a_real_error_not_busy(self):
        box = OutlookMailbox()

        def closed(fn, *args, timeout=20):
            raise MailError("Outlook on your PC did not answer. Open Outlook and try again.")

        box._do = closed
        state = box.quick_state()
        self.assertFalse(state["ok"])
        self.assertNotIn("busy", state)
        self.assertIn("Open Outlook", state["error"])

    def test_a_long_silent_outlook_is_not_asked_to_queue_more(self):
        from titip.mailbox import _ComThread

        thread = _ComThread()
        gate = threading.Event()
        ran = []
        threading.Thread(target=lambda: thread.call(gate.wait, 5, timeout=5), daemon=True).start()
        time.sleep(0.4)
        self.assertGreater(thread.stuck_for(), 0.3)
        started = time.monotonic()
        with self.assertRaises(OutlookBusy) as caught:
            thread.call(ran.append, "x", timeout=0.3)
        self.assertLess(time.monotonic() - started, 0.1, "told at once, not after another wait")
        self.assertFalse(caught.exception.started)
        gate.set()
        time.sleep(0.1)
        self.assertEqual(thread.stuck_for(), 0)
        self.assertEqual(ran, [])


class Demo(unittest.TestCase):
    def test_lists_are_newest_first_and_searchable(self):
        box = DemoMailbox()
        inbox = box.list("inbox")["mail"]
        self.assertGreaterEqual(len(inbox), 6)
        self.assertEqual([m["time"] for m in inbox], sorted((m["time"] for m in inbox), reverse=True))
        hits = box.list("inbox", query="gasket")["mail"]
        self.assertTrue(hits and all("gasket" in m["subject"].lower() for m in hits))
        with self.assertRaises(MailError):
            box.list("../secrets")

    def test_reply_lands_in_sent_and_draft_in_drafts(self):
        box = DemoMailbox()
        first = box.list("inbox")["mail"][0]
        before = box.state()["counts"]
        box.send({"mode": "reply", "id": first["id"], "body": "Run the rest."})
        box.send({"mode": "new", "action": "draft", "to": "a@example.com", "body": "tbd"})
        after = box.state()["counts"]
        self.assertEqual(after["sent"], before["sent"] + 1)
        self.assertEqual(after["drafts"], before["drafts"] + 1)
        newest = box.get(box.list("sent")["mail"][0]["id"])
        self.assertTrue(newest["subject"].startswith("RE: "))
        self.assertIn("Run the rest.", newest["body"])


if __name__ == "__main__":
    unittest.main()
