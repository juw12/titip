"""The rest of what 0.9.0 brought from Outlook, against a fake Outlook: an answer nobody re-addressed,
your own folders, move, flag, categories, read and unread, importance, a search in the text of mail,
the signature, and calendar entries made, changed and taken out."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from tests.test_more_of_outlook import ID, App, Appointment, Mail, Message, Namespace, Recipients, Source, at, outlook_with, went_to
from titip import mailbox
from titip.mailbox import (
    DemoMailbox,
    MailError,
    OutlookBusy,
    OutlookMailbox,
    clean_act_request,
    clean_plan_request,
    clean_send_request,
    local_time,
    read_signatures,
    split_categories,
)

FOLDER = "f.0123456789.ABCDEF0123456789AB"


class AnAnswerNobodyReaddressed(unittest.TestCase):
    """Most answers go to whom Outlook addressed them. Those take the steps they took before 0.9.0."""

    def test_untouched_on_the_phone_the_recipients_are_not_gone_through_at_all(self):
        source = Source()
        box = outlook_with(source)
        box._address = lambda mail, request: self.fail("the recipients were gone through")
        box._send(clean_send_request({"mode": "replyAll", "id": ID, "recipients": "exact", "kept": True, "body": "Agreed.",
                                      "to": ["boss@example.com", "anna@example.com"], "cc": ["tomas@example.com"]}))
        self.assertEqual(went_to(source.made), [("Boss, The", 1), ("Anna Okafor", 1), ("Hardware Team", 2), ("tomas@example.com", 2)])
        self.assertEqual(source.made.calls, ["send"])

    def test_only_a_page_that_says_so_and_only_an_answer(self):
        self.assertFalse(clean_send_request({"mode": "reply", "id": ID, "body": "x", "kept": "yes"})["kept"])
        source = Source()
        outlook_with(source)._send(clean_send_request({"mode": "forward", "id": ID, "recipients": "exact", "kept": True, "to": ["a@example.com"], "body": ""}))
        self.assertEqual(went_to(source.made), [("a@example.com", 1)], "a forward has nobody until the phone names somebody")

    def test_importance(self):
        box = outlook_with()
        box._send(clean_send_request({"mode": "new", "to": "a@example.com", "body": "Now.", "importance": "high"}))
        self.assertEqual(box._app.created[0].Importance, 2)
        box._send(clean_send_request({"mode": "new", "to": "a@example.com", "body": "Whenever.", "importance": "urgent!!"}))
        self.assertFalse(hasattr(box._app.created[1], "Importance"), "anything else leaves it as Outlook made it")


class Kept(Message):
    """A message things can be done to."""

    def __init__(self, unread=True, receipt=False, tasks=True):
        super().__init__()
        self.UnRead, self.ReadReceiptRequested, self.FlagStatus, self.Categories = unread, receipt, 0, ""
        self.tasks, self.calls = tasks, []

    def Save(self):
        self.calls.append("save")

    def MarkAsTask(self, when):
        if not self.tasks:
            raise RuntimeError("this account keeps no to-do list")
        self.calls.append(f"task{when}")

    def ClearTaskFlag(self):
        if not self.tasks:
            raise RuntimeError("this account keeps no to-do list")
        self.calls.append("clear")

    def Move(self, folder):
        self.calls.append(("move", folder))


class Folders(Namespace):
    def __init__(self):
        super().__init__([])
        self.Stores = []
        self.Categories = []

    def GetFolderFromID(self, entry_id, store_id):
        if entry_id.startswith("DEAD"):
            raise RuntimeError("no such folder")
        return ("folder", entry_id, store_id)

    def GetDefaultFolder(self, number):
        return ("default", number)


class DoneToAMessage(unittest.TestCase):
    def act(self, item, **payload):
        box = OutlookMailbox(read_ahead=False)
        box._item = lambda mail_id: item
        box._ns = Folders()
        box._stores["0123456789"] = "STORE"
        return box._act(clean_act_request({"id": ID, **payload}))

    def test_read_on_the_phone_is_read_in_outlook(self):
        mail = Kept()
        self.assertEqual(self.act(mail, what="read"), {"ok": True, "unread": False})
        self.assertEqual((mail.UnRead, mail.calls), (False, ["save"]))
        self.assertEqual(self.act(mail, what="read"), {"ok": True, "unread": False})
        self.assertEqual(mail.calls, ["save"], "what is read already is not saved again")

    def test_a_mail_that_asks_for_a_read_receipt_stays_unread_in_outlook(self):
        mail = Kept(receipt=True)
        self.assertEqual(self.act(mail, what="read"), {"ok": True, "unread": True, "left": "receipt"})
        self.assertEqual((mail.UnRead, mail.calls), (True, []), "Outlook is never made to ask about a receipt with nobody at the PC")

    def test_mark_as_unread(self):
        mail = Kept(unread=False)
        self.assertEqual(self.act(mail, what="unread"), {"ok": True, "unread": True})
        self.assertEqual((mail.UnRead, mail.calls), (True, ["save"]))

    def test_flag_the_way_outlook_does_and_the_plain_way_where_there_is_no_to_do_list(self):
        mail = Kept()
        self.assertEqual(self.act(mail, what="flag"), {"ok": True, "flagged": True})
        self.act(mail, what="unflag")
        self.assertEqual(mail.calls, ["task4", "save", "clear", "save"])
        plain = Kept(tasks=False)
        self.act(plain, what="flag")
        self.assertEqual((plain.FlagStatus, plain.calls), (2, ["save"]))
        self.act(plain, what="unflag")
        self.assertEqual(plain.FlagStatus, 0)

    def test_move_to_a_folder_of_your_own_or_back_to_the_inbox(self):
        mail = Kept()
        self.assertEqual(self.act(mail, what="move", to=FOLDER), {"ok": True, "moved": True})
        self.act(mail, what="move", to="inbox")
        self.assertEqual(mail.calls, [("move", ("folder", "ABCDEF0123456789AB", "STORE")), ("move", ("default", mailbox.OL_INBOX))])
        with self.assertRaises(MailError):
            self.act(mail, what="move", to="f.0123456789.DEAD0123456789ABCD")
        with self.assertRaises(MailError):
            self.act(mail, what="move", to="f.ffffffffff.ABCDEF0123456789AB")  # a data file that is not open
        for bad in ("outbox", "calendar", "", "../x", "f.short.AB"):
            with self.assertRaises(MailError, msg=bad):
                clean_act_request({"what": "move", "id": ID, "to": bad})
        with self.assertRaises(MailError):
            self.act(Appointment("AA", "Dentist", at(24, 14), at(24, 15)), what="move", to="inbox")

    def test_categories_are_outlooks_own_names_in_one_text(self):
        mail = Kept()
        self.act(mail, what="categorize", categories=["Supplier", "  To   read ", "Supplier", "", "Bad;name", "Bad,name"])
        self.assertEqual(split_categories(mail.Categories), ["Supplier", "To read"])
        self.assertEqual(mail.calls, ["save"])
        self.act(mail, what="categorize", categories=[])
        self.assertEqual(mail.Categories, "")
        self.assertEqual(split_categories("Customer; To read,Waiting"), ["Customer", "To read", "Waiting"])
        with self.assertRaises(MailError):
            clean_act_request({"what": "categorize", "id": ID, "categories": "Supplier"})

    def test_reading_a_message_says_its_flag_importance_and_categories(self):
        box = OutlookMailbox(read_ahead=False)
        box._who = ("me@example.com", "Me")
        mail = Kept()
        mail.FlagStatus, mail.Importance, mail.Categories = 2, 2, "Supplier; To read"
        box._item = lambda mail_id: mail
        got = box._get(ID)
        self.assertEqual((got["flagged"], got["important"], got["categories"]), (True, True, ["Supplier", "To read"]))

    def test_marking_as_read_waits_its_turn_and_throws_no_list_away(self):
        box = OutlookMailbox(read_ahead=False)
        asked = []
        box._do = lambda fn, *args, timeout=20, first=False: asked.append((fn.__name__, first)) or {"ok": True, "unread": False}
        box._lists[("inbox", 40)] = (0.0, "", {"mail": [{"id": ID, "unread": True}, {"id": "s1.other", "unread": True}]})
        box._messages[ID] = {"id": ID, "body": "x", "html": ""}
        box.act({"what": "read", "id": ID})
        self.assertEqual(asked, [("_act", False)])
        self.assertEqual([row["unread"] for row in box._lists[("inbox", 40)][2]["mail"]], [False, True])
        self.assertIn(ID, box._messages)
        box._do = lambda fn, *args, timeout=20, first=False: (_ for _ in ()).throw(OutlookBusy())
        with self.assertRaises(OutlookBusy):
            box.act({"what": "read", "id": ID})  # the phone does not wait for it and is not told a story


class Shifting(Appointment):
    """An Outlook that takes a time as if it were written on another clock, `hours` away."""

    def __init__(self, hours):
        self.__dict__["hours"] = hours
        super().__init__("NEW", "", at(1), at(1))

    def __setattr__(self, name, value):
        if name in ("Start", "End") and "Subject" in self.__dict__ and isinstance(value, dt.datetime):
            value = value + dt.timedelta(hours=self.hours)
        self.__dict__[name] = value


def planned(app_item):
    app_item.Recipients = Recipients()
    app_item.calls = []
    app_item.Send = lambda: app_item.calls.append("send")
    app_item.Save = lambda: app_item.calls.append("save")
    app_item.Delete = lambda: app_item.calls.append("delete")
    app_item.Close = lambda how: app_item.calls.append(f"close{how}")
    return app_item


class CalendarEntries(unittest.TestCase):
    def setUp(self):
        self.made = []

    def box(self, existing=None, hours=0):
        box = OutlookMailbox(read_ahead=False)
        outer = self

        class Calendar(App):
            def CreateItem(self, kind):
                outer.assertEqual(kind, 1)
                outer.made.append(planned(Shifting(hours)))
                return outer.made[-1]

        box._app = Calendar()
        box._item = lambda event_id: existing
        return box

    def order(self, **more):
        start = at(24, 14).astimezone().astimezone(dt.timezone.utc)
        return {"what": "save", "subject": "Gasket fit check", "location": "Lab 2", "body": "Bring both units.",
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": (start + dt.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ"), **more}

    def test_the_moment_the_phone_names_is_put_on_this_pcs_clock(self):
        self.assertEqual(local_time(self.order()["start"]), at(24, 14))
        request = clean_plan_request(self.order())
        self.assertEqual((request["start"], request["end"]), (at(24, 14), at(24, 14, 45)))

    def test_an_entry_for_yourself_is_saved_and_nobody_is_told(self):
        box = self.box()
        self.assertEqual(box._plan(clean_plan_request(self.order())), {"ok": True, "saved": True, "told": False})
        entry = self.made[0]
        self.assertEqual((entry.Subject, entry.Location, entry.Body, entry.Start, entry.End, entry.AllDayEvent, entry.calls),
                         ("Gasket fit check", "Lab 2", "Bring both units.", at(24, 14), at(24, 14, 45), False, ["save"]))

    def test_with_people_it_is_a_meeting_and_they_are_invited(self):
        box = self.box()
        result = box._plan(clean_plan_request(self.order(**{"with": ["priya@example.com", "jonas@example.com"]})))
        entry = self.made[0]
        self.assertEqual((result["told"], entry.MeetingStatus, went_to(entry), entry.calls), (True, 1, [("priya@example.com", 1), ("jonas@example.com", 1)], ["send"]))

    def test_somebody_outlook_does_not_know_stops_it_and_nothing_is_left_behind(self):
        box = self.box()
        with self.assertRaises(MailError) as caught:
            box._plan(clean_plan_request(self.order(**{"with": ["nobody"]})))
        self.assertIn("nobody", str(caught.exception))
        self.assertEqual(self.made[0].calls, ["close1"])

    def test_a_time_outlook_shifts_on_the_way_in_is_set_again_until_it_reads_back_right(self):
        for hours in (7, -8):
            self.made.clear()
            self.box(hours=hours)._plan(clean_plan_request(self.order()))
            self.assertEqual((self.made[0].Start, self.made[0].End), (at(24, 14), at(24, 14, 45)), hours)

    def test_all_day(self):
        self.box()._plan(clean_plan_request({"what": "save", "subject": "Trade fair", "allDay": True, "day": "2026-11-24", "days": 2}))
        entry = self.made[0]
        self.assertEqual((entry.AllDayEvent, entry.Start, entry.End), (True, at(24), at(26)))

    def test_what_makes_no_sense_is_refused_before_outlook_is_asked(self):
        for bad in ({"subject": "  "}, {"start": "tomorrow"}, {"end": "2026-11-20T00:00:00Z"}, {"end": "2027-11-24T00:00:00Z"}, {"what": "explode"}):
            with self.assertRaises(MailError, msg=bad):
                clean_plan_request({**self.order(), **bad})
        with self.assertRaises(MailError):
            clean_plan_request({"what": "save", "subject": "x", "allDay": True, "day": "24/11/2026"})
        with self.assertRaises(MailError):
            clean_plan_request({"what": "remove"})

    def test_your_own_entry_is_changed_and_the_people_invited_are_told(self):
        mine = planned(Appointment("AA", "Old title", at(24, 9), at(24, 10)))
        mine.Body = "Bring both units.\r\n"
        self.assertEqual(self.box(mine)._plan(clean_plan_request(self.order(id=ID))), {"ok": True, "saved": True, "told": False})
        self.assertEqual((mine.Subject, mine.Start, mine.End, mine.calls, mine.Body), ("Gasket fit check", at(24, 14), at(24, 14, 45), ["save"], "Bring both units.\r\n"))
        meeting = planned(Appointment("AB", "Review", at(24, 9), at(24, 10), meeting=1, response=1))
        self.assertTrue(self.box(meeting)._plan(clean_plan_request(self.order(id=ID, **{"with": ["late@example.com"]})))["told"])
        self.assertEqual((meeting.calls, went_to(meeting)), (["send"], []), "who is invited is not changed from the phone")
        self.assertEqual(self.made, [])

    def test_somebody_elses_meeting_and_a_repeating_entry_are_not_changed(self):
        theirs = planned(Appointment("AC", "Theirs", at(24, 9), at(24, 10), meeting=3, response=3))
        series = planned(Appointment("AD", "Stand-up", at(24, 9), at(24, 10), IsRecurring=True))
        for entry in (theirs, series, Message()):
            for order in (self.order(id=ID), {"what": "remove", "id": ID}):
                with self.assertRaises(MailError):
                    self.box(entry)._plan(clean_plan_request(order))
        self.assertEqual((theirs.calls, series.calls), ([], []))

    def test_taking_out_an_entry_and_cancelling_a_meeting(self):
        mine = planned(Appointment("AA", "Dentist", at(24, 9), at(24, 10)))
        self.assertEqual(self.box(mine)._plan(clean_plan_request({"what": "remove", "id": ID})), {"ok": True, "removed": True, "told": False})
        self.assertEqual(mine.calls, ["delete"])
        meeting = planned(Appointment("AB", "Review", at(24, 9), at(24, 10), meeting=1, response=1))
        self.assertTrue(self.box(meeting)._plan(clean_plan_request({"what": "remove", "id": ID}))["told"])
        self.assertEqual((meeting.MeetingStatus, meeting.calls), (5, ["send", "delete"]), "the people invited get the cancellation first")

    def test_it_goes_ahead_of_the_reading_ahead_and_the_calendar_is_read_again(self):
        box = OutlookMailbox(read_ahead=False)
        asked = []
        box._do = lambda fn, *args, timeout=20, first=False: asked.append((fn.__name__, first)) or {"ok": True}
        box._calendars[(0, 14)] = (0.0, {})
        box.plan(self.order())
        self.assertEqual((asked, box._calendars), ([("_plan", True)], {}))
        box._do = lambda fn, *args, timeout=20, first=False: (_ for _ in ()).throw(OutlookBusy(started=True))
        with self.assertRaises(MailError) as caught:
            box.plan(self.order())
        self.assertIn("so it is not there twice", str(caught.exception))


class Leaf:
    def __init__(self, name, entry, kind=0, unread=0, inside=()):
        self.Name, self.EntryID, self.DefaultItemType, self.UnReadItemCount, self.Folders = name, entry, kind, unread, list(inside)


class Store:
    def __init__(self, store_id, name, folders, broken=False):
        self.StoreID, self.DisplayName, self.folders, self.broken = store_id, name, folders, broken

    def GetRootFolder(self):
        if self.broken:
            raise RuntimeError("the data file is not there")
        return Leaf("root", "ROOT", inside=self.folders)


class Mailboxes:
    def __init__(self, stores, categories=("Customer", "Supplier")):
        self.Stores = stores
        self.Categories = [type("Category", (), {"Name": name})() for name in categories]

    def GetDefaultFolder(self, number):
        return type("Default", (), {"EntryID": f"DEFAULT{number}", "StoreID": "MAIN"})()


class YourOwnFolders(unittest.TestCase):
    def box(self, *stores):
        box = OutlookMailbox(read_ahead=False)
        box._ns = Mailboxes(list(stores))
        return box

    def test_every_mail_folder_of_every_data_file_with_where_it_is(self):
        main = Store("MAIN", "Mailbox", [
            Leaf("Inbox", f"DEFAULT{mailbox.OL_INBOX}", inside=[Leaf("Hinge project", "AA" * 12, unread=2, inside=[Leaf("Tests", "AB" * 12)])]),
            Leaf("Calendar", "CA" * 12, kind=1, inside=[Leaf("Team calendar", "CB" * 12, kind=1)]),
            Leaf("Suppliers", "AC" * 12),
            Leaf("Deleted Items", f"DEFAULT{mailbox.OL_DELETED}"),
        ])
        archive = Store("OLD", "Archive 2025", [Leaf("Inbox", "BA" * 12), Leaf("Projects", "BB" * 12)])
        got = self.box(archive, Store("GONE", "Offline", [], broken=True), main)._folders_now()
        listed = [(folder["name"], folder["in"], folder["file"], folder["unread"]) for folder in got["folders"]]
        self.assertEqual(listed, [
            ("Hinge project", "Inbox", "", 2), ("Tests", "Inbox / Hinge project", "", 0), ("Suppliers", "", "", 0),
            ("Inbox", "", "Archive 2025", 0), ("Projects", "", "Archive 2025", 0),
        ], "your mailbox first, Outlook's own folders and what is not mail left out, a broken data file skipped")
        archive.folders[1].UnReadItemCount = 7
        again = self.box(archive, main)._folders_now()["folders"]
        self.assertEqual([folder["unread"] for folder in again], [2, 0, 0, 0, 0], "unread mail is counted in your mailbox only")
        self.assertTrue(all(mailbox.FOLDER_ID.fullmatch(folder["id"]) for folder in got["folders"]))
        self.assertEqual((got["categories"], got["cut"]), (["Customer", "Supplier"], False))

    def test_the_list_is_bounded_in_number_and_in_depth(self):
        saved = mailbox.FOLDERS_SHOWN, mailbox.FOLDERS_DEEP
        mailbox.FOLDERS_SHOWN, mailbox.FOLDERS_DEEP = 4, 2
        self.addCleanup(lambda: (setattr(mailbox, "FOLDERS_SHOWN", saved[0]), setattr(mailbox, "FOLDERS_DEEP", saved[1])))
        deep = Leaf("One", "C1", inside=[Leaf("Two", "C2", inside=[Leaf("Three", "C3")])])
        got = self.box(Store("MAIN", "Mailbox", [deep]))._folders_now()
        self.assertEqual([folder["name"] for folder in got["folders"]], ["One", "Two"])
        got = self.box(Store("MAIN", "Mailbox", [Leaf(f"F{n}", f"D{n}") for n in range(9)]))._folders_now()
        self.assertEqual((len(got["folders"]), got["cut"]), (4, True))

    def test_asked_once_an_hour_and_the_old_answer_serves_while_outlook_is_busy(self):
        box = OutlookMailbox(read_ahead=False)
        asked = []

        def outlook(fn, *args, timeout=20, first=False):
            asked.append(fn.__name__)
            if len(asked) > 1:
                raise OutlookBusy()
            return {"folders": [], "categories": []}

        box._do = outlook
        box.folders()
        box.folders()
        self.assertEqual(asked, ["_folders_now"])
        box._folder_list = (box._folder_list[0] - mailbox.FOLDERS_FRESH_SECONDS, box._folder_list[1])
        self.assertEqual(box.folders(), {"folders": [], "categories": []})
        box._folder_list = None
        with self.assertRaises(OutlookBusy):
            box.folders()

    def test_a_folder_of_your_own_is_listed_like_any_other_and_only_itself(self):
        box = OutlookMailbox(read_ahead=False)
        box._ns = Folders()
        box._ns.Stores = [type("S", (), {"StoreID": "STORE"})()]
        key = box._store_key("STORE")
        box._stores.clear()  # as after a restart: the phone still holds the id
        folder_id = f"f.{key}.ABCDEF0123456789AB"
        self.assertEqual(box._mail_folders(folder_id), [("folder", "ABCDEF0123456789AB", "STORE")])
        asked = []
        box._do = lambda fn, *args, timeout=20, first=False: asked.append(args) or {"folder": args[0], "mail": [], "more": False}
        box.list(folder_id)
        box.list(folder_id, query="torque", deep=True)
        self.assertEqual(asked, [(folder_id, 40, 0, ""), (folder_id, 40, 0, "torque", True)])


class Table:
    """Outlook's table of one folder: the columns asked for, in the order they were added."""

    def __init__(self, rows, refuses=()):
        self.rows, self.refuses, self.added, self.Columns = rows, refuses, [], self

    def RemoveAll(self):
        self.added.clear()

    def Add(self, name):
        if name in self.refuses:
            raise RuntimeError("this folder does not have it")
        self.added.append(name)

    def Sort(self, order, descending):
        self.sorted = order

    def GetRowCount(self):
        return len(self.rows)

    def GetArray(self, count):
        return [[row.get(name) for name in self.added] for row in self.rows[:count]]


