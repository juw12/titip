"""0.14.1: another time for a meeting written the way Outlook's own proposal is, the automatic reply for
people outside, and the worker that keeps the app's own files. Against stand-ins; what the real Outlook
said to the rest is in refine_log.md, Run 20."""
import datetime as dt
import re
import unittest
from pathlib import Path

from tests.test_more_of_outlook import ID, Appointment, Message, at
from titip import mailbox
from titip.mailbox import DemoMailbox, MailError, OutlookMailbox, clean_act_request, clean_arrange_request

START, END = dt.datetime(2026, 11, 24, 8, 0), dt.datetime(2026, 11, 24, 9, 30)  # UTC


class Fields:
    """An answer's hidden fields, as PropertyAccessor reads and writes them. `shift`: hours Outlook adds to a
    time on the way in; `refuses`: fields it does not take; `stuck`: it stores another moment whatever is set."""

    def __init__(self, shift=0, refuses=(), stuck=False):
        self.kept, self.shift, self.refuses, self.stuck, self.deleted = {}, shift, refuses, stuck, []

    def SetProperty(self, name, value):
        if name in self.refuses:
            raise RuntimeError("The property is not supported.")
        if isinstance(value, dt.datetime):
            assert value.tzinfo is dt.timezone.utc, "a time is handed over as UTC, and says so"
            value = dt.datetime(2020, 1, 1) if self.stuck else value.replace(tzinfo=None) + dt.timedelta(hours=self.shift)
        self.kept[name] = value

    def GetProperty(self, name):
        return self.kept[name]

    def DeleteProperty(self, name):
        self.deleted.append(name)
        self.kept.pop(name, None)


class AnotherTimeProposed(unittest.TestCase):
    def answer(self, **how):
        return type("Answer", (), {"PropertyAccessor": Fields(**how)})()

    def test_the_four_fields_outlooks_own_proposal_uses(self):
        answer = self.answer()
        self.assertTrue(OutlookMailbox._propose(answer, START, END))
        self.assertEqual(answer.PropertyAccessor.kept, {
            mailbox.P_PROPOSED_START: START, mailbox.P_PROPOSED_END: END, mailbox.P_PROPOSED_MINUTES: 90, mailbox.P_PROPOSED: True})
        self.assertEqual((mailbox.P_PROPOSED_START[-8:], mailbox.P_PROPOSED_END[-8:], mailbox.P_PROPOSED_MINUTES[-8:], mailbox.P_PROPOSED[-8:]),
                         ("82500040", "82510040", "82560003", "8257000B"))
        self.assertIn("{00062002-0000-0000-C000-000000000046}", mailbox.APPOINTMENT)

    def test_an_outlook_that_shifts_a_time_is_given_it_again_until_it_reads_back_right(self):
        for hours in (7, -5):
            answer = self.answer(shift=hours)
            self.assertTrue(OutlookMailbox._propose(answer, START, END))
            self.assertEqual((answer.PropertyAccessor.kept[mailbox.P_PROPOSED_START], answer.PropertyAccessor.kept[mailbox.P_PROPOSED_END]), (START, END))

    def test_a_field_outlook_does_not_take_or_reads_back_wrong_takes_the_whole_proposal_out(self):
        for how in ({"refuses": (mailbox.P_PROPOSED_END,)}, {"refuses": (mailbox.P_PROPOSED,)}, {"stuck": True}, {"refuses": (mailbox.P_PROPOSED_START,)}):
            answer = self.answer(**how)
            self.assertFalse(OutlookMailbox._propose(answer, START, END), how)
            self.assertEqual(answer.PropertyAccessor.kept, {}, "no half proposal leaves: the answer says the time in words only")
            self.assertEqual(len(answer.PropertyAccessor.deleted), 4)

    def test_the_answer_leaves_either_way_and_says_whether_it_carries_the_proposal(self):
        for how, carried in (({}, True), ({"stuck": True}, False)):
            invitation = Message()
            invitation.MessageClass = "IPM.Schedule.Meeting.Request"
            meeting = Appointment("AA", "Drop test", at(24, 14), at(24, 16), meeting=3)
            real = meeting.Respond

            def respond(answer, no_window, real=real, how=how):
                reply = real(answer, no_window)
                reply.PropertyAccessor = Fields(**how)
                return reply

            meeting.Respond = respond
            invitation.GetAssociatedAppointment = lambda add: meeting
            box = OutlookMailbox(read_ahead=False)
            box._item = lambda mail_id: invitation
            box._app = type("App", (), {})()
            result = box._act(clean_act_request({"what": "respond", "id": ID, "answer": "tentative", "note": "I suggest another time: Tuesday.",
                                                 "propose": {"start": "2026-11-24T08:00:00.000Z", "end": "2026-11-24T09:30:00.000Z"}}))
            self.assertEqual(result, {"ok": True, "responded": "tentative", "told": True, "proposed": carried})
            self.assertEqual((meeting.reply.Body, meeting.reply.calls), ("I suggest another time: Tuesday.", ["send"]))
        plain = clean_act_request({"what": "respond", "id": ID, "answer": "accept"})
        self.assertIsNone(plain["propose"])

    def test_a_time_that_ends_before_it_starts_or_cannot_be_read_is_refused(self):
        for bad in ({"start": "2026-11-24T09:00:00Z", "end": "2026-11-24T08:00:00Z"}, {"start": "2026-11-24T09:00:00Z", "end": "2026-11-24T09:00:00Z"},
                    {"start": "tomorrow", "end": "later"}, {"start": "2026-11-24T09:00:00Z"}, {"start": "2026-01-01T09:00:00Z", "end": "2026-12-24T09:00:00Z"}):
            with self.assertRaises(MailError, msg=bad):
                clean_act_request({"what": "respond", "id": ID, "answer": "tentative", "propose": bad})


