"""0.12.0, against a stand-in Outlook: a list narrowed to what is unread or flagged, guests who need not come,
how an entry shows to others, an entry every second week, an answer with a note, and what Outlook knows of one person."""
import datetime as dt
import unittest

from tests import test_server
from tests.test_more_of_outlook import ID, App, Appointment, Message, Recipient, Recipients, at, went_to
from tests.test_rest_of_outlook import ROW, Shifting, TableFolder, planned
from titip import mailbox, remote
from titip.mailbox import DemoMailbox, MailError, OutlookBusy, OutlookMailbox, clean_act_request, clean_plan_request

UNREAD, FLAGGED = mailbox.SHOWS["unread"], mailbox.SHOWS["flagged"]


class OnlyUnreadOrFlagged(unittest.TestCase):
    def box(self, folder):
        box = OutlookMailbox(read_ahead=False)
        box._mail_folders = lambda wanted: [folder]
        return box

    def test_outlook_is_asked_for_what_is_unread_or_flagged_only(self):
        folder = TableFolder([ROW])
        self.box(folder)._list("inbox", 40, 0, "", False, "unread")
        self.box(folder)._list("inbox", 40, 0, "", False, "flagged")
        self.assertEqual(folder.filters, [f"@SQL=({UNREAD})", f"@SQL=({FLAGGED})"])
        self.assertEqual((UNREAD, FLAGGED), ('"urn:schemas:httpmail:read" = 0', '"http://schemas.microsoft.com/mapi/proptag/0x10900003" = 2'))

    def test_a_search_among_them_asks_for_both_at_once(self):
        folder = TableFolder([ROW])
        self.box(folder)._list("inbox", 40, 0, "quote", False, "unread")
        self.assertEqual(folder.filters, ['@SQL=("urn:schemas:httpmail:subject" like \'%quote%\' OR "urn:schemas:httpmail:fromname" like \'%quote%\')'
                                          f" AND ({UNREAD})"])
        deep = TableFolder([ROW], indexed=True)
        self.box(deep)._list("inbox", 40, 0, "quote", True, "flagged")
        self.assertTrue(deep.filters[0].startswith("@SQL=(") and deep.filters[0].endswith(f") AND ({FLAGGED})") and mailbox.BODY_TEXT in deep.filters[0])

    def test_without_it_the_folder_is_asked_as_before(self):
        folder = TableFolder([ROW])
        self.box(folder)._list("inbox", 40, 0, "")
        self.box(folder)._list("inbox", 40, 0, "", True)  # "also in the text" with nothing typed: nothing to look for
        self.assertEqual(folder.filters, ["", ""])

    def test_it_is_asked_of_outlook_and_an_unknown_one_is_refused(self):
        box = self.box(TableFolder([ROW]))
        asked = []
        box._do = lambda fn, *args, **more: asked.append(args) or {"mail": [], "more": False}
        box._want = lambda rows: None
        box.list("inbox", show="unread")
        box.list("inbox", 40, 0, "quote", True, "flagged")
        self.assertEqual(asked, [("inbox", 40, 0, "", False, "unread"), ("inbox", 40, 0, "quote", True, "flagged")])
        with self.assertRaises(MailError):
            box.list("inbox", show="everything")
        self.assertEqual(asked[2:], [])

    def test_a_search_that_takes_longer_than_the_phone_waits_is_asked_once_and_its_answer_is_there_for_the_next_try(self):
        import threading

        saved = mailbox.READ_SECONDS
        mailbox.READ_SECONDS = 0.05
        self.addCleanup(setattr, mailbox, "READ_SECONDS", saved)
        box = self.box(TableFolder([ROW]))
        done, asked = threading.Event(), []

        def slow(fn, *args, timeout=20, first=False):
            asked.append((args, timeout))
            done.wait(5)
            return {"mail": [{"id": "s1.A"}], "more": False}

        box._do = slow
        box._want = lambda rows: None
        for _ in range(4):  # the phone gives up and asks again
            with self.assertRaises(OutlookBusy):
                box.list("inbox", 40, 0, "Quote  for", True)
        self.assertEqual(asked, [(("inbox", 40, 0, "Quote  for", True), mailbox.ASK_SECONDS)], "one question, however often the phone asked")
        done.set()
        for _ in range(100):
            if any(key[0] == "narrowed" for key in box._answers):
                break
            threading.Event().wait(0.01)
        self.assertEqual(box.list("inbox", 40, 0, "quote for", True)["mail"], [{"id": "s1.A"}], "the answer that came late is the one the next try gets")
        self.assertEqual(len(asked), 1)
        box._answers[("calendars",)] = (0.0, [])
        for word in range(80):  # a search for every word typed does not push out what else is kept
            box._keep(("narrowed", "inbox", 40, 0, f"word {word}", False, ""), {"mail": [], "more": False})
            box._answers[("narrowed", "inbox", 40, 0, f"word {word}", False, "")] = (0.0, {"mail": [], "more": False})  # and a moment passes
        self.assertIn(("calendars",), box._answers)
        self.assertLess(len(box._answers), 5)
        box._do = lambda fn, *args, **more: {"ok": True, "deleted": True}
        box.act({"what": "delete", "id": "s1.ABCDEF0123456789"})
        self.assertFalse([key for key in box._answers if key[0] == "narrowed"], "after something was deleted no kept search is shown")


