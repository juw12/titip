"""What 0.9.0 added, against a fake Outlook: who an answer goes to, files that go along, the calendar,
invitations, deleting, and the folders beyond Inbox, Sent and Drafts."""
import datetime as dt
import unittest
from pathlib import Path

from titip import mailbox
from titip.mailbox import (
    DemoMailbox,
    MailError,
    OutlookMailbox,
    answer_plans,
    as_person,
    clean_act_request,
    clean_send_request,
    file_name,
    short_date,
)

ID = "s1.ABCDEF0123456789"


class Accessor:
    def __init__(self, smtp):
        self.smtp = smtp

    def GetProperty(self, name):
        if self.smtp is None:
            raise RuntimeError("Outlook does not have it")
        return self.smtp


class Recipient:
    """One person on a mail. A colleague has an internal Address and the internet one only on request."""

    def __init__(self, name, kind=1, smtp=None, known=True):
        self.Name, self.Type = name, kind
        self.Address = name if "@" in name else "/o=Office/cn=" + name
        self.PropertyAccessor = Accessor(smtp if smtp is not None else (name if "@" in name else None))
        self.Resolved = known  # Outlook knows whoever it put on a mail itself


class Recipients(list):
    Count = property(len)

    def Item(self, index):
        return self[index - 1]

    def Add(self, name):
        self.append(Recipient(name, known="@" in name))  # a name typed on the phone is looked up when the mail is sent
        return self[-1]

    def Remove(self, index):
        del self[index - 1]

    def ResolveAll(self):
        return all(recipient.Resolved for recipient in self)


class Attachment:
    def __init__(self, name, data=b""):
        self.FileName, self.data = name, data


class Attachments(list):
    Count = property(len)

    def Item(self, index):
        return self[index - 1]

    def Add(self, path):
        self.append(Attachment(Path(path).name, Path(path).read_bytes()))  # Outlook reads the file now
        self.paths = [*getattr(self, "paths", []), Path(path)]

    def Remove(self, index):
        del self[index - 1]


class Mail:
    def __init__(self, sent=False, recipients=(), files=()):
        self.Sent = sent
        self.Subject = ""
        self.HTMLBody = "<html><body><p>signature</p><hr>quoted thread</body></html>"
        self.Body = "signature"
        self.Recipients = Recipients(recipients)
        self.Attachments = Attachments(Attachment(name) for name in files)
        self.calls = []

    def _set(self, kind, value):
        self.Recipients[:] = [recipient for recipient in self.Recipients if recipient.Type != kind]
        for name in filter(None, (part.strip() for part in value.split(";"))):
            self.Recipients.Add(name).Type = kind

    To = property(lambda self: "", lambda self, value: self._set(1, value))
    CC = property(lambda self: "", lambda self, value: self._set(2, value))
    BCC = property(lambda self: "", lambda self, value: self._set(3, value))

    def Send(self):
        self.calls.append("send")

    def Save(self):
        self.calls.append("save")

    def Close(self, how):
        self.calls.append(f"close{how}")


class Source(Mail):
    """A received mail. Outlook addresses the answer: the sender, and for Reply all the others too."""

    def __init__(self, files=()):
        super().__init__(sent=True, files=files)
        self.made = None

    def _answer(self, *recipients):
        self.made = Mail(recipients=recipients, files=[attachment.FileName for attachment in self.Attachments])
        return self.made

    def Reply(self):
        return self._answer(Recipient("Boss, The", 1, smtp="boss@example.com"))

    def ReplyAll(self):
        return self._answer(
            Recipient("Boss, The", 1, smtp="boss@example.com"),
            Recipient("Anna Okafor", 1, smtp="anna@example.com"),
            Recipient("Hardware Team", 2),  # a list Outlook knows only by its internal name
            Recipient("tomas@example.com", 2),
        )

    def Forward(self):
        return self._answer()


class App:
    def __init__(self):
        self.created = []

    def CreateItem(self, kind):
        self.created.append(Mail())
        return self.created[-1]


def outlook_with(source=None):
    box = OutlookMailbox(read_ahead=False)
    box._app = App()
    box._item = lambda mail_id: source
    return box


def went_to(mail):
    return [(recipient.Name, recipient.Type) for recipient in mail.Recipients]


