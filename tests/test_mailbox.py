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

    def test_of_a_suggested_name_with_address_only_the_address_is_kept(self):
        typed = "Priya Nair <priya@x.com>; b@x.com, Priscilla Tan <tan@x.com>\nRina <rina@x.com>; "
        self.assertEqual(split_addresses(typed), ["priya@x.com", "b@x.com", "tan@x.com", "rina@x.com"])
        self.assertEqual(split_addresses(["Jonas Weber <jonas@x.com>", " c@x.com ", "", None]), ["jonas@x.com", "c@x.com"])
        self.assertEqual(split_addresses("Tomas"), ["Tomas"])  # a bare name is still Outlook's to resolve
        self.assertEqual(split_addresses(" ; <>; "), [])
        # the phone hands over a list, one entry for each person: a comma inside a name stays in the name
        self.assertEqual(split_addresses(["Okonkwo, Chidi", "a@x.com"]), ["Okonkwo, Chidi", "a@x.com"])
        request = clean_send_request({"mode": "new", "to": "Priya Nair <priya@x.com>; ", "cc": "Rina <rina@x.com>", "body": "Hi"})
        self.assertEqual((request["to"], request["cc"]), (["priya@x.com"], ["rina@x.com"]))

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

    def test_a_send_goes_ahead_of_everything_that_waits(self):
        from titip.mailbox import _ComThread

        thread = _ComThread()
        gate = threading.Event()
        order = []
        callers = [threading.Thread(target=lambda: thread.call(gate.wait, 5, timeout=5))]  # Outlook is busy with this one
        callers[0].start()
        time.sleep(0.1)
        for name, first in (("read ahead 1", False), ("read ahead 2", False), ("send", True), ("read ahead 3", False)):
            callers.append(threading.Thread(target=lambda name=name, first=first: thread.call(order.append, name, timeout=5, first=first)))
            callers[-1].start()
            time.sleep(0.05)  # they arrive in this order
        gate.set()
        for caller in callers:
            caller.join(5)
        self.assertEqual(order, ["send", "read ahead 1", "read ahead 2", "read ahead 3"])

    def test_the_send_is_the_call_that_goes_first(self):
        asked = []
        box = OutlookMailbox(read_ahead=False)
        box._do = lambda fn, *args, timeout=20, first=False: asked.append((fn.__name__, first)) or {"ok": True, "counts": {}}
        box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        box.state()
        self.assertEqual(asked, [("_send", True), ("_state", False)])

    def test_a_send_that_may_have_gone_out_warns_before_a_second_try(self):
        box = OutlookMailbox()

        def busy(fn, *args, timeout=20, first=False):
            raise OutlookBusy(started=True)

        box._do = busy
        with self.assertRaises(MailError) as caught:
            box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        self.assertIn("before you send it again", str(caught.exception))
        box._do = lambda fn, *args, timeout=20, first=False: (_ for _ in ()).throw(OutlookBusy(started=False))
        with self.assertRaises(MailError) as caught:
            box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        self.assertIn("nothing was sent", str(caught.exception))

    def test_pc_page_gets_the_last_known_state_while_outlook_is_busy(self):
        box = OutlookMailbox()
        box._do = lambda fn, *args, timeout=20, first=False: {"ok": True, "counts": {"inbox": 3}}
        self.assertEqual(box.quick_state()["counts"]["inbox"], 3)
        box._state_cache = (time.monotonic() - 3600, box._state_cache[1])  # long ago, also on a PC that just started

        def busy(fn, *args, timeout=20, first=False):
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

        def silent(fn, *args, timeout=20, first=False):
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

        def closed(fn, *args, timeout=20, first=False):
            raise MailError("Outlook on your PC did not answer. Open Outlook and try again.")

        box._do = closed
        state = box.quick_state()
        self.assertFalse(state["ok"])
        self.assertNotIn("busy", state)
        self.assertIn("Open Outlook", state["error"])

    def test_the_counts_are_not_asked_for_again_within_minutes(self):
        # On the build PC one count of every folder kept Outlook for 35 to 40 s, and its window stalled.
        box = OutlookMailbox()
        asked = []

        def count(fn, *args, timeout=20, first=False):
            asked.append(timeout)
            return {"ok": True, "counts": {"inbox": len(asked)}}

        box._do = count
        self.assertEqual(box.state()["counts"]["inbox"], 1)
        box._state_cache = (time.monotonic() - 120, box._state_cache[1])  # two minutes later
        self.assertEqual(box.state()["counts"]["inbox"], 1)
        self.assertEqual(box.quick_state()["counts"]["inbox"], 1)
        self.assertNotIn("busy", box.quick_state())
        self.assertEqual(len(asked), 1)
        box._state_cache = (time.monotonic() - 3600, box._state_cache[1])  # an hour later
        self.assertEqual(box.state()["counts"]["inbox"], 2)

    def test_a_send_makes_the_next_counts_new(self):
        box = OutlookMailbox()
        box._do = lambda fn, *args, timeout=20, first=False: {"ok": True, "counts": {"drafts": 1}}
        box.state()
        box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        self.assertIsNone(box._state_cache)

    def test_whose_mailbox_it_is_is_asked_once_per_connection(self):
        asked = []

        class FakeAccounts:
            def Item(self, index):
                asked.append("address")
                return type("Account", (), {"SmtpAddress": "me@example.com"})()

        class Namespace:
            Accounts = FakeAccounts()

            @property
            def CurrentUser(self):
                asked.append("name")
                return type("User", (), {"Name": "Me"})()

        box = OutlookMailbox()
        box._ns = Namespace()
        box._mail_folders = lambda name: []
        first, second = box._state(), box._state()
        self.assertEqual((first["account"], first["name"], first["counts"]["inbox"]), ("me@example.com", "Me", 0))
        self.assertEqual(second, first)
        self.assertEqual(asked, ["address", "name"])

    def test_an_address_outlook_could_not_give_is_asked_for_again(self):
        answers = [RuntimeError("not ready"), "me@example.com"]

        class FakeAccounts:
            def Item(self, index):
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return type("Account", (), {"SmtpAddress": answer})()

        class Namespace:
            Accounts = FakeAccounts()
            CurrentUser = type("User", (), {"Name": "Me"})()

        box = OutlookMailbox()
        box._ns = Namespace()
        box._mail_folders = lambda name: []
        self.assertEqual(box._state()["account"], "")
        self.assertEqual(box._state()["account"], "me@example.com")

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