class TableFolder:
    StoreID = "STORE"

    def __init__(self, rows, refuses=(), indexed=False, no_text_search=False):
        self.rows, self.refuses, self.no_text_search, self.filters = rows, refuses, no_text_search, []
        self.Store = type("S", (), {"IsInstantSearchEnabled": indexed})()

    def GetTable(self, text="", kind=0):
        self.filters.append(text)
        if self.no_text_search and mailbox.BODY_TEXT in text:
            raise RuntimeError("cannot search text here")
        return Table(self.rows, self.refuses)


ROW = {"EntryID": "ABCDEF0123456789", "Subject": "Quote", "SenderName": "Okafor", "MessageClass": "IPM.Note", "UnRead": True,
       mailbox.P_DELIVERED: dt.datetime(2026, 11, 20, 9, 0), mailbox.P_HAS_ATTACH: True, mailbox.P_DISPLAY_TO: "Me",
       mailbox.P_PREVIEW: "Dear Rin,  please find", mailbox.P_FLAG: 2, "Importance": 2, mailbox.P_MODIFIED: dt.datetime(2026, 11, 20, 9, 5)}


class ListingAFolder(unittest.TestCase):
    def listed(self, folder, name="inbox", query="", deep=False):
        box = OutlookMailbox(read_ahead=False)
        box._mail_folders = lambda wanted: [folder]
        return box._list(name, 40, 0, query, deep)["mail"]

    def test_a_row_says_flagged_and_important_next_to_what_it_said_before(self):
        row = self.listed(TableFolder([ROW]))[0]
        self.assertEqual((row["subject"], row["from"], row["unread"], row["attachments"], row["to"], row["time"], row["preview"]),
                         ("Quote", "Okafor", True, True, "Me", "2026-11-20T09:00:00Z", "Dear Rin, please find"))
        self.assertEqual((row["flagged"], row["important"]), (True, True))

    def test_a_folder_that_cannot_tell_one_of_them_still_lists_the_rest_in_the_right_places(self):
        for refuses in ([mailbox.P_PREVIEW], [mailbox.P_FLAG], ["Importance"], [mailbox.P_PREVIEW, mailbox.P_FLAG, "Importance"]):
            row = self.listed(TableFolder([ROW], refuses))[0]
            self.assertEqual(row["preview"], "" if mailbox.P_PREVIEW in refuses else "Dear Rin, please find", refuses)
            self.assertEqual(row["flagged"], mailbox.P_FLAG not in refuses, refuses)
            self.assertEqual(row["important"], "Importance" not in refuses, refuses)
            self.assertEqual((row["time"], row["to"]), ("2026-11-20T09:00:00Z", "Me"), refuses)

    def test_a_folder_of_your_own_places_what_was_never_delivered_by_when_it_changed(self):
        draft = {**ROW, mailbox.P_DELIVERED: None}
        self.assertEqual(self.listed(TableFolder([draft]), FOLDER)[0]["time"], "2026-11-20T09:05:00Z")
        self.assertEqual(self.listed(TableFolder([draft]), FOLDER)[0]["preview"], "Dear Rin, please find")

    def test_a_search_looks_in_the_text_only_when_asked_and_by_word_where_outlook_keeps_an_index(self):
        plain = TableFolder([ROW])
        self.listed(plain, query="gasket")
        self.assertNotIn(mailbox.BODY_TEXT, plain.filters[0])
        slow = TableFolder([ROW])
        self.listed(slow, query="gasket", deep=True)
        self.assertIn(f'"{mailbox.BODY_TEXT}" like \'%gasket%\'', slow.filters[0])
        quick = TableFolder([ROW], indexed=True)
        self.listed(quick, query="gasket", deep=True)
        self.assertIn(f'"{mailbox.BODY_TEXT}" ci_phrasematch \'gasket\'', quick.filters[0])
        self.assertIn('"urn:schemas:httpmail:subject" ci_phrasematch \'gasket\'', quick.filters[0])

    def test_a_data_file_that_cannot_search_text_is_searched_the_way_it_always_was(self):
        folder = TableFolder([ROW], no_text_search=True)
        rows = self.listed(folder, query="gasket", deep=True)
        self.assertEqual((len(rows), len(folder.filters)), (1, 2))
        self.assertNotIn(mailbox.BODY_TEXT, folder.filters[1])