class Storage:
    def __init__(self, text):
        self.Body, self.saves = text, 0


class ForPeopleOutside(unittest.TestCase):
    def box(self, inside="For colleagues.", outside="For everybody else.", keeps_outside=True):
        notes = {mailbox.OOF_TEMPLATE: Storage(inside), mailbox.OOF_OUTSIDE: Storage(outside)}

        class Inbox:
            def GetStorage(self, name, how):
                kept = notes[name]
                fresh = Storage(kept.Body)

                def save():
                    kept.saves += 1
                    if name == mailbox.OOF_TEMPLATE or keeps_outside:
                        kept.Body = fresh.Body

                fresh.Save = save
                return fresh

        box = OutlookMailbox(read_ahead=False)
        box._ns = type("NS", (), {"GetDefaultFolder": lambda ns, number: Inbox(),
                                   "DefaultStore": type("S", (), {"PropertyAccessor": type("A", (), {"GetProperty": lambda a, n: False})()})()})()
        return box, notes

    def test_both_texts_are_read_each_from_its_own_note(self):
        box, _ = self.box()
        self.assertEqual(box._away_now(), {"on": False, "text": "For colleagues.", "outside": "For everybody else."})

    def test_the_text_for_people_outside_is_written_only_when_the_phone_hands_one_over_and_it_differs(self):
        box, notes = self.box()
        box._arrange(clean_arrange_request({"what": "awayText", "text": "New for colleagues."}))
        self.assertEqual((notes[mailbox.OOF_OUTSIDE].Body, notes[mailbox.OOF_OUTSIDE].saves), ("For everybody else.", 0), "a page from before 0.14.1 leaves it alone")
        result = box._arrange(clean_arrange_request({"what": "awayText", "text": "New for colleagues.", "outside": "For everybody else."}))
        self.assertEqual((notes[mailbox.OOF_OUTSIDE].saves, result["outside"]), (0, "For everybody else."), "the same text is not written again")
        box._arrange(clean_arrange_request({"what": "awayText", "text": "New for colleagues.", "outside": "  I am away.\r\nWrite to the office.  "}))
        self.assertEqual((notes[mailbox.OOF_TEMPLATE].Body, notes[mailbox.OOF_OUTSIDE].Body), ("New for colleagues.", "I am away.\nWrite to the office."))
        box._arrange(clean_arrange_request({"what": "awayText", "text": "New for colleagues.", "outside": ""}))
        self.assertEqual(notes[mailbox.OOF_OUTSIDE].Body, "", "an empty one is a way to say: nothing for people outside")

    def test_a_text_for_outside_that_outlook_does_not_keep_is_said(self):
        box, notes = self.box(keeps_outside=False)
        with self.assertRaises(MailError) as told:
            box._arrange(clean_arrange_request({"what": "awayText", "text": "New for colleagues.", "outside": "New for outside."}))
        self.assertIn("not the one for people outside", str(told.exception))
        self.assertEqual(notes[mailbox.OOF_TEMPLATE].Body, "New for colleagues.")

    def test_the_invented_mailbox_keeps_both(self):
        box = DemoMailbox()
        self.assertEqual(box.away()["outside"], "")
        box.arrange({"what": "awayText", "text": "Back on Monday.", "outside": "Away until Monday."})
        self.assertEqual(box.away(), {"on": False, "text": "Back on Monday.", "outside": "Away until Monday."})
        box.arrange({"what": "awayText", "text": "Back on Tuesday."})
        self.assertEqual(box.away()["outside"], "Away until Monday.")