class FakeOutlook:
    """Stands in for OutlookMailbox._do: answers lists and messages, or stays silent while `busy` is set."""

    def __init__(self, drafts=()):
        self.free = threading.Event()
        self.free.set()
        self.asked = []
        self.drafts = set(drafts)
        self.new = ()  # numbers of messages that arrived since
        self.closed = False

    def silent(self, yes=True):
        (self.free.clear if yes else self.free.set)()

    def rows(self, folder, further=False):
        """The first page, or with `further` what an older page or a search shows."""
        numbers = (4, 3) if further else (*self.new, 2, 1)
        return [{"id": f"s1.{folder.encode().hex().upper():0<16}{n}", "subject": f"{folder} {n}", "from": "Rina",
                 "time": f"2026-11-17T10:0{n}:00Z"} for n in numbers]

    def __call__(self, fn, *args, timeout=20, first=False):
        self.asked.append((fn.__name__, args[0] if args else None))
        if fn.__name__ == "_send" and not self.free.is_set():
            raise OutlookBusy(started=False)
        if not self.free.wait(timeout):
            raise OutlookBusy()
        if self.closed:
            raise MailError("Outlook on your PC did not answer. Open Outlook and try again.")
        if fn.__name__ == "_list":
            return {"folder": args[0], "mail": self.rows(args[0], further=bool(args[2] or args[3])), "more": False}
        if fn.__name__ == "_get":
            return {"id": args[0], "subject": "S", "time": "2026-11-17T10:02:00Z", "draft": args[0] in self.drafts,
                    "body": "text " + str(len(self.asked)), "html": ""}
        return {"ok": True, "sent": True}

    def count(self, name):
        return sum(1 for asked in self.asked if asked[0] == name)


def box_with(outlook, read_ahead=False):
    box = OutlookMailbox(read_ahead=read_ahead)
    box._do = outlook
    return box


def wait_until(condition, seconds=3):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        time.sleep(0.01)
    return condition()


