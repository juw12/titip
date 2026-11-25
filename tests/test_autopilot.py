"""The autopilot loop with an invented mailbox, an invented model and a clock we move by hand."""
import json
import tempfile
import unittest
from pathlib import Path

from titip import autopilot, brain
from titip.autopilot import Autopilot, SourceError


class Row:
    def __init__(self, key, text=""):
        self.key, self.text = key, text or f"unread, {key}"


class Box:
    """A mailbox that holds what the test puts in it and remembers what was sent."""

    address = "nadia@mail.example"

    def __init__(self):
        self.mail = {}
        self.sent = []
        self.opened = []
        self.closed = 0
        self.fail = ""
        self.rows = []
        self.last = ""
        self.unread_again = []

    def put(self, key, sender, address, subject, text):
        self.mail[key] = brain.Mail(self.address, sender, address, subject, text)

    def open(self, account, query):
        self.opened.append(query)
        if query == autopilot.UNREAD_QUERY:
            self.rows = [Row(key) for key in self.mail]
        else:
            self.rows = [Row(key) for key, mail in self.mail.items() if mail.address in query]
        return self.address

    def unread(self, any_state=False):
        return list(self.rows)

    def read(self, row, mailbox):
        self.last = row.key
        return self.mail[row.key]

    def keep_unread(self):
        self.unread_again.append(self.last)
        return True

    def reply(self, mail, text):
        if self.fail:
            raise SourceError(self.fail)
        self.sent.append((mail.address, text))

    def close(self):
        self.closed += 1


def answer(action="reply", reply="Thanks, noted.\n\nNadia", **more):
    return lambda prompt: json.dumps({"action": action, "reason": "because", "summary": "what it wants", "reply": reply, **more})