class WhoAnAnswerGoesTo(unittest.TestCase):
    """The phone shows who Reply and Reply all go to; what it then hands over is the whole list."""

    def send(self, box, **payload):
        return box._send(clean_send_request(payload))

    def test_untouched_the_answer_goes_to_exactly_whom_outlook_addressed_it(self):
        source = Source()
        self.send(outlook_with(source), mode="replyAll", id=ID, recipients="exact", body="Agreed.",
                  to=["boss@example.com", "anna@example.com"], cc=["Hardware Team", "tomas@example.com"])
        self.assertEqual(went_to(source.made), [("Boss, The", 1), ("Anna Okafor", 1), ("Hardware Team", 2), ("tomas@example.com", 2)])
        self.assertEqual(source.made.calls, ["send"])
        self.assertEqual(source.calls, [], "the original message is never changed")

    def test_somebody_taken_out_does_not_get_it_and_somebody_added_does(self):
        source = Source()
        self.send(outlook_with(source), mode="replyAll", id=ID, recipients="exact", body="Agreed.",
                  to=["boss@example.com"], cc=["tomas@example.com", "new@example.com"], bcc=["quiet@example.com"])
        self.assertEqual(went_to(source.made), [("Boss, The", 1), ("tomas@example.com", 2), ("new@example.com", 2), ("quiet@example.com", 3)])

    def test_somebody_moved_from_to_into_cc_is_there_once(self):
        source = Source()
        self.send(outlook_with(source), mode="replyAll", id=ID, recipients="exact", body="x",
                  to=["boss@example.com"], cc=["anna@example.com"])
        self.assertEqual(went_to(source.made), [("Boss, The", 1), ("anna@example.com", 2)])

    def test_a_reply_can_go_to_somebody_else_than_the_sender(self):
        source = Source()
        self.send(outlook_with(source), mode="reply", id=ID, recipients="exact", body="x", to=["other@example.com"])
        self.assertEqual(went_to(source.made), [("other@example.com", 1)])

    def test_an_answer_with_nobody_left_is_refused_before_outlook_is_asked(self):
        with self.assertRaises(MailError) as caught:
            clean_send_request({"mode": "reply", "id": ID, "recipients": "exact", "body": "x"})
        self.assertEqual(str(caught.exception), "Add at least one recipient.")

    def test_a_page_from_before_0_9_0_still_only_adds_to_what_outlook_addressed(self):
        source = Source()
        self.send(outlook_with(source), mode="reply", id=ID, cc="extra@example.com", body="x")
        self.assertEqual(went_to(source.made), [("Boss, The", 1), ("extra@example.com", 2)])

    def test_a_forward_and_a_new_mail_take_bcc(self):
        source = Source()
        self.send(outlook_with(source), mode="forward", id=ID, recipients="exact", to=["a@example.com"], bcc=["b@example.com"], body="")
        self.assertEqual(went_to(source.made), [("a@example.com", 1), ("b@example.com", 3)])
        box = outlook_with()
        self.send(box, mode="new", to="a@example.com", bcc="b@example.com", body="Hi")
        self.assertEqual(went_to(box._app.created[0]), [("a@example.com", 1), ("b@example.com", 3)])
        self.assertEqual(clean_send_request({"mode": "new", "bcc": "b@example.com", "body": "Hi"})["bcc"], ["b@example.com"],
                         "Bcc alone is somebody to send it to")

    def test_a_name_outlook_does_not_know_stops_the_answer_and_discards_it(self):
        source = Source()
        with self.assertRaises(MailError) as caught:
            self.send(outlook_with(source), mode="reply", id=ID, recipients="exact", body="x", to=["boss@example.com", "nobody"])
        self.assertIn("nobody", str(caught.exception))
        self.assertEqual(source.made.calls, ["close1"])


class Plans(unittest.TestCase):
    ME = {"me@example.com", "me.alias@example.com"}

    def people(self, *names):
        return [as_person(name, f"{name.lower()}@example.com") for name in names]

    def names(self, people):
        return [one["name"] for one in people]

    def test_reply_goes_to_the_sender_and_reply_all_to_everyone_but_you(self):
        me, alias = as_person("Me", "ME@example.com"), as_person("Me too", "me.alias@example.com")
        plans = answer_plans(as_person("Rina", "rina@example.com"), [me, *self.people("Tomas")], [alias, *self.people("Anna", "Tomas")], [], self.ME)
        self.assertEqual(self.names(plans["reply"]["to"]), ["Rina"])
        self.assertEqual(plans["reply"]["cc"], [])
        self.assertEqual(self.names(plans["replyAll"]["to"]), ["Rina", "Tomas"])
        self.assertEqual(self.names(plans["replyAll"]["cc"]), ["Anna"], "nobody twice, and never yourself")

    def test_a_mail_that_asks_to_be_answered_elsewhere(self):
        plans = answer_plans(as_person("Shop", "noreply@shop.example"), self.people("Me"), [], [as_person("Shop desk", "desk@shop.example")], self.ME)
        self.assertEqual(self.names(plans["reply"]["to"]), ["Shop desk"])

    def test_an_answer_to_your_own_mail_goes_to_whom_it_went(self):
        plans = answer_plans(as_person("Me", "me@example.com"), self.people("Tomas", "Anna"), self.people("Rina"), [], self.ME)
        self.assertEqual(self.names(plans["reply"]["to"]), ["Tomas", "Anna"])
        self.assertEqual((self.names(plans["replyAll"]["to"]), self.names(plans["replyAll"]["cc"])), (["Tomas", "Anna"], ["Rina"]))
        alone = answer_plans(as_person("Me", "me@example.com"), [as_person("Me", "me@example.com")], [], [], self.ME)
        self.assertEqual(self.names(alone["reply"]["to"]), ["Me"], "a note to yourself is answered to yourself")

    def test_a_colleague_without_an_internet_address_is_kept_by_name(self):
        colleague = as_person("Hardware Team", "/o=Office/cn=Recipients/cn=hw")
        self.assertEqual(colleague, {"name": "Hardware Team", "address": ""})
        plans = answer_plans(as_person("Rina", "rina@example.com"), [colleague, colleague], [], [], self.ME)
        self.assertEqual(self.names(plans["replyAll"]["to"]), ["Rina", "Hardware Team"])