class CopiesInMemory(unittest.TestCase):
    """A tap on the phone must not wait for Outlook when Titip already knows the answer."""

    def setUp(self):
        from titip import mailbox

        self.saved = {name: getattr(mailbox, name) for name in ("QUICK_SECONDS", "READ_SECONDS", "ASK_SECONDS", "WARM_PAUSE")}
        mailbox.QUICK_SECONDS, mailbox.READ_SECONDS, mailbox.ASK_SECONDS, mailbox.WARM_PAUSE = 0.1, 0.2, 5, 0
        self.addCleanup(lambda: [setattr(mailbox, name, value) for name, value in self.saved.items()])

    def test_a_message_read_before_opens_at_once_while_outlook_is_silent(self):
        outlook = FakeOutlook()
        box = box_with(outlook)
        first = box.get("s1.ABCDEF0123456789")
        outlook.silent()
        started = time.monotonic()
        again = box.get("s1.ABCDEF0123456789")
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(again, first)
        self.assertEqual(outlook.count("_get"), 1, "a received message does not change: Outlook is not asked again")

    def test_tries_share_one_question_and_its_late_answer_is_kept(self):
        outlook = FakeOutlook()
        box = box_with(outlook)
        outlook.silent()
        for _ in range(3):  # the phone asking again and again
            with self.assertRaises(OutlookBusy):
                box.get("s1.ABCDEF0123456789")
        self.assertEqual(outlook.count("_get"), 1, "one question in Outlook's queue, not one per try")
        outlook.silent(False)
        self.assertTrue(wait_until(lambda: not box._asking))
        outlook.silent()
        self.assertEqual(box.get("s1.ABCDEF0123456789")["subject"], "S")  # nobody was waiting when it arrived
        self.assertEqual(outlook.count("_get"), 1)

    def test_first_page_comes_from_the_copy_marked_saved_when_outlook_is_busy(self):
        outlook = FakeOutlook()
        box = box_with(outlook)
        fresh = box.list("inbox")
        self.assertNotIn("saved", fresh)
        self.assertIs(box.list("inbox"), fresh)
        self.assertEqual(outlook.count("_list"), 1, "a list a few seconds old is not asked for again")
        key = ("inbox", 40)
        box._lists[key] = (0.0, "2026-11-17T10:03:00Z", box._lists[key][2])  # long ago
        outlook.silent()
        started = time.monotonic()
        old = box.list("inbox")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual((old["saved"], old["savedAt"], old["mail"]), (True, "2026-11-17T10:03:00Z", fresh["mail"]))
        outlook.silent(False)
        self.assertTrue(wait_until(lambda: not box._asking))
        self.assertNotIn("saved", box.list("inbox"))

    def test_without_a_copy_the_phone_is_told_busy_and_searches_always_ask_outlook(self):
        outlook = FakeOutlook()
        box = box_with(outlook)
        outlook.silent()
        with self.assertRaises(OutlookBusy):
            box.list("sent")
        outlook.silent(False)
        self.assertTrue(wait_until(lambda: not box._asking))
        box.list("sent", query="gasket")
        box.list("sent", query="gasket")
        box.list("sent", offset=40)
        self.assertEqual(outlook.count("_list"), 4)
        with self.assertRaises(MailError):
            box.list("../secrets")

    def test_a_draft_is_asked_for_again_and_the_copy_is_only_a_fallback(self):
        draft = "s1.DDDDDD0123456789"
        outlook = FakeOutlook(drafts=[draft])
        box = box_with(outlook)
        first = box.get(draft)
        second = box.get(draft)
        self.assertNotEqual(first["body"], second["body"], "a draft may have changed on the PC")
        outlook.silent()
        kept = box.get(draft)
        self.assertEqual((kept["saved"], kept["body"]), (True, second["body"]))

    def test_sending_is_asked_once_and_forgets_the_lists(self):
        outlook = FakeOutlook()
        box = box_with(outlook)
        box.list("sent")
        box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        self.assertEqual(box._lists, {})
        self.assertEqual(outlook.count("_send"), 1)
        outlook.silent()
        with self.assertRaises(MailError) as caught:
            box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        self.assertNotIsInstance(caught.exception, OutlookBusy)  # the phone repeats only what is marked busy
        self.assertEqual(outlook.count("_send"), 2, "a send is never shared, kept or repeated")
        self.assertEqual(box._asking, {})

    def test_memory_stays_bounded(self):
        from titip import mailbox

        outlook = FakeOutlook()
        box = box_with(outlook)
        for n in range(mailbox.KEEP_MESSAGES + 5):
            box.get(f"s1.{n:016X}")
        self.assertEqual(len(box._messages), mailbox.KEEP_MESSAGES)
        self.assertNotIn("s1." + "0" * 16, box._messages)  # the oldest went first
        self.assertEqual(box._kept_characters, sum(len(m["body"]) for m in box._messages.values()))

    def test_messages_are_read_ahead_only_once_a_phone_asks(self):
        outlook = FakeOutlook()
        box = box_with(outlook, read_ahead=True)
        box.quick_state(wait=0.2)  # the page on the PC
        self.assertIsNone(box._warmer, "Titip does not read mail before a phone asks")
        rows = box.list("inbox")["mail"]
        wanted = [row["id"] for folder in ("inbox", "sent", "drafts") for row in outlook.rows(folder)]
        self.assertTrue(wait_until(lambda: all(mail_id in box._messages for mail_id in wanted)))
        outlook.silent()
        started = time.monotonic()
        self.assertEqual(box.get(rows[1]["id"])["id"], rows[1]["id"])
        self.assertLess(time.monotonic() - started, 0.05, "a message nobody opened yet is already there")
        box._phone_at = -1e9  # the phone has been away for a long time: reading ahead ends by itself

    def test_what_the_phone_was_shown_further_down_is_read_ahead_too(self):
        outlook = FakeOutlook()
        box = box_with(outlook, read_ahead=True)
        found = box.list("inbox", query="gasket")["mail"]
        older = box.list("sent", offset=40)["mail"]
        wanted = [row["id"] for row in found + older]
        self.assertTrue(wait_until(lambda: all(mail_id in box._messages for mail_id in wanted) and not box._wanted))
        outlook.silent()
        started = time.monotonic()
        self.assertEqual(box.get(found[0]["id"])["id"], found[0]["id"])
        self.assertLess(time.monotonic() - started, 0.05, "a search result opens from memory")
        box._phone_at = -1e9

    def test_reading_ahead_goes_on_slowly_after_the_phone_is_put_down_and_never_reopens_outlook(self):
        from titip import mailbox

        slow = mailbox.WARM_LONG_EVERY
        mailbox.WARM_LONG_EVERY = 0.02
        self.addCleanup(lambda: setattr(mailbox, "WARM_LONG_EVERY", slow))
        outlook = FakeOutlook()
        box = box_with(outlook, read_ahead=True)
        box.list("inbox")
        self.assertTrue(wait_until(lambda: len(box._messages) == 6))
        box._phone_at = time.monotonic() - mailbox.WARM_SECONDS - 1  # the phone was put down ten minutes ago
        box._nudge.set()
        outlook.new = (5,)  # and then a mail arrives
        arrived = outlook.rows("inbox")[0]["id"]
        self.assertTrue(wait_until(lambda: arrived in box._messages), "new mail is read before the phone is picked up")
        outlook.closed = True
        self.assertTrue(wait_until(lambda: not box._warmer.is_alive()), "the slow rounds end when Outlook is closed")
        asked = len(outlook.asked)
        time.sleep(0.1)
        self.assertEqual(len(outlook.asked), asked, "a closed Outlook is not asked again until a phone asks")
        outlook.closed = False
        outlook.silent()
        self.assertEqual(box.get(arrived)["id"], arrived)