class Loop(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 1_800_000_000.0
        self.box = Box()
        self.pilot = self.make(answer())
        self.pilot.command("switch", on=True)
        self.pilot.check()  # the first look only counts what is there

    def make(self, ask):
        return Autopilot(Path(self.folder.name), self.box, ask, clock=lambda: self.now)

    def kinds(self):
        return [event["kind"] for event in self.pilot.state["events"]]

    def test_the_first_look_opens_nothing(self):
        box = Box()
        box.put("old", "Tomas", "tomas@lind.example", "Hello", "Hi there")
        with tempfile.TemporaryDirectory() as home:
            pilot = Autopilot(Path(home), box, answer(), clock=lambda: self.now)
            pilot.command("switch", on=True)
            pilot.check()
            self.assertEqual(box.sent, [])
            self.assertIn("already there", pilot.state["events"][-1]["text"])
            pilot.check()
            self.assertEqual(box.sent, [])  # still not opened on later looks
            box.put("new", "Tomas", "tomas@lind.example", "Again", "One more question")
            pilot.check()
            self.assertEqual(len(box.sent), 1)

    def test_a_safe_mail_is_answered_once_and_written_down(self):
        self.box.put("m1", "Tomas Lind", "tomas@lind.example", "Your template", "Is it free?")
        self.pilot.check()
        self.assertEqual(self.box.sent, [("tomas@lind.example", "Thanks, noted.\n\nNadia")])
        self.assertEqual(self.kinds()[-1], "sent")
        self.assertEqual(self.box.unread_again, [])  # an answered mail stays read
        self.pilot.check()
        self.assertEqual(len(self.box.sent), 1)
        self.assertEqual(self.pilot.snapshot()["sentToday"], 1)
        self.assertGreaterEqual(self.box.closed, 2)  # the window is always closed again

    def test_paused_means_nothing_is_read(self):
        self.pilot.command("switch", on=False)
        self.box.put("m1", "Tomas", "tomas@lind.example", "Hi", "Hello")
        opened = len(self.box.opened)
        self.pilot.check()
        self.assertEqual(len(self.box.opened), opened)

    def test_review_mode_keeps_every_answer(self):
        self.pilot.command("mode", mode="review")
        self.box.put("m1", "Tomas", "tomas@lind.example", "Hi", "Hello")
        self.pilot.check()
        self.assertEqual(self.box.sent, [])
        held = self.pilot.snapshot()["held"]
        self.assertEqual(held[0]["draft"], "Thanks, noted.\n\nNadia")
        self.assertIn("every answer first", held[0]["reason"])

    def test_risky_mail_waits_and_your_wording_is_what_gets_sent(self):
        self.box.put("m1", "Harbor Print", "billing@harbor.example", "Invoice", "Please transfer the payment to our bank account.")
        self.pilot.check()
        self.assertEqual(self.box.sent, [])
        item = self.pilot.snapshot()["held"][0]
        self.assertIn("money", item["reason"])
        self.assertEqual(self.box.unread_again, ["m1"])  # what waits for you still looks new in Gmail
        self.pilot.command("send", id=item["id"], text="I will look at it next week.\n\nNadia")
        self.assertTrue(self.pilot.state["held"][0]["sending"])
        self.pilot.send_held(item["id"], "I will look at it next week.\n\nNadia")
        self.assertEqual(self.box.sent, [("billing@harbor.example", "I will look at it next week.\n\nNadia")])
        self.assertEqual(self.pilot.snapshot()["held"], [])

    def test_a_waiting_answer_is_not_sent_to_a_different_sender(self):
        self.box.put("m1", "Harbor Print", "billing@harbor.example", "Invoice", "Please pay the fee by bank account transfer.")
        self.pilot.check()
        item = self.pilot.snapshot()["held"][0]
        self.box.mail["m1"].address = "someone@else.example"
        self.box.open = lambda account, query: (setattr(self.box, "rows", [Row("m1")]) or self.box.address)
        self.pilot.send_held(item["id"], "Hello")
        self.assertEqual(self.box.sent, [])
        self.assertIn("someone else", self.pilot.snapshot()["held"][0]["error"])

    def test_dropping_a_waiting_mail_sends_nothing(self):
        self.box.put("m1", "Harbor Print", "billing@harbor.example", "Invoice", "Please send your passport.")
        self.pilot.check()
        self.pilot.command("drop", id=self.pilot.snapshot()["held"][0]["id"])
        self.assertEqual((self.box.sent, self.pilot.snapshot()["held"]), ([], []))
        self.assertEqual(self.kinds()[-1], "dropped")

    def test_the_daily_limit_holds_the_rest(self):
        for number in range(autopilot.MAX_AUTO_PER_DAY + 2):
            self.box.put(f"m{number}", f"Person {number}", f"p{number}@mail.example", f"Question {number}", "Hello?")
        for _ in range(6):
            self.pilot.check()
        self.assertEqual(len(self.box.sent), autopilot.MAX_AUTO_PER_DAY)
        self.assertEqual(len(self.pilot.snapshot()["held"]), 2)
        self.now += 24 * 3600  # a new day, a new allowance
        self.box.put("next", "Late", "late@mail.example", "Tomorrow", "Hello?")
        self.pilot.check()
        self.assertEqual(len(self.box.sent), autopilot.MAX_AUTO_PER_DAY + 1)

    def test_one_conversation_gets_one_automatic_answer_per_half_day(self):
        self.box.put("m1", "Tomas", "tomas@lind.example", "Template", "Is it free?")
        self.pilot.check()
        self.box.put("m2", "Tomas", "tomas@lind.example", "Re: Template", "And for companies?")
        self.pilot.check()
        self.assertEqual(len(self.box.sent), 1)
        self.assertIn("already got", self.pilot.snapshot()["held"][0]["reason"])

    def test_at_most_three_mails_are_opened_per_look(self):
        for number in range(5):
            self.box.put(f"m{number}", f"P{number}", f"p{number}@mail.example", f"S{number}", "Hello?")
        self.pilot.check()
        self.assertEqual(len(self.box.sent), autopilot.MAX_PER_CHECK)

    def test_your_own_message_and_newsletters_are_left(self):
        self.box.put("m1", "Nadia", "nadia@mail.example", "Note to self", "Buy stamps")
        self.box.put("m2", "Shop", "no-reply@shop.example", "Receipt", "Thanks for your order")
        self.pilot.check()
        self.assertEqual(self.box.sent, [])
        self.assertEqual(self.kinds()[-2:], ["left", "left"])
        self.assertEqual(self.box.unread_again, ["m1", "m2"])

    def test_who_wrote_is_written_down_whatever_was_done_with_the_mail(self):
        self.box.put("m1", "Tomas Lind", "Tomas@Lind.example", "Your template", "Is it free?")
        self.box.put("m2", "Field Notes", "newsletter@fieldnotes.example", "Issue 48", "Paper sizes")
        self.box.put("m3", "Nadia", "nadia@mail.example", "Note to self", "Buy stamps")
        self.pilot.check()
        self.assertEqual(self.kinds()[-3:], ["sent", "left", "left"])
        wrote = {"tomas@lind.example": int(self.now), "newsletter@fieldnotes.example": int(self.now)}
        self.assertEqual(self.pilot.state["wrote"], wrote)  # an answered and a left mail, not your own note
        self.assertEqual(self.make(answer()).state["wrote"], wrote)  # still there after a restart

    def test_left_mail_stays_read_when_you_chose_so_and_what_waits_for_you_does_not(self):
        self.assertFalse(self.pilot.snapshot()["leftRead"])
        self.assertTrue(self.pilot.command("left", read=True)["leftRead"])
        self.box.put("m1", "Nadia", "nadia@mail.example", "Note to self", "Buy stamps")
        self.box.put("m2", "Shop", "no-reply@shop.example", "Receipt", "Thanks for your order")
        self.box.put("m3", "Harbor Print", "billing@harbor.example", "Invoice", "Please transfer the payment to our bank account.")
        self.pilot.check()
        self.assertEqual(self.kinds()[-3:], ["left", "left", "held"])
        self.assertEqual(self.box.unread_again, ["m3"])  # only what waits for you still looks new
        self.assertTrue(self.make(answer()).state["leftRead"])  # the choice outlives a restart
        with self.assertRaises(ValueError):
            self.pilot.command("left", read="yes")
        self.pilot.command("left", read=False)
        self.box.put("m4", "Shop", "no-reply@shop.example", "Another receipt", "Thanks again")
        self.pilot.check()
        self.assertEqual(self.box.unread_again, ["m3", "m4"])

    def test_your_own_mail_and_no_reply_senders_are_not_even_listed(self):
        for wanted in ("is:unread", "category:primary", "-from:me", "-from:noreply", "-from:no-reply"):
            self.assertIn(wanted, autopilot.UNREAD_QUERY.split())

    def test_a_mail_that_cannot_be_marked_unread_is_still_written_down(self):
        def broken():
            raise OSError("the page changed")

        self.box.keep_unread = broken
        self.box.put("m1", "Shop", "no-reply@shop.example", "Receipt", "Thanks for your order")
        self.pilot.check()
        self.assertEqual(self.kinds()[-1], "left")

    def test_a_mail_the_model_could_not_judge_is_read_again_at_the_next_look(self):
        answers = [RuntimeError("the model is busy"), answer()("")]

        def ask(prompt):
            result = answers.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        self.pilot = self.make(ask)
        self.box.put("m1", "Tomas Lind", "tomas@lind.example", "Your template", "Is it free?")
        self.pilot.check()
        self.assertEqual((self.box.sent, self.pilot.state["held"]), ([], []))
        self.assertEqual(self.kinds()[-1], "retry")
        self.assertIn("Trying again at the next check (try 1 of 3)", self.pilot.state["events"][-1]["text"])
        self.assertEqual(self.box.unread_again, ["m1"])  # it still looks new in Gmail meanwhile
        self.assertNotIn("m1", self.pilot.state["seen"])
        self.pilot.check()
        self.assertEqual(self.box.sent, [("tomas@lind.example", "Thanks, noted.\n\nNadia")])
        self.assertEqual(self.pilot.state["tries"], {})
        self.pilot.check()
        self.assertEqual(len(self.box.sent), 1)

    def test_after_three_failed_looks_the_mail_is_kept_for_you_and_left_alone(self):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            raise TimeoutError("no answer")

        self.pilot = self.make(ask)
        self.box.put("m1", "Tomas Lind", "tomas@lind.example", "Your template", "Is it free?")
        for _ in range(5):
            self.pilot.check()
        self.assertEqual(len(asked), autopilot.MAX_TRIES)
        self.assertEqual(self.kinds()[-3:], ["retry", "retry", "held"])
        held = self.pilot.snapshot()["held"]
        self.assertEqual([item["reason"] for item in held], ["The model did not answer (TimeoutError)."])
        self.assertEqual((self.box.sent, self.pilot.state["tries"]), ([], {}))

    def test_a_sender_address_that_was_not_on_the_page_gets_another_look(self):
        self.box.put("m1", "Tomas Lind", "", "Your template", "Is it free?")
        self.pilot.check()
        self.assertEqual((self.kinds()[-1], self.pilot.state["held"]), ("retry", []))
        self.box.put("m1", "Tomas Lind", "tomas@lind.example", "Your template", "Is it free?")
        self.pilot.check()
        self.assertEqual(self.box.sent, [("tomas@lind.example", "Thanks, noted.\n\nNadia")])

    def test_a_mail_the_rules_keep_is_not_tried_again(self):
        self.box.put("m1", "Harbor Print", "billing@harborprint.example", "Invoice", "Please transfer the payment to our bank account.")
        self.pilot.check()
        self.pilot.check()
        self.assertEqual(self.kinds().count("held"), 1)
        self.assertNotIn("retry", self.kinds())

    def test_the_count_of_looks_survives_a_restart(self):
        def ask(prompt):
            raise RuntimeError("down")

        self.pilot = self.make(ask)
        self.box.put("m1", "Tomas Lind", "tomas@lind.example", "Your template", "Is it free?")
        self.pilot.check()
        self.pilot.check()
        self.pilot = self.make(ask)
        self.assertEqual(self.pilot.state["tries"], {"m1": 2})
        self.pilot.check()
        self.assertEqual(len(self.pilot.state["held"]), 1)

    def test_a_failed_send_keeps_the_draft(self):
        self.box.fail = "Gmail showed no Reply button."
        self.box.put("m1", "Tomas", "tomas@lind.example", "Template", "Is it free?")
        self.pilot.check()
        held = self.pilot.snapshot()["held"][0]
        self.assertIn("Sending failed", held["reason"])
        self.assertEqual(held["draft"], "Thanks, noted.\n\nNadia")

    def test_a_mailbox_that_cannot_be_read_is_reported_and_closed(self):
        def broken(account, query):
            raise SourceError("Chrome is not signed in to this Gmail account.")

        self.box.open = broken
        closed = self.box.closed
        self.pilot.check()
        self.assertEqual(self.pilot.state["events"][-1]["text"], "Chrome is not signed in to this Gmail account.")
        self.assertEqual(self.box.closed, closed + 1)

    def test_state_survives_a_restart(self):
        self.box.put("m1", "Harbor Print", "billing@harbor.example", "Invoice", "Please transfer the payment to our bank account.")
        self.pilot.check()
        again = self.make(answer())
        self.assertTrue(again.state["on"])
        self.assertEqual(len(again.snapshot()["held"]), 1)
        again.check()
        self.assertEqual(len(again.snapshot()["held"]), 1)  # not read a second time

    def test_a_mail_seen_by_an_older_titip_is_not_opened_again(self):
        before = "Mira Voss , The drawing , has attachment, 18 Nov , Dear Juwen, see the file"
        state_file = Path(self.folder.name) / "autopilot.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["seen"][before] = int(self.now)
        state_file.write_text(json.dumps(state), encoding="utf-8")
        again = self.make(answer())
        self.assertIn("Mira Voss , The drawing , has attachment , Dear Juwen, see the file", again.state["seen"])
        self.assertNotIn(before, again.state["seen"])

    def test_bad_requests_are_refused(self):
        with self.assertRaises(ValueError):
            self.pilot.command("forward", to="x@y.example")
        with self.assertRaises(ValueError):
            self.pilot.command("send", id="nope", text="Hi")

    def test_thread_key_and_search(self):
        self.assertEqual(autopilot.thread_key("A@B.example", "RE: Fwd:  Hello  there"), "a@b.example|hello there")
        query = autopilot.find_query("a@b.example", 'Re: The "big" (plan)')
        self.assertIn("from:(a@b.example)", query)
        self.assertNotIn('"big"', query)


class Demo(unittest.TestCase):
    def test_the_demo_shows_every_path(self):
        with tempfile.TemporaryDirectory() as home:
            box = autopilot.DemoGmail()
            pilot = Autopilot(Path(home), box, autopilot.demo_ask)
            pilot.command("switch", on=True)
            for _ in range(5):
                pilot.check()
            kinds = [event["kind"] for event in pilot.state["events"]]
            self.assertEqual([box.sent[0][0]], ["tomas@lind.example"])
            self.assertIn("held", kinds)
            self.assertIn("left", kinds)
            for mail in autopilot.DEMO_MAIL:
                self.assertTrue(mail[1].endswith(".example"))


if __name__ == "__main__":
    unittest.main()