class Message:
    """A received message as _get reads it."""

    MessageClass = "IPM.Note"
    Subject, SenderName, SenderEmailAddress, To, CC, BCC, Body, HTMLBody, Sent = "S", "Rina Wijaya", "rina@example.com", "Me; Tomas", "Anna", "", "text", "", True

    def __init__(self, *recipients, reply_to=()):
        self.Recipients = Recipients(recipients)
        self.ReplyRecipients = Recipients(reply_to)
        self.Attachments = Attachments()
        self.PropertyAccessor = Accessor("rina@example.com")


class Reading(unittest.TestCase):
    def test_a_message_says_who_it_went_to_and_who_an_answer_goes_to(self):
        box = OutlookMailbox(read_ahead=False)
        box._who = ("me@example.com", "Me")
        message = Message(Recipient("Me", 1, smtp="me@example.com"), Recipient("Tomas Berg", 1, smtp="tomas@example.com"),
                          Recipient("Anna Okafor", 2, smtp="anna@example.com"), Recipient("Hidden", 3, smtp="hidden@example.com"))
        box._item = lambda mail_id: message
        got = box._get(ID)
        self.assertEqual([one["name"] for one in got["people"]["to"]], ["Me", "Tomas Berg"])
        self.assertEqual(got["people"]["cc"], [{"name": "Anna Okafor", "address": "anna@example.com"}])
        self.assertEqual([one["address"] for one in got["answer"]["reply"]["to"]], ["rina@example.com"])
        self.assertEqual([one["address"] for one in got["answer"]["replyAll"]["to"]], ["rina@example.com", "tomas@example.com"])
        self.assertEqual([one["address"] for one in got["answer"]["replyAll"]["cc"]], ["anna@example.com"])
        self.assertEqual((got["kind"], got["invite"], got["peopleCut"]), ("mail", None, False))
        self.assertIn("tomas@example.com", box._people, "the people of the message are still learned for the suggestions")

    def test_a_mail_to_more_people_than_are_read_says_so(self):
        saved, mailbox.RECIPIENTS_READ = mailbox.RECIPIENTS_READ, 3
        self.addCleanup(lambda: setattr(mailbox, "RECIPIENTS_READ", saved))
        box = OutlookMailbox(read_ahead=False)
        box._who = ("me@example.com", "Me")
        box._item = lambda mail_id: Message(*(Recipient(f"p{n}@example.com") for n in range(5)))
        got = box._get(ID)
        self.assertTrue(got["peopleCut"])
        self.assertEqual(len(got["people"]["to"]), 3)

    def test_a_message_outlook_cannot_list_the_people_of_still_opens(self):
        box = OutlookMailbox(read_ahead=False)
        box._who = ("me@example.com", "Me")
        message = Message()
        message.Recipients = None
        box._item = lambda mail_id: message
        got = box._get(ID)
        self.assertEqual(got["people"], {"to": [], "cc": [], "bcc": []})
        self.assertEqual([one["address"] for one in got["answer"]["reply"]["to"]], ["rina@example.com"])