class FakeAccessor:
    def __init__(self, address):
        self.address = address

    def GetProperty(self, name):
        if self.address is None:
            raise RuntimeError("Outlook does not have it")
        return self.address


class FakePerson:
    """A recipient as Outlook hands it over: a name, an internal address, and the internet address on request."""

    def __init__(self, name, smtp, address="/o=Office/cn=Recipients/cn=x"):
        self.Name, self.Address = name, address
        self.PropertyAccessor = FakeAccessor(smtp)


class FakeMessage:
    def __init__(self, sender, *people):
        self.SenderName, self.SenderEmailAddress = sender
        self.PropertyAccessor = FakeAccessor(self.SenderEmailAddress)
        self.people = list(people)
        self.Recipients = self

    Count = property(lambda self: len(self.people))

    def Item(self, index):
        return self.people[index - 1]


class People(unittest.TestCase):
    """Typing a name in To or Cc suggests the people of your own mail, the ones you write to first."""

    ME = ("me@example.com", "Me")

    def box(self):
        box = OutlookMailbox(read_ahead=False)
        box._who = self.ME
        return box

    def names(self, box):
        return [(person["name"], person["address"]) for person in box.people()["people"]]

    def test_whoever_you_write_to_most_comes_first_and_the_rest_by_name(self):
        box = self.box()
        box.remember_people([("Rina Wijaya", "rina@example.com", 1), ("Tomas Berg", "Tomas@Example.com", 3), ("Anna Okafor", "anna@example.com", 1)])
        box.remember_people([("", "tomas@example.com", 2), ("Rina W.", "rina@example.com", 1)])
        self.assertEqual(self.names(box), [("Tomas Berg", "Tomas@Example.com"), ("Rina Wijaya", "rina@example.com"), ("Anna Okafor", "anna@example.com")])
        self.assertEqual(box._people["tomas@example.com"][2], 5, "one person however the address is written")

    def test_only_real_addresses_are_kept_and_never_your_own(self):
        box = self.box()
        box.remember_people([
            ("Internal", "/o=Office/cn=Recipients/cn=x", 3), ("Nobody", "", 3), ("Two", "a@x.com; b@x.com", 3), ("None", None, 3),
            ("Me", "ME@example.com", 9), ("'priya@example.com'", "priya@example.com", 1), ('  "Jonas   Weber" ', " jonas@example.com ", 1),
        ])
        self.assertEqual(self.names(box), [("Jonas Weber", "jonas@example.com"), ("", "priya@example.com")])

    def test_the_list_stays_bounded(self):
        from titip import mailbox

        saved = mailbox.PEOPLE_KEEP, mailbox.PEOPLE_SHOWN
        mailbox.PEOPLE_KEEP, mailbox.PEOPLE_SHOWN = 5, 3
        self.addCleanup(lambda: (setattr(mailbox, "PEOPLE_KEEP", saved[0]), setattr(mailbox, "PEOPLE_SHOWN", saved[1])))
        box = self.box()
        box.remember_people([(f"Person {n}", f"p{n}@example.com", n) for n in range(1, 9)])
        self.assertEqual(len(box._people), 5)
        self.assertEqual([name for name, _ in self.names(box)], ["Person 5", "Person 4", "Person 3"])

    def test_a_message_counts_once_and_who_you_wrote_to_counts_more_than_who_wrote_to_you(self):
        box = self.box()
        mine = FakeMessage(("Me", "me@example.com"), FakePerson("Tomas Berg", "tomas@example.com"), FakePerson("Anna Okafor", None, "anna@example.com"))
        theirs = FakeMessage(("Rina Wijaya", "rina@example.com"), FakePerson("Me", "me@example.com"), FakePerson("Jonas Weber", "jonas@example.com"))
        for _ in range(3):  # read ahead, opened on the phone, opened again
            box._count_people("s1.AA", mine, "Me", "me@example.com")
            box._count_people("s1.BB", theirs, "Rina Wijaya", "rina@example.com")
        weights = {address: person[2] for address, person in box._people.items()}
        self.assertEqual(weights, {"me@example.com": 3, "tomas@example.com": 3, "anna@example.com": 3, "rina@example.com": 2, "jonas@example.com": 1})
        self.assertEqual([name for name, _ in self.names(box)], ["Anna Okafor", "Tomas Berg", "Rina Wijaya", "Jonas Weber"])

    def test_a_message_outlook_cannot_list_the_people_of_still_opens(self):
        box = self.box()
        broken = type("Broken", (), {"Recipients": property(lambda self: 1 / 0)})()
        box._count_people("s1.CC", broken, "Rina", "rina@example.com")  # must not raise
        self.assertEqual(box.people(), {"people": []})

    def test_a_mail_to_a_whole_department_does_not_fill_the_list(self):
        from titip import mailbox

        box = self.box()
        crowd = FakeMessage(("Rina", "rina@example.com"), *(FakePerson(f"P{n}", f"p{n}@example.com") for n in range(mailbox.PEOPLE_RECIPIENTS + 25)))
        box._count_people("s1.DD", crowd, "Rina", "rina@example.com")
        self.assertEqual(len(box._people), mailbox.PEOPLE_RECIPIENTS + 1)

    def test_older_sent_mail_is_looked_at_a_few_a_round_newest_first_and_only_once(self):
        from titip import mailbox

        saved = mailbox.PEOPLE_PAUSE, mailbox.PEOPLE_PER_ROUND, dict(mailbox.WARM_MESSAGES)
        mailbox.PEOPLE_PAUSE, mailbox.PEOPLE_PER_ROUND = 0, 2
        mailbox.WARM_MESSAGES["sent"] = 1  # the first one is read ahead in full anyway
        self.addCleanup(lambda: (setattr(mailbox, "PEOPLE_PAUSE", saved[0]), setattr(mailbox, "PEOPLE_PER_ROUND", saved[1]),
                                 mailbox.WARM_MESSAGES.update(saved[2])))
        asked, busy = [], []

        def outlook(fn, *args, timeout=20, first=False):
            asked.append((fn.__name__, *args[:2]))
            if fn.__name__ == "_list":
                return {"mail": [{"id": f"s1.{n}"} for n in range(5)]}
            if busy:
                raise busy.pop()
            return None

        box = box_with(outlook)
        box._learn_people()
        self.assertEqual(asked, [("_list", "sent", mailbox.PEOPLE_SENT), ("_note_people", "s1.1"), ("_note_people", "s1.2")])
        busy.append(OutlookBusy())
        with self.assertRaises(OutlookBusy):
            box._learn_people()
        busy.append(MailError("deleted in the meantime"))
        box._learn_people()
        box._learn_people()
        box._learn_people()
        self.assertEqual([args[1] for args in asked[3:]], ["s1.3", "s1.3", "s1.4"], "a busy Outlook is asked again, a deleted message is not")
        self.assertEqual(sum(1 for args in asked if args[0] == "_list"), 1)

    def test_asking_for_people_never_waits_for_outlook(self):
        outlook = FakeOutlook()
        box = box_with(outlook)
        box._who = self.ME
        box.remember_people([("Rina Wijaya", "rina@example.com", 2)])
        outlook.silent()
        started = time.monotonic()
        self.assertEqual(box.people(), {"people": [{"name": "Rina Wijaya", "address": "rina@example.com"}]})
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(outlook.asked, [])