class Signatures(unittest.TestCase):
    def folder(self, **files):
        place = tempfile.TemporaryDirectory()
        self.addCleanup(place.cleanup)
        for name, data in files.items():
            (Path(place.name) / f"{name}.txt").write_bytes(data)
        (Path(place.name) / "Work.htm").write_bytes(b"<html><body><b>formatted</b></body></html>")
        return Path(place.name)

    def test_the_one_outlook_is_set_to_for_a_new_mail_and_for_an_answer(self):
        folder = self.folder(Work="Rin Aoki\r\nHardware\r\n".encode("utf-16"), Short=b"\xef\xbb\xbfRin", Other=b"Somebody else")
        got = read_signatures(folder, lambda: ("", "Work", "Short"))
        self.assertEqual(got, {"new": "Rin Aoki\nHardware", "reply": "Rin"})

    def test_one_signature_and_no_setting_is_the_one_for_a_new_mail(self):
        self.assertEqual(read_signatures(self.folder(Only=b"Rin"), lambda: ("", "", "")), {"new": "Rin", "reply": ""})
        self.assertEqual(read_signatures(self.folder(One=b"a", Two=b"b"), lambda: ("", "", "")), {"new": "", "reply": ""})

    def test_none_is_not_a_problem(self):
        self.assertEqual(read_signatures(Path("/no/such/folder"), lambda: ("", "Work", "")), {"new": "", "reply": ""})
        self.assertEqual(read_signatures(None, lambda: 1 / 0), {"new": "", "reply": ""})
        self.assertEqual(OutlookMailbox(read_ahead=False).signature().keys(), {"new", "reply"})