class FilesThatGoAlong(unittest.TestCase):
    def send(self, box, **payload):
        return box._send(clean_send_request(payload))

    def test_a_file_is_read_into_the_mail_and_nothing_of_it_stays_on_disk(self):
        box = outlook_with()
        self.send(box, mode="new", to="a@example.com", body="See attached.",
                  files=[{"name": "plan.pdf", "data": b"%PDF one"}, {"name": "plan.pdf", "data": b"%PDF two"}])
        mail = box._app.created[0]
        self.assertEqual([(file.FileName, file.data) for file in mail.Attachments], [("plan.pdf", b"%PDF one"), ("plan.pdf", b"%PDF two")])
        self.assertEqual(mail.calls, ["send"])
        for path in mail.Attachments.paths:
            self.assertFalse(path.exists() or path.parent.exists() or path.parent.parent.exists(), path)

    def test_nothing_stays_on_disk_when_the_send_fails_either(self):
        box = outlook_with()
        with self.assertRaises(MailError):
            self.send(box, mode="new", to="nobody", body="x", files=[{"name": "a.txt", "data": b"a"}])
        mail = box._app.created[0]
        self.assertEqual(mail.calls, ["close1"])
        self.assertFalse(mail.Attachments.paths[0].parent.parent.exists())

    def test_a_draft_keeps_the_file_and_a_reply_can_carry_one(self):
        box = outlook_with()
        self.assertEqual(self.send(box, mode="new", action="draft", body="", files=[{"name": "a.txt", "data": b"a"}]), {"ok": True, "saved": True})
        self.assertEqual((box._app.created[0].calls, len(box._app.created[0].Attachments)), (["save"], 1))
        source = Source()
        self.send(outlook_with(source), mode="reply", id=ID, recipients="exact", to=["boss@example.com"], body="x", files=[{"name": "b.txt", "data": b"b"}])
        self.assertEqual([file.FileName for file in source.made.Attachments], ["b.txt"])

    def test_a_forward_can_leave_a_file_of_the_original_behind(self):
        source = Source(files=["a.pdf", "big.zip", "c.png"])
        self.send(outlook_with(source), mode="forward", id=ID, recipients="exact", to=["a@example.com"], body="",
                  drop=[{"index": 1, "name": "a.pdf"}, {"index": 3, "name": "c.png"}])
        self.assertEqual([file.FileName for file in source.made.Attachments], ["big.zip"])
        self.assertEqual([file.FileName for file in source.Attachments], ["a.pdf", "big.zip", "c.png"], "the original keeps its files")

    def test_only_the_file_the_phone_named_is_taken_out(self):
        source = Source(files=["a.pdf", "big.zip"])
        with self.assertRaises(MailError) as caught:
            self.send(outlook_with(source), mode="forward", id=ID, recipients="exact", to=["a@example.com"], body="",
                      drop=[{"index": 2, "name": "a.pdf"}])  # the numbers have shifted since the phone read it
        self.assertIn("could not be taken out", str(caught.exception))
        self.assertEqual((source.made.calls, len(source.made.Attachments)), (["close1"], 2), "nothing was sent")
        self.assertEqual(clean_send_request({"mode": "reply", "id": ID, "body": "x", "drop": [{"index": 1, "name": "a"}]})["drop"], [],
                         "an answer carries no files of the original to take out")

    def test_what_is_not_a_file_or_too_much_is_refused(self):
        for files in ([{"name": "a", "data": "text, not bytes"}], ["../../secret"], [{"name": "a"}]):
            with self.assertRaises(MailError):
                clean_send_request({"mode": "new", "to": "a@example.com", "body": "x", "files": files})
        with self.assertRaises(MailError):
            clean_send_request({"mode": "new", "to": "a@example.com", "body": "x",
                                "files": [{"name": "a", "data": b"x"}] * (mailbox.MAX_FILES + 1)})
        saved, mailbox.MAX_FILES_BYTES = mailbox.MAX_FILES_BYTES, 10
        self.addCleanup(lambda: setattr(mailbox, "MAX_FILES_BYTES", saved))
        with self.assertRaises(MailError):
            clean_send_request({"mode": "new", "to": "a@example.com", "body": "x", "files": [{"name": "a", "data": b"x" * 11}]})

    def test_a_file_name_cannot_leave_its_folder(self):
        self.assertEqual(file_name("../../Windows/system.ini"), "system.ini")
        self.assertEqual(file_name("C:\\Users\\me\\report?.pdf"), "report_.pdf")
        self.assertEqual(file_name("  .. "), "file")
        self.assertEqual(file_name("nul.txt"), "_nul.txt")
        self.assertEqual(file_name(None), "file")
        long = file_name("x" * 300 + ".xlsx")
        self.assertTrue(len(long) <= mailbox.MAX_FILE_NAME and long.endswith(".xlsx"))


def at(day, hour=0, minute=0):
    return dt.datetime(2026, 11, day, hour, minute)


class Appointment:
    MessageClass = "IPM.Appointment"

    def __init__(self, entry, subject, start, end, all_day=False, meeting=0, response=0, **more):
        self.EntryID, self.Subject, self.Start, self.End, self.AllDayEvent = entry, subject, start, end, all_day
        self.StartUTC, self.EndUTC = start - dt.timedelta(hours=7), end - dt.timedelta(hours=7)
        self.MeetingStatus, self.ResponseStatus = meeting, response
        self.Location, self.Organizer, self.IsRecurring, self.BusyStatus = "Room 2", "Jonas Weber", False, 2
        self.Body, self.RequiredAttendees, self.OptionalAttendees = "Agenda", "Me; Jonas Weber", ""
        self.answered = []
        for name, value in more.items():
            setattr(self, name, value)

    def Respond(self, answer, no_window):
        self.answered.append((answer, no_window))
        reply = None if getattr(self, "silent", False) else Mail()
        self.reply = reply
        return reply