class ReadAheadAtStart(unittest.TestCase):
    """After a restart the copies are gone; the newest mail is read again before the phone asks."""

    def setUp(self):
        from titip import mailbox

        self.saved = {name: getattr(mailbox, name) for name in ("QUICK_SECONDS", "READ_SECONDS", "ASK_SECONDS", "WARM_PAUSE")}
        mailbox.QUICK_SECONDS, mailbox.READ_SECONDS, mailbox.ASK_SECONDS, mailbox.WARM_PAUSE = 0.1, 0.2, 5, 0
        self.addCleanup(lambda: [setattr(mailbox, name, value) for name, value in self.saved.items()])

    def test_with_outlook_open_the_newest_mail_is_ready_before_a_phone_asks(self):
        outlook = FakeOutlook()
        box = box_with(outlook, read_ahead=True)
        self.assertTrue(box.read_ahead_now(running=lambda: True))
        wanted = [row["id"] for folder in ("inbox", "sent", "drafts") for row in outlook.rows(folder)]
        self.assertTrue(wait_until(lambda: all(mail_id in box._messages for mail_id in wanted)))
        outlook.closed = True  # these are the slow rounds: they end when Outlook is closed
        box._nudge.set()
        self.assertTrue(wait_until(lambda: not box._warmer.is_alive()))

    def test_outlook_is_never_started_for_it(self):
        outlook = FakeOutlook()
        box = box_with(outlook, read_ahead=True)
        self.assertFalse(box.read_ahead_now(running=lambda: False))
        self.assertIsNone(box._warmer)
        self.assertEqual(outlook.asked, [])

    def test_not_when_reading_ahead_is_off_or_a_phone_already_asked(self):
        asked = []
        running = lambda: asked.append(1) or True  # noqa: E731
        self.assertFalse(box_with(FakeOutlook(), read_ahead=False).read_ahead_now(running=running))
        box = box_with(FakeOutlook(), read_ahead=True)
        box.list("inbox")
        at = box._phone_at
        self.assertFalse(box.read_ahead_now(running=running))
        self.assertEqual((box._phone_at, asked), (at, []), "the phone in use is not mistaken for one put down")
        box._phone_at = -1e9


