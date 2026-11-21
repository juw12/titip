"""0.10.3: Titip keeps Outlook less busy, and a send gets through.

On the PC this was written for, reading the calendar takes Outlook from 40 seconds to several minutes.
The phone stopped waiting after 20, asked again, and each time Outlook was set to read it all once
more: the answer never came, and a mail sent meanwhile was told "Outlook is busy".
"""
import sys
import threading
import time
import types
import unittest

from tests.test_more_of_outlook import ID, Message, Recipient, Source, outlook_with, went_to
from titip import mailbox
from titip.mailbox import MailError, OutlookBusy, OutlookMailbox, clean_send_request

if "pythoncom" not in sys.modules and sys.platform != "win32":  # the queue itself needs nothing of Windows
    sys.modules["pythoncom"] = types.SimpleNamespace(CoInitialize=lambda: None, com_error=type("com_error", (Exception,), {}))


def wait_until(condition, seconds=3):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        time.sleep(0.01)
    return condition()


class SlowOutlook:
    """Stands in for OutlookMailbox._do: every question takes until `answer` is set."""

    def __init__(self):
        self.answer = threading.Event()
        self.asked = []

    def __call__(self, fn, *args, timeout=20, first=False):
        self.asked.append((fn.__name__, timeout))
        if not self.answer.wait(timeout):
            raise OutlookBusy(True)
        if fn.__name__ == "_calendar":
            return {"events": [{"subject": "Stand-up"}], "from": "2026-11-21"}
        if fn.__name__ == "_folders_now":
            return {"folders": [{"name": "Projects"}], "categories": []}
        return [{"name": "Priya Nair"}]


class ASlowQuestionIsAskedOnce(unittest.TestCase):
    def setUp(self):
        saved = mailbox.READ_SECONDS, mailbox.QUICK_SECONDS
        mailbox.READ_SECONDS, mailbox.QUICK_SECONDS = 0.1, 0.05
        self.addCleanup(lambda: (setattr(mailbox, "READ_SECONDS", saved[0]), setattr(mailbox, "QUICK_SECONDS", saved[1])))
        self.outlook = SlowOutlook()
        self.box = OutlookMailbox(read_ahead=False)
        self.box._do = self.outlook

    def test_the_calendar_however_often_the_phone_asks_and_its_answer_is_kept_though_nobody_waited(self):
        for _ in range(5):  # the phone stops waiting, and asks again a second later
            with self.assertRaises(OutlookBusy):
                self.box.calendar()
        self.assertEqual(self.outlook.asked, [("_calendar", mailbox.SLOW_SECONDS)], "one question, and Outlook is given the time it takes")
        self.outlook.answer.set()
        self.assertTrue(wait_until(lambda: (0, 14) in self.box._calendars))
        self.assertEqual(self.box.calendar()["events"], [{"subject": "Stand-up"}])
        self.assertEqual(len(self.outlook.asked), 1, "the answer that came after everybody gave up is the one the phone gets")

    def test_the_folders_and_the_other_calendars_the_same_way(self):
        for ask in (self.box.folders, self.box.calendars, self.box.rules):
            with self.assertRaises(OutlookBusy):
                ask()
            with self.assertRaises(OutlookBusy):
                ask()
        self.assertEqual(sorted(name for name, _ in self.outlook.asked), ["_calendars_now", "_folders_now", "_rules_now"])
        self.outlook.answer.set()
        self.assertTrue(wait_until(lambda: self.box._folder_list is not None and len(self.box._answers) == 2))
        self.assertEqual(self.box.folders()["folders"], [{"name": "Projects"}])
        self.assertEqual(self.box.calendars(), {"calendars": [{"name": "Priya Nair"}]})
        self.assertEqual(len(self.outlook.asked), 3)

    def test_the_calendar_is_kept_ten_minutes_and_refresh_asks_anew(self):
        self.outlook.answer.set()
        self.box.calendar()
        self.box.calendar()
        self.box.calendar(fresh=True)
        self.assertEqual(len(self.outlook.asked), 1, "just read: Refresh does not make Outlook read it again")
        when, page = self.box._calendars[(0, 14)]
        self.box._calendars[(0, 14)] = (when - mailbox.CALENDAR_AGAIN_SECONDS - 1, page)
        self.box.calendar()
        self.assertEqual(len(self.outlook.asked), 1)
        self.box.calendar(fresh=True)
        self.assertEqual(len(self.outlook.asked), 2)

    def test_while_outlook_reads_it_again_the_page_from_before_is_shown(self):
        self.outlook.answer.set()
        self.box.calendar()
        self.outlook.answer.clear()
        when, page = self.box._calendars[(0, 14)]
        self.box._calendars[(0, 14)] = (when - mailbox.CALENDAR_FRESH_SECONDS - 1, page)
        again = self.box.calendar()
        self.assertEqual((again["saved"], again["events"]), (True, [{"subject": "Stand-up"}]))
        self.outlook.answer.set()