class CalendarItems:
    def __init__(self, entries):
        self.entries, self.steps, self.filter, self.filters = entries, [], "", []
        self.reads = None  # how this Outlook reads a filter: text -> the entries it gives back (or it raises)

    def Sort(self, by):
        self.steps.append(("sort", by))

    def __setattr__(self, name, value):
        if name == "IncludeRecurrences":
            self.steps.append(("recurrences", value))
        object.__setattr__(self, name, value)

    def Restrict(self, text):
        self.filter = text
        self.filters.append(text)
        return iter(self.entries if self.reads is None else self.reads(text))


class Namespace:
    def __init__(self, entries):
        self.items = CalendarItems(entries)
        self.asked = []

    def GetDefaultFolder(self, number):
        self.asked.append(number)
        return type("Folder", (), {"StoreID": "STORE", "Items": self.items})()

    def CompareEntryIDs(self, one, other):
        return one == other


class Calendar(unittest.TestCase):
    def setUp(self):
        real = mailbox.today
        mailbox.today = lambda: at(20)
        self.addCleanup(lambda: setattr(mailbox, "today", real))

    def box(self, *entries):
        box = OutlookMailbox(read_ahead=False)
        box._ns = Namespace(list(entries))
        return box

    def test_the_days_ahead_with_every_time_a_repeating_meeting_takes_place(self):
        box = self.box(
            Appointment("AA", "Ended yesterday", at(19, 9), at(19, 10)),
            Appointment("AB", "All of yesterday", at(19), at(20), all_day=True),
            Appointment("AC", "Began yesterday, still on", at(19, 22), at(20, 2)),
            Appointment("AD", "Stand-up", at(21, 9, 30), at(21, 9, 50), IsRecurring=True),
            Appointment("AD", "Stand-up", at(22, 9, 30), at(22, 9, 50), IsRecurring=True),
            Appointment("AE", "Trade fair", at(23), at(25), all_day=True),
            Appointment("AF", "Invitation", at(24, 14), at(24, 15), meeting=3, response=5),
            Appointment("AG", "Next month", at(20, 9) + dt.timedelta(days=14), at(20, 10) + dt.timedelta(days=14)),
            Appointment("AH", "Never reached", at(20, 9) + dt.timedelta(days=40), at(20, 10) + dt.timedelta(days=40)),
        )
        page = box._calendar(0, 14)
        self.assertEqual([event["subject"] for event in page["events"]],
                         ["Began yesterday, still on", "Stand-up", "Stand-up", "Trade fair", "Invitation"])
        self.assertEqual((page["from"], page["days"], page["more"]), ("2026-11-20", 14, False))
        self.assertEqual(box._ns.items.steps, [("sort", "[Start]"), ("recurrences", True)], "in this order, or Outlook lists a series once")
        self.assertEqual(box._ns.asked, [mailbox.OL_CALENDAR])
        stand_up, fair, invitation = page["events"][1], page["events"][3], page["events"][4]
        self.assertEqual((stand_up["start"], stand_up["end"], stand_up["day"]), ("2026-11-21T02:30:00Z", "2026-11-21T02:50:00Z", "2026-11-21"))
        self.assertEqual(page["events"][1]["id"], page["events"][2]["id"], "one series, one id")
        self.assertEqual((fair["allDay"], fair["day"], fair["lastDay"]), (True, "2026-11-23", "2026-11-24"))
        self.assertEqual((invitation["invited"], invitation["response"], invitation["meeting"]), (True, "none", True))
        self.assertEqual((stand_up["invited"], stand_up["response"], stand_up["recurring"]), (False, "", True))

    def test_outlook_is_asked_for_a_day_more_on_each_side_first_in_this_pcs_way_of_writing_dates(self):
        box = self.box(Appointment("AD", "Fine", at(21, 9), at(21, 10)))
        box._calendar(0, 14)
        self.assertEqual(box._ns.items.filters, [f"[Start] <= '{short_date(at(20) + dt.timedelta(days=15))}' AND [End] >= '{short_date(at(19))}'"])
        box._ns.items.reads = lambda text: []
        box._calendar(14, 14)
        self.assertIn(short_date(at(20) + dt.timedelta(days=13)), box._ns.items.filter)
        self.assertEqual(len(box._ns.items.filters), 2, "the way that gave an answer is asked again, and an empty answer from it is believed")

    def test_dates_outlook_reads_another_way_are_asked_for_again_written_differently(self):
        """Outlook on this PC reads 11/12/2026 as the 11th of December, not as the 12th of November."""
        fine = Appointment("AD", "This week", at(21, 9), at(21, 10))
        far = [Appointment("AX", f"Far away {n}", dt.datetime(2027, 3, 1 + n, 9), dt.datetime(2027, 3, 1 + n, 10)) for n in range(4)]
        box = self.box()
        box._ns.items.reads = lambda text: [fine] if "2026-" in text else far  # only year first is understood
        page = box._calendar(0, 14)
        self.assertEqual([event["subject"] for event in page["events"]], ["This week"])
        self.assertEqual(len(box._ns.items.filters), 2)
        self.assertIn("2026-11-19", box._ns.items.filters[-1])
        box._calendar(0, 7)
        self.assertEqual(len(box._ns.items.filters), 3, "the way that worked is the one asked first from then on")
        self.assertIn("2026-11-19", box._ns.items.filters[-1])

    def test_a_way_of_writing_dates_that_outlook_refuses_is_passed_over(self):
        fine = Appointment("AD", "This week", at(21, 9), at(21, 10))
        box = self.box()

        def reads(text):
            if "2026-" not in text:
                raise RuntimeError("Type mismatch or the value in the condition is not valid")
            return [fine]

        box._ns.items.reads = reads
        self.assertEqual(len(box._calendar(0, 14)["events"]), 1)
        box._ns.items.reads = lambda text: (_ for _ in ()).throw(RuntimeError("never"))
        box._date_way = None
        with self.assertRaises(MailError) as caught:
            box._calendar(0, 14)
        self.assertIn("did not understand the days", str(caught.exception))

    def test_when_no_way_of_asking_is_clean_what_belongs_to_the_days_is_still_shown(self):
        fine = Appointment("AD", "This week", at(21, 9), at(21, 10))
        far = [Appointment("AX", f"Far away {n}", dt.datetime(2027, 3, 1 + n, 9), dt.datetime(2027, 3, 1 + n, 10)) for n in range(4)]
        box = self.box()
        box._ns.items.reads = lambda text: [fine, *far]
        self.assertEqual([event["subject"] for event in box._calendar(0, 14)["events"]], ["This week"])
        self.assertIsNone(box._date_way)

    def test_days_with_nothing_in_them_are_an_empty_calendar_not_a_problem(self):
        box = self.box()
        page = box._calendar(0, 14)
        self.assertEqual((page["events"], page["from"]), ([], "2026-11-20"))
        self.assertGreaterEqual(len(box._ns.items.filters), 2, "every way of writing the dates was tried before saying so")

    def test_a_series_without_an_end_does_not_keep_outlook_for_ever(self):
        saved, mailbox.CALENDAR_LOOK = mailbox.CALENDAR_LOOK, 5
        self.addCleanup(lambda: setattr(mailbox, "CALENDAR_LOOK", saved))
        box = self.box(*(Appointment("AD", "Every minute", at(21, 9, n), at(21, 9, n + 1)) for n in range(30)))
        page = box._calendar(0, 14)
        self.assertEqual((len(page["events"]), page["more"]), (5, True))

    def test_an_entry_outlook_cannot_read_is_left_out_and_the_rest_still_come(self):
        broken = type("Broken", (), {"Start": property(lambda self: 1 / 0)})()
        page = self.box(broken, Appointment("AD", "Fine", at(21, 9), at(21, 10)))._calendar(0, 14)
        self.assertEqual([event["subject"] for event in page["events"]], ["Fine"])

    def test_the_page_is_kept_a_minute_and_shown_marked_when_outlook_is_busy(self):
        box = OutlookMailbox(read_ahead=False)
        asked = []

        def outlook(fn, *args, timeout=20, first=False):
            asked.append(fn.__name__)
            if len(asked) > 1:
                raise mailbox.OutlookBusy()
            return {"events": [], "from": "2026-11-20"}

        box._do = outlook
        self.assertNotIn("saved", box.calendar())
        self.assertNotIn("saved", box.calendar())
        self.assertEqual(asked, ["_calendar"], "not asked again within the minute")
        box._calendars[(0, 14)] = (0.0 - mailbox.CALENDAR_FRESH_SECONDS, box._calendars[(0, 14)][1])
        self.assertTrue(box.calendar()["saved"])
        with self.assertRaises(mailbox.OutlookBusy):
            box.calendar(14)  # days nobody has seen yet: the phone is told busy and asks again

    def test_one_entry_in_full(self):
        box = OutlookMailbox(read_ahead=False)
        box._item = lambda event_id: Appointment("AD", "Drop test", at(21, 14), at(21, 16), meeting=3, response=3, OptionalAttendees="Priya Nair")
        got = box._event(ID)
        self.assertEqual((got["id"], got["body"], got["required"], got["optional"], got["response"]), (ID, "Agenda", "Me; Jonas Weber", "Priya Nair", "accepted"))
        box._item = lambda event_id: Message()
        with self.assertRaises(MailError):
            box._event(ID)