class Planning(unittest.TestCase):
    """An Outlook that makes calendar entries, or holds one that is there; and what the phone orders."""

    def setUp(self):
        self.made = []

    def box(self, existing=None):
        box = OutlookMailbox(read_ahead=False)
        outer = self

        class Calendar(App):
            def CreateItem(self, kind):
                outer.made.append(planned(Shifting(0)))
                return outer.made[-1]

        box._app = Calendar()
        box._item = lambda event_id: existing
        box._who = ("me@example.com", "Me")
        box._mine = {"me@example.com"}
        return box

    def order(self, **more):
        start = at(24, 14).astimezone().astimezone(dt.timezone.utc)
        return {"what": "save", "subject": "Gasket fit check", "location": "Lab 2", "body": "Bring both units.",
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": (start + dt.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ"), **more}

    def meeting(self):
        entry = planned(Appointment("AB", "Review", at(24, 9), at(24, 10), meeting=1, response=1, Organizer="Me"))
        entry.Recipients = Recipients([
            Recipient("Me", 1, smtp="me@example.com"), Recipient("Tomas Berg", 1, smtp="tomas@example.com"),
            Recipient("Anna Okafor", 1, smtp="anna@example.com"), Recipient("Jonas Weber", 2, smtp="jonas@example.com"),
            Recipient("Room 2", 3),
        ])
        return entry


class GuestsWhoNeedNotCome(Planning):
    def test_a_new_meeting_invites_both_kinds_and_one_with_only_optional_guests_is_a_meeting_too(self):
        self.box()._plan(clean_plan_request(self.order(**{"with": ["priya@example.com"], "optional": ["tomas@example.com", "ana@example.com"]})))
        self.assertEqual(went_to(self.made[0]), [("priya@example.com", 1), ("tomas@example.com", 2), ("ana@example.com", 2)])
        self.assertEqual((self.made[0].MeetingStatus, self.made[0].calls), (1, ["send"]))
        self.box()._plan(clean_plan_request(self.order(optional=["tomas@example.com"])))
        self.assertEqual((went_to(self.made[1]), self.made[1].MeetingStatus, self.made[1].calls), ([("tomas@example.com", 2)], 1, ["send"]))

    def test_the_optional_ones_are_changed_and_the_required_ones_the_organizer_and_the_room_stay(self):
        entry = self.meeting()
        result = self.box(entry)._plan(clean_plan_request(self.order(id=ID, optionalExact=True, optional=["new@example.com"])))
        self.assertEqual(went_to(entry), [("Me", 1), ("Tomas Berg", 1), ("Anna Okafor", 1), ("Room 2", 3), ("new@example.com", 2)])
        self.assertEqual((result["told"], entry.calls), (True, ["send"]))

    def test_somebody_moved_from_required_to_optional_is_there_once_as_optional(self):
        entry = self.meeting()
        self.box(entry)._plan(clean_plan_request(self.order(
            id=ID, withExact=True, optionalExact=True, **{"with": ["anna@example.com"], "optional": ["jonas@example.com", "tomas@example.com"]})))
        self.assertEqual(went_to(entry), [("Me", 1), ("Anna Okafor", 1), ("Jonas Weber", 2), ("Room 2", 3), ("tomas@example.com", 2)])

    def test_a_page_from_before_0_12_0_changes_the_required_ones_and_leaves_the_optional_ones(self):
        entry = self.meeting()
        self.box(entry)._plan(clean_plan_request(self.order(id=ID, withExact=True, **{"with": ["tomas@example.com"]})))
        self.assertEqual(went_to(entry), [("Me", 1), ("Tomas Berg", 1), ("Jonas Weber", 2), ("Room 2", 3)])

    def test_an_entry_in_full_lists_them_apart(self):
        entry = self.meeting()
        box = self.box(entry)
        full = box._event(ID)
        self.assertEqual([one["name"] for one in full["invitedPeople"]], ["Tomas Berg", "Anna Okafor"])
        self.assertEqual([one["name"] for one in full["optionalPeople"]], ["Jonas Weber"])