class FakeBook:
    """Outlook's address book: sorted by name, and it can only jump to the first name at or after a text."""

    def __init__(self, *entries, alias=None):
        self.entries = sorted((FakePerson(name, smtp) for name, smtp in entries), key=lambda entry: entry.Name.lower())
        self.alias = alias or {}
        self.jumps = []
        self.AddressEntries = self

    def Item(self, seek):
        self.jumps.append(seek)
        for entry in self.entries:
            if entry.Name.lower() >= seek.lower():
                return entry
        raise RuntimeError("past the last name")

    def GetGlobalAddressList(self):
        return self

    def CreateRecipient(self, typed):
        book = self

        class Recipient:
            Name = typed
            AddressEntry = None

            def Resolve(self):
                found = book.alias.get(typed.lower())
                if found is None:
                    return False
                self.Name, self.AddressEntry = found[0], FakePerson(*found)
                return True

        return Recipient()


class AddressBook(unittest.TestCase):
    """A name typed in To or Cc is also looked up in Outlook's own address book, the company directory."""

    def found(self, book, typed):
        box = OutlookMailbox(read_ahead=False)
        box._ns = book
        return [(person["name"], person["address"]) for person in box._lookup(typed)]

    def test_the_names_that_start_with_what_was_typed_each_with_the_address(self):
        book = FakeBook(
            ("Priya Nair", "priya@x.example"), ("Pritam Das", "pritam@x.example"), ("priyanka rao", "rao@x.example"),
            ("Quentin Blake", "quentin@x.example"), ("Hana Sato", "hana@x.example"),
        )
        self.assertEqual(self.found(book, "PRI"), [
            ("Pritam Das", "pritam@x.example"), ("Priya Nair", "priya@x.example"), ("priyanka rao", "rao@x.example"),
        ])
        self.assertEqual(self.found(book, "hana s"), [("Hana Sato", "hana@x.example")])
        self.assertEqual(self.found(book, "zed"), [], "nothing starts that way, also past the last name")

    def test_only_a_few_names_are_read_however_many_match(self):
        from titip import mailbox

        book = FakeBook(*((f"Lee {number:03d}", f"lee{number}@x.example") for number in range(200)))
        found = self.found(book, "lee")
        self.assertEqual([name for name, _ in found], [f"Lee {number:03d}" for number in range(mailbox.LOOKUP_ROWS)])
        self.assertLessEqual(len(book.jumps), mailbox.LOOKUP_ROWS, "one jump for each name, not a walk through the book")

    def test_an_entry_without_an_internet_address_is_left_out_and_the_rest_still_come(self):
        book = FakeBook(("Lab A", None), ("Lab B", "lab-b@x.example"), ("Lab C", "/o=Office/cn=lab-c"), ("Lab D", "lab-b@x.example"))
        self.assertEqual(self.found(book, "lab"), [("Lab B", "lab-b@x.example")])

    def test_a_short_name_that_outlook_can_match_to_one_person_is_found_too(self):
        book = FakeBook(("Rina Wijaya (R&D)", "rina@x.example"), alias={"rwijaya": ("Rina Wijaya (R&D)", "rina@x.example")})
        self.assertEqual(self.found(book, "rwijaya"), [("Rina Wijaya (R&D)", "rina@x.example")])
        self.assertEqual(self.found(book, "nobody"), [])
        self.assertEqual(self.found(book, "rw@"), [], "something with an @ is an address being typed, not a name")

    def test_an_account_without_a_directory_has_nothing_to_suggest(self):
        class NoBook:
            def GetGlobalAddressList(self):
                return None

        self.assertEqual(self.found(NoBook(), "pri"), [])

    def test_typing_never_waits_long_and_goes_ahead_of_the_reading_ahead(self):
        from titip import mailbox

        asked = []

        def outlook(fn, *args, timeout=20, first=False):
            asked.append((fn.__name__, args, timeout, first))
            if args == ("busy",):
                raise OutlookBusy()
            return [{"name": "Priya Nair", "address": "priya@x.example"}]

        box = box_with(outlook)
        self.assertEqual(box.lookup("  pri  "), {"people": [{"name": "Priya Nair", "address": "priya@x.example"}], "book": True})
        self.assertEqual(asked, [("_lookup", ("pri",), mailbox.LOOKUP_SECONDS, True)])
        self.assertEqual(box.lookup("busy"), {"people": [], "book": True, "busy": True}, "an empty answer, not a try-again")
        self.assertEqual(box.lookup("p"), {"people": [], "book": True})
        self.assertEqual(box.lookup(None), {"people": [], "book": True})
        self.assertEqual(len(asked), 2, "one letter is not looked up")
        box.lookup("x" * 500)
        self.assertEqual(len(asked[-1][1][0]), mailbox.LOOKUP_LONGEST)