class Invitation:
    MessageClass = "IPM.Schedule.Meeting.Request"

    def __init__(self, appointment):
        self.appointment, self.added = appointment, []

    def GetAssociatedAppointment(self, add):
        self.added.append(add)
        return self.appointment


class AnsweringAnInvitation(unittest.TestCase):
    def act(self, item, **payload):
        box = OutlookMailbox(read_ahead=False)
        box._item = lambda mail_id: item
        box._ns = Namespace([])
        return box._act(clean_act_request({"id": ID, **payload}))

    def test_accept_from_the_mail_sends_the_answer_without_a_window_on_the_pc(self):
        meeting = Appointment("AF", "Drop test", at(24, 14), at(24, 15), meeting=3, response=5)
        invitation = Invitation(meeting)
        self.assertEqual(self.act(invitation, what="respond", answer="accept"), {"ok": True, "responded": "accept", "told": True})
        self.assertEqual((meeting.answered, meeting.reply.calls, invitation.added), ([(3, True)], ["send"], [True]))

    def test_tentative_and_decline_from_the_calendar(self):
        for answer, code in (("tentative", 2), ("decline", 4)):
            meeting = Appointment("AF", "Drop test", at(24, 14), at(24, 15), meeting=3, response=5)
            self.act(meeting, what="respond", answer=answer)
            self.assertEqual((meeting.answered, meeting.reply.calls), ([(code, True)], ["send"]))

    def test_an_organizer_who_wants_no_answers_is_not_sent_one(self):
        meeting = Appointment("AF", "Town hall", at(24, 14), at(24, 15), meeting=3, response=5, silent=True)
        self.assertEqual(self.act(meeting, what="respond", answer="accept")["told"], False)

    def test_your_own_entry_and_a_plain_mail_cannot_be_answered(self):
        for item in (Appointment("AA", "Mine", at(24, 14), at(24, 15), meeting=1), Appointment("AB", "Dentist", at(24, 14), at(24, 15)), Message(), Invitation(None)):
            with self.assertRaises(MailError):
                self.act(item, what="respond", answer="accept")
        for bad in ({"what": "respond", "answer": "maybe"}, {"what": "respond"}, {"what": "archive"}, {"what": "delete", "id": ""}):
            with self.assertRaises(MailError):
                clean_act_request({"id": ID, **bad})

    def test_looking_at_an_invitation_shows_what_it_is_for_and_does_not_put_it_in_the_calendar(self):
        box = OutlookMailbox(read_ahead=False)
        box._who = ("me@example.com", "Me")
        message = Message()
        message.MessageClass = "IPM.Schedule.Meeting.Request"
        invitation = Invitation(Appointment("AF", "Drop test", at(24, 14), at(24, 15), meeting=3, response=5))
        message.GetAssociatedAppointment = invitation.GetAssociatedAppointment
        box._item = lambda mail_id: message
        got = box._get(ID)
        self.assertEqual((got["kind"], got["invite"]["day"], got["invite"]["response"], got["invite"]["invited"]), ("meeting", "2026-11-24", "none", True))
        self.assertEqual(invitation.added, [False])