class Turned(Exception):
    """What pywin32 raises when Outlook does not take a call."""

    def __init__(self, hresult, text=""):
        super().__init__(text)
        self.hresult, self.excepinfo = hresult, (0, "Outlook", text)


class TurnedAway(unittest.TestCase):
    REJECTED = mailbox.RPC_BUSY[0]

    def box(self):
        box = OutlookMailbox(read_ahead=False)
        box._connect = lambda: None
        return box

    def test_a_call_outlook_turns_away_is_made_again_and_goes_through(self):
        tries, pauses = [], []

        def step():
            tries.append(1)
            if len(tries) < 3:
                raise Turned(self.REJECTED)
            return "done"

        self.assertEqual(self.box()._attempt(step, (), Turned, pause=pauses.append), "done")
        self.assertEqual((len(tries), pauses), (3, [mailbox.RPC_PAUSE] * 2))

    def test_turned_away_every_time_is_busy_and_says_by_what(self):
        tries = []

        def step():
            tries.append(1)
            raise Turned(mailbox.RPC_BUSY[1])

        with self.assertRaises(OutlookBusy) as caught:
            self.box()._attempt(step, (), Turned, pause=lambda seconds: None)
        self.assertEqual(len(tries), mailbox.RPC_TRIES)
        self.assertFalse(caught.exception.started)
        self.assertIn("a window of its own", caught.exception.why())

    def test_any_other_refusal_is_told_at_once_in_outlooks_words(self):
        tries = []

        def step():
            tries.append(1)
            raise Turned(-2147352567, "The attachment is too large.")

        box = self.box()
        box._app = object()
        with self.assertRaises(MailError) as caught:
            box._attempt(step, (), Turned, pause=self.fail)
        self.assertEqual((str(caught.exception), len(tries), box._app), ("The attachment is too large.", 1, None))

    def test_a_forward_turned_away_at_the_last_step_is_made_again_and_leaves_once(self):
        source = Source(files=["plan.pdf"])
        box = outlook_with(source)
        box._connect = lambda: None
        made, turned = [], []
        real_forward = source.Forward

        def forward():
            mail = real_forward()
            made.append(mail)
            if not turned:  # the first one: Outlook does not take Send
                turned.append(1)
                mail.Send = lambda: (_ for _ in ()).throw(Turned(self.REJECTED))
            return mail

        source.Forward = forward
        request = clean_send_request({"mode": "forward", "id": ID, "recipients": "exact", "to": ["a@example.com"], "body": "FYI"})
        self.assertEqual(box._attempt(box._send, (request,), Turned, pause=lambda seconds: None), {"ok": True, "sent": True})
        self.assertEqual([mail.calls for mail in made], [["close1"], ["send"]], "the one that did not leave is thrown away, the second one leaves")
        self.assertEqual(went_to(made[1]), [("a@example.com", 1)])