class FakeNamespace:
    def __init__(self, items):
        self.items = items

    def GetItemFromID(self, entry_id, store_id):
        return self.items[(store_id, entry_id)]  # KeyError: Outlook does not have it under that id


class MovedMessages(unittest.TestCase):
    """A rule or archiving tool moves new mail into a data file, which changes its id."""

    OLD, NEW, OTHER = "live.AAAAAAAAAAAAAAAA", "arch.BBBBBBBBBBBBBBBB", "arch.CCCCCCCCCCCCCCCC"

    def box(self, rows):
        box = OutlookMailbox(read_ahead=False)
        box._stores = {"live": "LIVE", "arch": "ARCHIVE"}
        box._ns = FakeNamespace({("ARCHIVE", "BBBBBBBBBBBBBBBB"): "the moved message", ("ARCHIVE", "CCCCCCCCCCCCCCCC"): "another"})
        box._rows[self.OLD] = ("inbox", "2026-11-17T10:02:00Z", "Hinge sample B", "Rina")
        self.listed = []

        def newest(folder, limit, offset, query):
            self.listed.append(folder)
            return {"mail": rows}

        box._list = newest
        return box

    def row(self, mail_id, subject="Hinge sample B"):
        return {"id": mail_id, "subject": subject, "from": "Rina", "time": "2026-11-17T10:02:00Z"}

    def test_the_same_message_is_found_under_its_new_id(self):
        box = self.box([self.row(self.OTHER, "Something else"), self.row(self.NEW)])
        self.assertEqual(box._item(self.OLD), "the moved message")
        self.assertEqual(box._item(self.OLD), "the moved message")  # reply and attachments use the old id too
        self.assertEqual(self.listed, ["inbox"], "looked up once, then remembered")

    def test_no_guessing_between_look_alikes_or_for_an_unknown_message(self):
        box = self.box([self.row(self.NEW), self.row(self.OTHER)])  # two with the same time, subject and sender
        with self.assertRaises(MailError):
            box._item(self.OLD)
        box = self.box([self.row(self.NEW)])
        with self.assertRaises(MailError):
            box._item("live.EEEEEEEEEEEEEEEE")  # never listed: nothing to go by
        self.assertEqual(self.listed, [])
        box = self.box([])  # really deleted
        with self.assertRaises(MailError) as caught:
            box._item(self.OLD)
        self.assertIn("moved or deleted", str(caught.exception))


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

    def test_the_people_of_the_invented_mail_can_be_suggested(self):
        people = DemoMailbox().people()["people"]
        self.assertGreaterEqual(len(people), 8)
        self.assertTrue(all(person["name"] and "@" in person["address"] for person in people))
        self.assertTrue(all(person["address"].endswith(".example") for person in people), "nobody real")

    def test_the_invented_address_book_is_looked_up_by_the_start_of_a_name(self):
        box = DemoMailbox()
        found = box.lookup("PRI")
        self.assertTrue(found["book"])
        self.assertEqual([person["name"] for person in found["people"]], ["Priya Nair", "Pritam Das", "Priyanka Rao"])
        self.assertTrue(all(person["address"].endswith(".example") for person in box.book), "nobody real")
        self.assertEqual(box.lookup("p")["people"], [], "one letter is not looked up")
        self.assertEqual(box.lookup("nair")["people"], [], "only the start of the name, as in Outlook's book")
        known = {person["address"] for person in box.people()["people"]}
        self.assertTrue(any(person["address"] not in known for person in box.book), "people the mail does not hold")

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