class Deletable(Message):
    def __init__(self, folder_id="INBOX"):
        super().__init__()
        self.deleted = 0
        store = type("Store", (), {"GetDefaultFolder": lambda self, number: type("Bin", (), {"EntryID": "BIN"})()})()
        self.Parent = type("Folder", (), {"EntryID": folder_id, "Store": store})()

    def Delete(self):
        self.deleted += 1


class Deleting(unittest.TestCase):
    def act(self, item):
        box = OutlookMailbox(read_ahead=False)
        box._item = lambda mail_id: item
        box._ns = Namespace([])
        return box._act(clean_act_request({"what": "delete", "id": ID}))

    def test_a_message_goes_to_deleted_items(self):
        mail = Deletable()
        self.assertEqual(self.act(mail), {"ok": True, "deleted": True})
        self.assertEqual(mail.deleted, 1)

    def test_what_is_in_deleted_items_already_is_never_removed_for_good(self):
        mail = Deletable(folder_id="BIN")
        with self.assertRaises(MailError) as caught:
            self.act(mail)
        self.assertIn("for good", str(caught.exception))
        self.assertEqual(mail.deleted, 0)

    def test_a_calendar_entry_is_not_deleted_from_the_phone(self):
        meeting = Appointment("AF", "Drop test", at(24, 14), at(24, 15))
        meeting.Delete = lambda: self.fail("deleted")
        with self.assertRaises(MailError):
            self.act(meeting)

    def test_it_goes_ahead_of_the_reading_ahead_and_the_lists_are_asked_for_again(self):
        box = OutlookMailbox(read_ahead=False)
        asked = []
        box._do = lambda fn, *args, timeout=20, first=False: asked.append((fn.__name__, first)) or {"ok": True, "deleted": True}
        box._lists[("inbox", 40)] = (0.0, "", {"mail": []})
        box._calendars[(0, 14)] = (0.0, {})
        box._messages[ID] = {"id": ID, "body": "x", "html": ""}
        box.act({"what": "delete", "id": ID})
        self.assertEqual(asked, [("_act", True)])
        self.assertEqual((box._lists, box._calendars, dict(box._messages)), ({}, {}, {}))

    def test_a_busy_outlook_says_whether_it_may_have_happened(self):
        box = OutlookMailbox(read_ahead=False)
        box._do = lambda fn, *args, timeout=20, first=False: (_ for _ in ()).throw(mailbox.OutlookBusy(started=True))
        with self.assertRaises(MailError) as caught:
            box.act({"what": "delete", "id": ID})
        self.assertIn("before you try it again", str(caught.exception))
        box._do = lambda fn, *args, timeout=20, first=False: (_ for _ in ()).throw(mailbox.OutlookBusy(started=False))
        with self.assertRaises(MailError) as caught:
            box.act({"what": "delete", "id": ID})
        self.assertIn("nothing was changed", str(caught.exception))