WEB = Path(__file__).resolve().parent.parent / "titip" / "web"


class ThePageWithoutANetwork(unittest.TestCase):
    """The worker keeps the app's own files; the page puts it in where it is the page for any network."""

    def setUp(self):
        self.worker = (WEB / "sw.js").read_text(encoding="utf-8")
        self.page = (WEB / "app.js").read_text(encoding="utf-8")

    def test_every_file_the_worker_fetches_for_itself_is_a_file_of_the_page(self):
        listed = re.search(r"var OWN = \[(.*?)\];", self.worker, re.S).group(1)
        names = re.findall(r'"([^"]+)"', listed)
        self.assertIn("./", names)
        self.assertEqual([name for name in names if name != "./" and not (WEB / name).is_file()], [])
        index = (WEB / "index.html").read_text(encoding="utf-8")
        needed = set(re.findall(r'(?:href|src)="([a-z0-9.-]+\.(?:css|js|png|svg|webmanifest))"', index))
        needed |= set(re.findall(r'url\("([a-z0-9-]+\.woff2)"\)', (WEB / "styles.css").read_text(encoding="utf-8")))
        self.assertEqual(sorted(needed - set(names)), [], "what the page loads at its start is all kept")
        self.assertNotIn("desk.html", names, "the page of the PC itself is not a phone's to keep")

    def test_only_its_own_files_and_the_network_first(self):
        self.assertIn('url.origin !== self.location.origin || url.pathname.indexOf("/api/") >= 0', self.worker)
        self.assertIn('request.method !== "GET"', self.worker)
        self.assertLess(self.worker.index("fetch(request)"), self.worker.index("cache.match(kept)"))

    def test_the_page_for_any_network_puts_the_worker_in_when_it_loads(self):
        self.assertIn('if (HOSTED || store.get("titip.push") === "1") navigator.serviceWorker.register("sw.js")', self.page)

    def test_what_is_pressed_is_a_finger_high(self):
        looks = (WEB / "styles.css").read_text(encoding="utf-8")
        small = [line.split("{")[0].strip() for line in looks.splitlines() if "min-height: 44px" in line or "min-width: 44px" in line]
        self.assertEqual(small, [], "Android asks for 48 px: var(--tap)")
        self.assertIn("--tap: 48px", looks)
        self.assertIn("#from.tap { display: inline-block; padding: 14px 0; margin: -14px 0; }", looks)
        self.assertNotIn('id="from" role="button"', (WEB / "index.html").read_text(encoding="utf-8"), "a name is a button only when it shows something")


if __name__ == "__main__":
    unittest.main()