class Demo(unittest.TestCase):
    def test_a_folder_of_your_own_move_flag_categories_and_unread(self):
        box = DemoMailbox()
        own = box.folders()
        hinge = own["folders"][0]
        self.assertEqual((hinge["name"], hinge["in"], own["categories"][1]), ("Hinge project", "Inbox", "Supplier"))
        self.assertEqual(len(box.list(hinge["id"])["mail"]), 2)
        first = box.list("inbox")["mail"][0]
        self.assertTrue(first["important"] and first["unread"])
        box.act({"what": "read", "id": first["id"]})
        box.act({"what": "flag", "id": first["id"]})
        box.act({"what": "categorize", "id": first["id"], "categories": ["Customer"]})
        box.act({"what": "move", "id": first["id"], "to": hinge["id"]})
        moved = next(mail for mail in box.list(hinge["id"])["mail"] if mail["id"] == first["id"])
        self.assertEqual((moved["unread"], moved["flagged"], box.get(first["id"])["categories"]), (False, True, ["Customer"]))
        box.act({"what": "unread", "id": first["id"]})
        self.assertEqual(box.folders()["folders"][0]["unread"], 1)
        with self.assertRaises(MailError):
            box.act({"what": "move", "id": first["id"], "to": "f.0000000000.ABCDEF0123456789AB"})

    def test_a_search_in_the_text_and_the_signature(self):
        box = DemoMailbox()
        self.assertEqual(box.list("inbox", query="shore")["mail"], [])
        self.assertEqual(len(box.list("inbox", query="shore", deep=True)["mail"]), 1)
        self.assertTrue(box.signature()["new"].startswith("Rin Aoki"))

    def test_an_entry_made_changed_and_taken_out(self):
        box = DemoMailbox()
        day = (dt.date.today() + dt.timedelta(days=2)).isoformat()
        box.plan({"what": "save", "subject": "Fair", "allDay": True, "day": day, "days": 1, "with": ["priya.nair@lumenworks.example"]})
        fair = next(event for event in box.calendar()["events"] if event["subject"] == "Fair")
        self.assertEqual((fair["day"], fair["allDay"], fair["mine"], fair["meeting"]), (day, True, True, True))
        box.plan({"what": "save", "id": fair["id"], "subject": "Trade fair", "allDay": True, "day": day, "days": 2})
        self.assertEqual(box.event(fair["id"])["subject"], "Trade fair")
        theirs = next(event for event in box.calendar()["events"] if event["invited"])
        with self.assertRaises(MailError):
            box.plan({"what": "remove", "id": theirs["id"]})
        self.assertEqual(box.plan({"what": "remove", "id": fair["id"]}), {"ok": True, "removed": True, "told": True})


if __name__ == "__main__":
    unittest.main()