class HowItShowsAndRepeats(Planning):
    def test_show_as_and_private_are_set_only_when_the_phone_names_them(self):
        self.box()._plan(clean_plan_request(self.order(busy="away", private=True)))
        self.assertEqual((self.made[0].BusyStatus, self.made[0].Sensitivity), (3, 2))
        mine = planned(Appointment("AA", "Fit check", at(24, 9), at(24, 10), Sensitivity=2))
        self.box(mine)._plan(clean_plan_request(self.order(id=ID)))
        self.assertEqual((mine.BusyStatus, mine.Sensitivity), (2, 2), "not named: as they are")
        self.box(mine)._plan(clean_plan_request(self.order(id=ID, busy="free", private=False)))
        self.assertEqual((mine.BusyStatus, mine.Sensitivity), (0, 0))
        clean = clean_plan_request(self.order(busy="asleep", private="yes"))
        self.assertEqual((clean["busy"], clean["private"]), (None, None))

    def test_the_calendar_says_what_is_private(self):
        row = OutlookMailbox._event_row(Appointment("AA", "Dentist", at(24, 9), at(24, 10), Sensitivity=2))
        self.assertTrue(row["private"])
        self.assertFalse(OutlookMailbox._event_row(Appointment("AA", "Stand-up", at(24, 9), at(24, 10)))["private"])

    def test_every_second_week_and_every_working_day(self):
        box = self.box()
        made = []

        def create(kind):
            entry = planned(Appointment("NEW", "", at(1), at(1)))
            entry.pattern = type("P", (), {})()
            entry.GetRecurrencePattern = lambda: entry.pattern
            made.append(entry)
            return entry

        box._app.CreateItem = create
        box._plan(clean_plan_request(self.order(repeat={"every": "week", "interval": 2, "times": 5})))
        self.assertEqual((made[0].pattern.RecurrenceType, made[0].pattern.Interval, made[0].pattern.Occurrences), (1, 2, 5))
        box._plan(clean_plan_request(self.order(repeat={"every": "weekday"})))
        self.assertEqual((made[1].pattern.RecurrenceType, made[1].pattern.DayOfWeekMask, made[1].pattern.NoEndDate), (1, 62, True))
        self.assertFalse(hasattr(made[1].pattern, "Interval"))
        box._plan(clean_plan_request(self.order(repeat={"every": "week"})))
        self.assertFalse(hasattr(made[2].pattern, "Interval") or hasattr(made[2].pattern, "DayOfWeekMask"), "every week: as before 0.12.0")

    def test_how_often_is_kept_within_what_outlook_takes(self):
        repeat = lambda **how: clean_plan_request(self.order(repeat=how))["repeat"]  # noqa: E731
        self.assertEqual(repeat(every="day", interval=1000)["interval"], mailbox.REPEAT_INTERVAL_MOST)
        self.assertEqual(repeat(every="month", interval=-3)["interval"], 1)
        self.assertEqual(repeat(every="year", interval=4)["interval"], 1)
        self.assertEqual(repeat(every="weekday", interval=2)["interval"], 1)
        self.assertEqual(repeat(every="week")["interval"], 1)
        with self.assertRaises(MailError):
            repeat(every="week", interval="often")

    def test_an_entry_can_go_over_several_days_with_hours(self):
        start = at(24, 14).astimezone().astimezone(dt.timezone.utc)
        order = self.order(end=(start + dt.timedelta(days=2, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.box()._plan(clean_plan_request(order))
        self.assertEqual((self.made[0].Start, self.made[0].End), (at(24, 14), at(26, 15)))


class AnAnswerWithANote(unittest.TestCase):
    def answer(self, **more):
        invitation = Message()
        invitation.MessageClass = "IPM.Schedule.Meeting.Request"
        meeting = Appointment("AA", "Drop test", at(24, 14), at(24, 16), meeting=3)
        invitation.GetAssociatedAppointment = lambda add: meeting
        box = OutlookMailbox(read_ahead=False)
        box._item = lambda mail_id: invitation
        box._app = type("App", (), {})()
        result = box._act(clean_act_request({"what": "respond", "id": ID, "answer": "tentative", **more}))
        return result, meeting

    def test_the_note_is_what_the_organizer_reads_in_the_answer(self):
        result, meeting = self.answer(note="  I can join after 15:00.  ")
        self.assertEqual((meeting.reply.Body, meeting.reply.calls, result["told"]), ("I can join after 15:00.", ["send"], True))

    def test_without_one_the_answer_is_outlooks_own(self):
        _, meeting = self.answer()
        self.assertEqual((meeting.reply.Body, meeting.reply.calls), ("signature", ["send"]))
        self.assertEqual(len(clean_act_request({"what": "respond", "id": ID, "answer": "accept", "note": "x" * 9000})["note"]), mailbox.NOTE_MOST)


class Entry:
    """One line of an address book: a colleague in the directory, somebody in your contacts, or only an address."""

    def __init__(self, address, user=None, contact=None):
        self.Address, self.user, self.contact = address, user, contact
        self.PropertyAccessor = type("A", (), {"GetProperty": lambda accessor, name: address if "@" in address else (_ for _ in ()).throw(RuntimeError())})()

    def GetExchangeUser(self):
        return self.user

    def GetContact(self):
        return self.contact


class Book:
    def __init__(self, known):
        self.known, self.asked = known, []

    def CreateRecipient(self, text):
        self.asked.append(text)
        entry = self.known.get(text.lower())
        return type("R", (), {"Name": getattr(entry, "name", text), "AddressEntry": entry, "Resolve": lambda recipient: entry is not None})()


def person_of(**fields):
    return type("P", (), fields)()


class OnePerson(unittest.TestCase):
    def box(self, **known):
        box = OutlookMailbox(read_ahead=False)
        box._ns = Book({key.replace("_", " "): value for key, value in known.items()})
        box._do = lambda fn, *args, **more: fn(*args)
        return box

    def test_a_colleague_comes_from_the_company_directory(self):
        colleague = Entry("/o=Lumenworks/cn=priya", user=person_of(
            Name="Priya  Nair", PrimarySmtpAddress="priya.nair@lumenworks.example", JobTitle="Design lead", Department="Hardware",
            CompanyName="Lumenworks", OfficeLocation="Building 2, 3F", MobileTelephoneNumber="+00 000 0108", BusinessTelephoneNumber=""))
        card = self.box(priya_nair=colleague).person("  Priya   Nair ")
        self.assertEqual(card, {
            "found": True, "known": True, "name": "Priya Nair", "address": "priya.nair@lumenworks.example", "job": "Design lead",
            "department": "Hardware", "company": "Lumenworks", "office": "Building 2, 3F", "phones": [{"label": "Mobile", "number": "+00 000 0108"}]})

    def test_somebody_outside_comes_from_your_contacts(self):
        supplier = Entry("orders@okafor-supplies.example", contact=person_of(
            FullName="Ada Okafor", Email1Address="orders@okafor-supplies.example", JobTitle="Sales", CompanyName="Okafor Supplies",
            MobileTelephoneNumber="+00 000 0142", BusinessTelephoneNumber="+00 000 0140"))
        card = self.box(**{"orders@okafor-supplies.example": supplier}).person("orders@okafor-supplies.example")
        self.assertEqual((card["name"], card["job"], card["company"], card["known"]), ("Ada Okafor", "Sales", "Okafor Supplies", True))
        self.assertEqual(card["phones"], [{"label": "Mobile", "number": "+00 000 0142"}, {"label": "Work", "number": "+00 000 0140"}])

    def test_an_address_nobody_keeps_is_found_and_said_to_be_only_that(self):
        card = self.box(**{"new@example.com": Entry("new@example.com")}).person("new@example.com")
        self.assertEqual((card["found"], card["known"], card["address"], card["phones"]), (True, False, "new@example.com", []))

    def test_a_name_outlook_cannot_pin_down_is_not_found(self):
        self.assertEqual(self.box().person("Somebody Unknown"), {"found": False})
        with self.assertRaises(MailError):
            self.box().person("x")

    def test_a_directory_that_fails_still_gives_the_contact_and_a_busy_outlook_is_not_an_error(self):
        class Failing(Entry):
            def GetExchangeUser(self):
                raise RuntimeError("the directory cannot be reached")

        card = self.box(ada=Failing("ada@example.com", contact=person_of(FullName="Ada", JobTitle="Sales"))).person("ada")
        self.assertEqual((card["job"], card["address"]), ("Sales", "ada@example.com"))
        box = self.box()

        def busy(fn, *args, timeout=0, first=False):
            self.assertEqual((timeout, first), (mailbox.LOOKUP_SECONDS, True), "a tap does not wait long, and goes ahead of the reading")
            raise OutlookBusy()

        box._do = busy
        self.assertEqual(box.person("Priya Nair"), {"found": False, "busy": True})


class Demo(unittest.TestCase):
    def test_the_same_things_in_the_invented_mailbox(self):
        box = DemoMailbox()
        every = box.list("inbox", 100)["mail"]
        unread = box.list("inbox", 100, show="unread")["mail"]
        flagged = box.list("inbox", 100, show="flagged")["mail"]
        self.assertTrue(0 < len(unread) < len(every) and all(mail["unread"] for mail in unread))
        self.assertTrue(0 < len(flagged) < len(every) and all(mail["flagged"] for mail in flagged))
        with self.assertRaises(MailError):
            box.list("inbox", show="everything")
        self.assertEqual((box.person("Ada Okafor")["job"], box.person("orders@okafor-supplies.example")["known"]), ("Sales", True))
        self.assertEqual((box.person("Pritam Das")["known"], box.person("nobody at all"), box.person("new@example.com")["found"]), (False, {"found": False}, True))
        start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        order = {"what": "save", "subject": "Two days in the lab", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "end": (start + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "optional": ["jonas.weber@lumenworks.example"],
                 "busy": "away", "private": True}
        self.assertEqual(box.plan(order), {"ok": True, "saved": True, "told": True})
        made = next(event for event in box.calendar()["events"] if event["subject"] == "Two days in the lab")
        full = box.event(made["id"])
        self.assertEqual((made["busy"], made["private"], made["meeting"], made["lastDay"] > made["day"]), ("away", True, True, True))
        self.assertEqual(([one["name"] for one in full["optionalPeople"]], full["invitedPeople"]), (["Jonas Weber"], []))
        box.plan({**order, "id": made["id"], "optionalExact": True, "optional": [], "private": False})
        after = box.event(made["id"])
        self.assertEqual((after["optionalPeople"], after["private"], after["meeting"]), ([], False, False))


class OverTheWire(test_server.ServerCase):
    def setUp(self):
        self.token = self.pair()[0]

    def get(self, path, cookie=True):
        import json

        reply, data = self.phone("GET", path, cookie=self.token if cookie else None)
        return reply.status, json.loads(data) if reply.status == 200 else None

    def test_a_narrowed_list_and_one_person_for_a_paired_phone_only(self):
        status, page = self.get("/api/mail?folder=inbox&limit=100&show=unread")
        self.assertTrue(status == 200 and page["mail"] and all(mail["unread"] for mail in page["mail"]))
        self.assertEqual(self.app.mail_call("mail", {"folder": "inbox", "limit": 100, "show": "unread"}), (200, page))  # what the relay is answered with
        self.assertEqual(self.app.mail_call("mail", {"folder": "inbox", "limit": 100, "show": ""})[1]["mail"], self.get("/api/mail?folder=inbox&limit=100")[1]["mail"])
        self.assertEqual(self.get("/api/mail?folder=inbox&show=everything")[0], 502)
        status, card = self.get("/api/person?q=Ada%20Okafor")
        self.assertEqual((status, card["job"], card["phones"][0]["label"]), (200, "Sales", "Mobile"))
        self.assertEqual(self.app.mail_call("person", {"q": "Ada Okafor"}), (200, card))
        for path in ("/api/person?q=Ada%20Okafor", "/api/mail?folder=inbox&show=unread"):
            self.assertEqual(self.get(path, cookie=False)[0], 401, path)
        self.assertIn("person", remote.ASK_ROUTES)
        self.assertNotIn("person", remote.ONCE_ROUTES)


if __name__ == "__main__":
    unittest.main()