class MoreFolders(unittest.TestCase):
    def test_outbox_deleted_and_junk_can_be_listed_and_nothing_else(self):
        box = OutlookMailbox(read_ahead=False)
        asked = []
        box._do = lambda fn, *args, timeout=20, first=False: asked.append(args[0]) or {"folder": args[0], "mail": [], "more": False}
        for folder in ("outbox", "deleted", "junk"):
            self.assertEqual(box.list(folder)["folder"], folder)
        self.assertEqual(asked, ["outbox", "deleted", "junk"])
        for bad in ("calendar", "contacts", "../inbox", ""):
            with self.assertRaises(MailError):
                box.list(bad)

    def test_they_are_not_read_ahead(self):
        self.assertEqual(mailbox.FOLDERS, ("inbox", "sent", "drafts"))
        self.assertEqual(set(mailbox.WARM_MESSAGES), set(mailbox.FOLDERS))
        self.assertEqual(set(mailbox.OL_FOLDER), set(mailbox.ALL_FOLDERS))
        self.assertEqual(set(mailbox.ORDER), set(mailbox.ALL_FOLDERS))


class Demo(unittest.TestCase):
    def test_reply_all_shows_and_reaches_everyone_but_you(self):
        box = DemoMailbox()
        first = box.get(box.list("inbox")["mail"][0]["id"])
        plan = first["answer"]["replyAll"]
        self.assertEqual(([one["name"] for one in plan["to"]], [one["name"] for one in plan["cc"]]), (["Mateo Silva"], ["Priya Nair"]))
        box.send({"mode": "replyAll", "id": first["id"], "recipients": "exact", "body": "ok",
                  "to": [plan["to"][0]["address"]], "cc": [], "bcc": ["jonas.weber@lumenworks.example"]})
        sent = box.get(box.list("sent")["mail"][0]["id"])
        self.assertEqual((sent["to"], sent["cc"], sent["bcc"]), ("mateo.silva@lumenworks.example", "", "jonas.weber@lumenworks.example"))
        self.assertEqual(sent["people"]["bcc"], [{"name": "Jonas Weber", "address": "jonas.weber@lumenworks.example"}])

    def test_files_go_along_and_a_forward_can_leave_one_behind(self):
        box = DemoMailbox()
        first = box.list("inbox")["mail"][0]["id"]
        box.send({"mode": "forward", "id": first, "recipients": "exact", "to": ["a@mail.example"], "body": "",
                  "drop": [{"index": 2, "name": "cam-scuffing.jpg"}], "files": [{"name": "notes.txt", "data": b"12345"}]})
        sent = box.get(box.list("sent")["mail"][0]["id"])
        self.assertEqual([(file["name"], file["size"]) for file in sent["attachments"]], [("hinge-B-torque.xlsx", 48213), ("notes.txt", 5)])

    def test_the_outbox_the_calendar_an_invitation_and_delete(self):
        box = DemoMailbox()
        self.assertEqual(box.state()["counts"]["outbox"], 1)
        waiting = box.list("outbox")["mail"][0]
        self.assertEqual(waiting["to"], "Okafor Supplies")
        events = box.calendar()["events"]
        self.assertGreaterEqual(len(events), 6)
        self.assertEqual([event["start"] for event in events], sorted(event["start"] for event in events))
        self.assertTrue(all("body" not in event for event in events), "the list is short; the text comes with the entry")
        invitation = next(mail for mail in box.list("inbox", limit=100)["mail"] if mail["kind"] == "meeting")
        invite = box.get(invitation["id"])["invite"]
        self.assertEqual((invite["response"], invite["invited"]), ("none", True))
        self.assertEqual(box.act({"what": "respond", "id": invitation["id"], "answer": "tentative"})["responded"], "tentative")
        self.assertEqual(box.event(invite["id"])["response"], "tentative")
        self.assertIn("high-speed camera", box.event(invite["id"])["body"])
        with self.assertRaises(MailError):
            box.act({"what": "respond", "id": events[0]["id"], "answer": "accept"})  # your own stand-up
        box.act({"what": "delete", "id": waiting["id"]})
        self.assertEqual((box.state()["counts"]["outbox"], box.list("deleted")["mail"][0]["id"]), (0, waiting["id"]))
        with self.assertRaises(MailError):
            box.act({"what": "delete", "id": waiting["id"]})
        self.assertEqual(box.calendar(offset=400)["events"], [])


if __name__ == "__main__":
    unittest.main()