class ASendDoesNotWaitOutALongReading(unittest.TestCase):
    def test_a_reading_gives_way_between_two_steps_and_the_send_goes_next(self):
        from titip.mailbox import _ComThread

        thread = _ComThread()
        box = OutlookMailbox(read_ahead=False)
        box._thread = thread
        order, reading_started = [], threading.Event()

        def long_reading():
            reading_started.set()
            for step in range(200):  # a calendar, entry by entry
                box._give_way()
                time.sleep(0.01)
            order.append("the reading finished")

        def reader():
            try:
                thread.call(long_reading, timeout=5, what="reading the calendar")
            except OutlookBusy:
                order.append("the reading gave way")

        reading = threading.Thread(target=reader)
        reading.start()
        self.assertTrue(reading_started.wait(2))
        started = time.monotonic()
        thread.call(order.append, "the send", timeout=5, first=True, what="sending a mail")
        reading.join(5)
        self.assertLess(time.monotonic() - started, 1, "the send did not wait for the two seconds the reading would have taken")
        self.assertEqual(order, ["the send", "the reading gave way"])
        self.assertFalse(thread.must_give_way())

    def test_a_send_never_gives_way_to_another_send_and_a_reading_alone_is_left_alone(self):
        from titip.mailbox import _ComThread

        thread = _ComThread()
        box = OutlookMailbox(read_ahead=False)
        box._thread = thread
        seen = []
        thread.call(lambda: seen.append(thread.must_give_way()), timeout=5)  # a reading, nobody waiting
        gate = threading.Event()

        def first_send():
            gate.wait(2)
            seen.append(thread.must_give_way())  # another send waits behind this one
            box._give_way()

        sender = threading.Thread(target=lambda: thread.call(first_send, timeout=5, first=True))
        sender.start()
        time.sleep(0.1)
        second = threading.Thread(target=lambda: thread.call(lambda: None, timeout=5, first=True))
        second.start()
        time.sleep(0.1)
        gate.set()
        sender.join(5)
        second.join(5)
        self.assertEqual(seen, [False, False])

    def test_a_send_that_could_not_wait_says_what_outlook_was_kept_by(self):
        from titip.mailbox import _ComThread

        thread = _ComThread()
        gate = threading.Event()
        holder = threading.Thread(target=lambda: thread.call(gate.wait, 5, timeout=5, what="reading the calendar"))
        holder.start()
        time.sleep(0.1)
        box = OutlookMailbox(read_ahead=False)
        box._thread = thread
        saved = mailbox.SEND_SECONDS
        mailbox.SEND_SECONDS = 0.3
        self.addCleanup(lambda: setattr(mailbox, "SEND_SECONDS", saved))
        with self.assertRaises(OutlookBusy) as caught:
            thread.call(lambda: None, timeout=0.3, first=True)
        self.assertEqual((caught.exception.started, caught.exception.doing), (False, "reading the calendar"))
        self.assertFalse(thread.must_give_way(), "a send that gave up is not waited for any more")
        busy = OutlookBusy(False, "reading the calendar", 95.4)
        box._do = lambda fn, *args, timeout=20, first=False: (_ for _ in ()).throw(busy)
        with self.assertRaises(MailError) as caught:
            box.send({"mode": "new", "to": "a@example.com", "body": "x"})
        self.assertEqual(str(caught.exception), "Outlook on your PC is busy, so nothing was sent. Outlook has been reading the calendar for 95 seconds. "
                                                "Your text is kept here. Try again in a moment.")
        gate.set()
        holder.join(5)


class ReadingAMessageCostsLess(unittest.TestCase):
    def test_of_a_mail_to_very_many_only_the_first_are_asked_for_their_address_one_by_one(self):
        saved = mailbox.ACCESSOR_RECIPIENTS
        mailbox.ACCESSOR_RECIPIENTS = 2
        self.addCleanup(lambda: setattr(mailbox, "ACCESSOR_RECIPIENTS", saved))
        asked = []

        class Counting(Recipient):
            def __init__(self, name, smtp):
                super().__init__(name, 1, smtp=smtp)
                accessor = self.PropertyAccessor
                self.PropertyAccessor = types.SimpleNamespace(GetProperty=lambda tag: asked.append(name) or accessor.GetProperty(tag))

        message = Message(Counting("One", "one@example.com"), Counting("Two", "two@example.com"), Counting("Three", "three@example.com"),
                          Counting("four@example.com", "four@example.com"))
        found, _ = OutlookMailbox._recipients(message.Recipients, 300)
        self.assertEqual(asked, ["One", "Two"])
        self.assertEqual([address for _, address, _ in found], ["one@example.com", "two@example.com", "/o=Office/cn=Three", "four@example.com"])


if __name__ == "__main__":
    unittest.main()
