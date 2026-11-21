"""What 0.10.0 brought from Outlook, against a fake Outlook: formatted text, a read receipt, reminders,
entries that repeat, who is invited, other calendars, rules, and folders and categories made."""
import datetime as dt
import unittest

from tests.test_more_of_outlook import ID, App, Appointment, Message, Recipient, Recipients, at, outlook_with, went_to
from tests.test_rest_of_outlook import FOLDER, Shifting, planned
from titip import mailbox
from titip.mailbox import (
    DemoMailbox,
    MailError,
    OutlookBusy,
    OutlookMailbox,
    clean_arrange_request,
    clean_plan_request,
    clean_send_request,
    text_to_html,
)


class FormattedText(unittest.TestCase):
    def test_marks_become_formatting_only_when_the_phone_says_so(self):
        text = "Hi **all**, *please* read __this__ <now>."
        self.assertIn("Hi **all**, *please* read __this__ &lt;now&gt;.", text_to_html(text))
        self.assertIn("Hi <b>all</b>, <i>please</i> read <u>this</u> &lt;now&gt;.", text_to_html(text, True))

    def test_signs_that_are_not_marks_stay_as_typed(self):
        for plain in ("2 * 3 * 4", "a*b*c", "snake_case__name", "** not bold **", "a * b", "5 stars: *****"):
            self.assertIn(f">{plain}<", text_to_html(plain, True), plain)

    def test_lines_that_start_a_list_become_one(self):
        made = text_to_html("To do:\n- one\n- **two**\n\n1. first\n2) second\nDone.", True)
        self.assertIn("To do:<br><ul", made)
        self.assertIn("<li>one</li><li><b>two</b></li></ul><br><ol", made)
        self.assertIn("<li>first</li><li>second</li></ol>Done.</div>", made)

    def test_nothing_typed_can_become_markup(self):
        made = text_to_html("**<script>alert(1)</script>** and *<img src=x onerror=y>*", True)
        self.assertNotIn("<script", made)
        self.assertNotIn("<img", made)
        self.assertIn("<b>&lt;script&gt;alert(1)&lt;/script&gt;</b>", made)

    def test_a_mail_goes_out_formatted_and_can_ask_for_a_read_receipt(self):
        box = outlook_with()
        box._send(clean_send_request({"mode": "new", "to": "a@example.com", "body": "**Stop** here", "format": "marks", "receipt": True}))
        mail = box._app.created[0]
        self.assertIn("<b>Stop</b> here", mail.HTMLBody)
        self.assertTrue(mail.ReadReceiptRequested)
        box._send(clean_send_request({"mode": "new", "to": "a@example.com", "body": "**Stop** here", "format": "html", "receipt": "yes"}))
        self.assertIn("**Stop** here", box._app.created[1].HTMLBody)
        self.assertFalse(hasattr(box._app.created[1], "ReadReceiptRequested"))


class Planning(unittest.TestCase):
    """An Outlook that makes calendar entries, or holds one that is there; and what the phone orders."""

    def setUp(self):
        self.made = []

    def box(self, existing=None, hours=0):
        box = OutlookMailbox(read_ahead=False)
        outer = self

        class Calendar(App):
            def CreateItem(self, kind):
                outer.made.append(planned(Shifting(hours)))
                return outer.made[-1]

        box._app = Calendar()
        box._item = lambda event_id: existing
        return box

    def order(self, **more):
        start = at(24, 14).astimezone().astimezone(dt.timezone.utc)
        return {"what": "save", "subject": "Gasket fit check", "location": "Lab 2", "body": "Bring both units.",
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": (start + dt.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ"), **more}


class Pattern:
    def __init__(self, series, shift=dt.timedelta(0)):
        self.series, self.shift, self.asked = series, shift, []

    def GetOccurrence(self, when):
        self.asked.append(when)
        for one in self.series.times:
            if one.Start == when + self.shift:
                return one
        raise RuntimeError("no such occurrence")


def series_of(*starts, meeting=0, shift=dt.timedelta(0)):
    series = planned(Appointment("AD", "Stand-up", starts[0], starts[0] + dt.timedelta(minutes=20), meeting=meeting, IsRecurring=True))
    series.times = [planned(Appointment("AD", "Stand-up", start, start + dt.timedelta(minutes=20), meeting=meeting, IsRecurring=True)) for start in starts]
    series.pattern = Pattern(series, shift)
    series.GetRecurrencePattern = lambda: series.pattern
    return series


class Reminders(Planning):
    def test_a_reminder_is_set_taken_away_or_left_as_outlook_sets_it(self):
        self.box()._plan(clean_plan_request(self.order(reminder=30)))
        self.assertEqual((self.made[0].ReminderSet, self.made[0].ReminderMinutesBeforeStart), (True, 30))
        self.box()._plan(clean_plan_request(self.order(reminder="none")))
        self.assertEqual(self.made[1].ReminderSet, False)
        self.box()._plan(clean_plan_request(self.order()))
        self.assertFalse(hasattr(self.made[2], "ReminderSet"), "not named by the phone: as Outlook sets it")
        self.assertEqual(clean_plan_request(self.order(reminder=10 ** 9))["reminder"], mailbox.REMINDER_MOST)
        with self.assertRaises(MailError):
            clean_plan_request(self.order(reminder="soon"))

    def test_the_calendar_says_when_an_entry_reminds(self):
        row = OutlookMailbox._event_row(Appointment("AA", "Dentist", at(24, 9), at(24, 10), ReminderSet=True, ReminderMinutesBeforeStart=15))
        self.assertEqual(row["reminder"], 15)
        self.assertIsNone(OutlookMailbox._event_row(Appointment("AA", "Dentist", at(24, 9), at(24, 10)))["reminder"])


class Repeating(Planning):
    def test_a_new_entry_can_repeat_a_number_of_times_or_without_an_end(self):
        box = self.box()
        made = []

        def create(kind):
            entry = planned(Appointment("NEW", "", at(1), at(1)))
            entry.pattern = type("P", (), {})()
            entry.GetRecurrencePattern = lambda: entry.pattern
            made.append(entry)
            return entry

        box._app.CreateItem = create
        box._plan(clean_plan_request(self.order(repeat={"every": "week", "times": 10})))
        self.assertEqual((made[0].pattern.RecurrenceType, made[0].pattern.Occurrences, made[0].Start), (1, 10, at(24, 14)))
        box._plan(clean_plan_request(self.order(repeat={"every": "month"})))
        self.assertEqual((made[1].pattern.RecurrenceType, made[1].pattern.NoEndDate), (2, True))
        self.assertFalse(hasattr(made[1].pattern, "Occurrences"))
        self.assertEqual(clean_plan_request(self.order(repeat={"every": "day", "times": 10 ** 6}))["repeat"]["times"], mailbox.REPEAT_MOST)
        self.assertIsNone(clean_plan_request(self.order(repeat={"every": "fortnight"}))["repeat"])
        self.assertIsNone(clean_plan_request(self.order(id=ID, repeat={"every": "day"}))["repeat"], "an entry that is there is not made to repeat from the phone")

    def utc(self, moment):
        return moment.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_an_entry_that_repeats_needs_to_know_this_one_or_all(self):
        series = series_of(at(23, 9, 30), at(24, 9, 30))
        with self.assertRaises(MailError) as caught:
            self.box(series)._plan(clean_plan_request(self.order(id=ID)))
        self.assertIn("this one time or the whole series", str(caught.exception))

    def test_one_time_of_a_series_is_changed_and_the_others_stay(self):
        series = series_of(at(23, 9, 30), at(24, 9, 30))
        order = self.order(id=ID, scope="one", occurrence=self.utc(at(24, 9, 30)), **{"with": ["late@example.com"], "withExact": True})
        self.box(series)._plan(clean_plan_request(order))
        first, second = series.times
        self.assertEqual((second.Subject, second.Start, second.calls), ("Gasket fit check", at(24, 14), ["save"]))
        self.assertEqual((first.Subject, first.calls, series.calls, went_to(second)), ("Stand-up", [], [], []),
                         "who is invited to one time stays as the series has it")

    def test_outlook_is_asked_again_on_another_clock_until_the_one_it_gives_starts_when_it_should(self):
        away = dt.datetime.now().astimezone().utcoffset()
        if not away:
            self.skipTest("this computer is on UTC: there is no other clock to be wrong by")
        series = series_of(at(24, 9, 30), shift=away)  # this Outlook wants the time less this PC's distance from UTC
        self.box(series)._plan(clean_plan_request({"what": "remove", "id": ID, "scope": "one", "occurrence": self.utc(at(24, 9, 30))}))
        self.assertEqual((series.times[0].calls, series.pattern.asked), (["delete"], [at(24, 9, 30), at(24, 9, 30) - away]))
        with self.assertRaises(MailError) as caught:
            self.box(series)._plan(clean_plan_request({"what": "remove", "id": ID, "scope": "one", "occurrence": self.utc(at(25, 9, 30))}))
        self.assertIn("cannot find that one time", str(caught.exception))

    def test_the_series_takes_a_new_name_but_not_a_new_time(self):
        series = series_of(at(23, 9, 30), at(24, 9, 30))
        self.box(series)._plan(clean_plan_request(self.order(id=ID, scope="series")))
        self.assertEqual((series.Subject, series.Location, series.Start, series.calls), ("Gasket fit check", "Lab 2", at(23, 9, 30), ["save"]))
        self.assertEqual([one.calls for one in series.times], [[], []])

    def test_the_series_is_taken_out_whole_and_a_meeting_is_cancelled(self):
        series = series_of(at(23, 9, 30), at(24, 9, 30))
        self.box(series)._plan(clean_plan_request({"what": "remove", "id": ID, "scope": "series"}))
        self.assertEqual(series.calls, ["delete"])
        meeting = series_of(at(23, 9, 30), meeting=1)
        self.assertTrue(self.box(meeting)._plan(clean_plan_request({"what": "remove", "id": ID, "scope": "series"}))["told"])
        self.assertEqual((meeting.MeetingStatus, meeting.calls), (5, ["send", "delete"]))


class WhoIsInvited(Planning):
    def meeting(self):
        entry = planned(Appointment("AB", "Review", at(24, 9), at(24, 10), meeting=1, response=1, Organizer="Me"))
        entry.Recipients = Recipients([
            Recipient("Me", 1, smtp="me@example.com"), Recipient("Tomas Berg", 1, smtp="tomas@example.com"),
            Recipient("Anna Okafor", 1, smtp="anna@example.com"), Recipient("Jonas Weber", 2, smtp="jonas@example.com"),
            Recipient("Room 2", 3),
        ])
        return entry

    def box(self, existing=None, hours=0):
        box = super().box(existing, hours)
        box._who = ("me@example.com", "Me")
        box._mine = {"me@example.com"}
        return box

    def test_somebody_is_taken_off_and_somebody_else_invited_and_the_rest_stays(self):
        entry = self.meeting()
        result = self.box(entry)._plan(clean_plan_request(self.order(id=ID, withExact=True, **{"with": ["tomas@example.com", "new@example.com"]})))
        self.assertEqual(went_to(entry), [("Me", 1), ("Tomas Berg", 1), ("Jonas Weber", 2), ("Room 2", 3), ("new@example.com", 1)])
        self.assertEqual((result["told"], entry.calls), (True, ["send"]))

    def test_a_page_that_does_not_list_them_changes_nobody(self):
        entry = self.meeting()
        self.box(entry)._plan(clean_plan_request(self.order(id=ID, **{"with": []})))
        self.assertEqual(len(entry.Recipients), 5)

    def test_an_entry_for_yourself_becomes_a_meeting_when_somebody_is_invited(self):
        mine = planned(Appointment("AA", "Fit check", at(24, 9), at(24, 10)))
        result = self.box(mine)._plan(clean_plan_request(self.order(id=ID, withExact=True, **{"with": ["priya@example.com"]})))
        self.assertEqual((mine.MeetingStatus, went_to(mine), mine.calls, result["told"]), (1, [("priya@example.com", 1)], ["send"], True))

    def test_the_entry_says_who_is_invited_without_yourself(self):
        box = self.box()
        box._item = lambda event_id: self.meeting()
        got = box._event(ID)
        self.assertEqual([one["address"] for one in got["invitedPeople"]], ["tomas@example.com", "anna@example.com"])

    def test_an_entry_in_another_calendar_is_not_changed_from_the_phone(self):
        entry = self.meeting()
        entry.Parent = type("Folder", (), {"EntryID": "THEIRS"})()
        box = self.box(entry)
        box._ns = type("Ns", (), {
            "CompareEntryIDs": lambda self, one, other: one == other,
            "GetDefaultFolder": lambda self, number: type("Folder", (), {"EntryID": "MINE"})(),
        })()
        with self.assertRaises(MailError) as caught:
            box._plan(clean_plan_request(self.order(id=ID)))
        self.assertIn("another calendar", str(caught.exception))
        self.assertEqual(entry.calls, [])


class Shown:
    def __init__(self, name, folder, broken=False):
        self.DisplayName, self._folder, self.broken = name, folder, broken

    @property
    def Folder(self):
        if self.broken:
            raise RuntimeError("the mailbox cannot be reached")
        return self._folder


def calendar_folder(entry, name="Calendar", kind=1, inside=()):
    return type("Folder", (), {"EntryID": entry, "StoreID": "STORE-" + entry[:2], "Name": name, "DefaultItemType": kind, "Folders": list(inside)})()


class OtherCalendars(unittest.TestCase):
    def box(self, groups, inside=()):
        box = OutlookMailbox(read_ahead=False)
        mine = calendar_folder("AA" * 12, inside=inside)
        module = type("Module", (), {"NavigationGroups": [type("Group", (), {"Name": name, "NavigationFolders": shown})() for name, shown in groups]})()
        explorer = type("Explorer", (), {"NavigationPane": type("Pane", (), {"Modules": type("Modules", (), {"GetNavigationModule": lambda self, number: module})()})()})()
        box._app = type("App", (), {"ActiveExplorer": lambda self: explorer})()
        box._ns = type("Ns", (), {"GetDefaultFolder": lambda self, number: mine})()
        return box

    def test_the_calendars_outlook_shows_at_the_side_and_the_ones_inside_your_own(self):
        mine = calendar_folder("AA" * 12)
        box = self.box([
            ("My Calendars", [Shown("Calendar", mine), Shown("Birthdays", calendar_folder("AB" * 12))]),
            ("Shared Calendars", [Shown("Priya Nair", calendar_folder("BA" * 12)), Shown("Gone", None, broken=True), Shown("Contacts", calendar_folder("CC" * 12, kind=2))]),
        ], inside=[calendar_folder("AC" * 12, "Projects"), calendar_folder("AB" * 12, "Birthdays")])
        got = box._calendars_now()
        self.assertEqual([(one["name"], one["group"]) for one in got], [("Birthdays", "My Calendars"), ("Priya Nair", "Shared Calendars"), ("Projects", "")])
        self.assertTrue(all(mailbox.FOLDER_ID.fullmatch(one["id"]) for one in got))

    def test_without_a_window_of_outlook_the_ones_inside_your_own_are_still_listed(self):
        box = self.box([], inside=[calendar_folder("AC" * 12, "Projects")])
        box._app = type("App", (), {"ActiveExplorer": lambda self: 1 / 0})()
        self.assertEqual([one["name"] for one in box._calendars_now()], ["Projects"])

    def test_another_calendar_is_read_and_nothing_in_it_is_yours_to_change(self):
        from tests.test_more_of_outlook import CalendarItems

        real = mailbox.today
        mailbox.today = lambda: at(20)
        self.addCleanup(lambda: setattr(mailbox, "today", real))
        box = OutlookMailbox(read_ahead=False)
        items = CalendarItems([Appointment("AD", "Theirs", at(21, 9), at(21, 10)), Appointment("AE", "Invited", at(22, 9), at(22, 10), meeting=3, response=5)])
        theirs = type("Folder", (), {"StoreID": "OTHER", "Items": items, "DefaultItemType": 1})()
        box._folder = lambda which: theirs
        page = box._calendar(0, 14, FOLDER)
        self.assertEqual([(event["mine"], event["invited"], event["other"]) for event in page["events"]], [(False, False, True)] * 2)
        box._folder = lambda which: type("Folder", (), {"StoreID": "OTHER", "DefaultItemType": 0})()
        with self.assertRaises(MailError):
            box._calendar(0, 14, FOLDER)

    def test_each_calendar_is_kept_apart_and_a_name_that_is_not_one_is_refused(self):
        box = OutlookMailbox(read_ahead=False)
        asked = []
        box._do = lambda fn, *args, timeout=20, first=False: asked.append(args) or {"events": [], "from": "2026-11-20"}
        box.calendar()
        box.calendar(which=FOLDER)
        box.calendar(which=FOLDER)
        self.assertEqual(asked, [(0, 14), (0, 14, FOLDER)])
        for bad in ("inbox", "../x", "f.short.AB"):
            with self.assertRaises(MailError):
                box.calendar(which=bad)


class Rule:
    def __init__(self, name, on=True, order=1, kind=0):
        self.Name, self.Enabled, self.ExecutionOrder, self.RuleType = name, on, order, kind
        part = lambda: type("Part", (), {"Recipients": Recipients(), "Enabled": False, "Folder": None})()  # noqa: E731
        self.Conditions = type("Conditions", (), {"From": part()})()
        self.Actions = type("Actions", (), {"MoveToFolder": part()})()


class Rules(list):
    saved = 0

    def Create(self, name, kind):
        self.append(Rule(name, on=True, order=len(self) + 1, kind=kind))
        return self[-1]

    def Save(self):
        self.saved += 1


class Child:
    def __init__(self, name, entry="AB" * 12, items=0, inside=()):
        self.Name, self.EntryID, self.StoreID = name, entry, "STORE"
        self.Items = type("Items", (), {"Count": items})()
        self.Folders = Children(inside)
        self.deleted = False

    def Delete(self):
        self.deleted = True


class Children(list):
    Count = property(len)

    def Add(self, name):
        self.append(Child(name, "CD" * 12))
        return self[-1]


class Arranged:
    """An Outlook with rules, folders and categories."""

    def __init__(self):
        self.rules = Rules([Rule("Newsletters", on=False, order=1), Rule("Sent copies", order=2, kind=1)])
        self.DefaultStore = type("Store", (), {"GetRules": lambda store: self.rules})()
        self.inbox = Child("Inbox", "6A" * 12, inside=[Child("Suppliers")])
        self.inbox.Parent = Child("Mailbox", "00" * 12, inside=[self.inbox])
        self.own = Child("Projects", "AB" * 12)
        self.Categories = Categories(["Customer"])
        self.Stores = []

    def GetDefaultFolder(self, number):
        return self.inbox if number == mailbox.OL_INBOX else Child("Usual", f"{number:02d}" * 12)

    def GetFolderFromID(self, entry_id, store_id):
        return self.inbox if entry_id == self.inbox.EntryID else self.own

    def CompareEntryIDs(self, one, other):
        return one == other


class Categories(list):
    def __init__(self, names):
        super().__init__(type("Category", (), {"Name": name})() for name in names)

    def Add(self, name):
        self.append(type("Category", (), {"Name": name})())


class Arranging(unittest.TestCase):
    def setUp(self):
        self.box = OutlookMailbox(read_ahead=False)
        self.box._ns = self.outlook = Arranged()
        self.box._stores["0123456789"] = "STORE"

    def arrange(self, **payload):
        return self.box._arrange(clean_arrange_request(payload))

    def test_the_rules_in_their_order_each_on_or_off(self):
        self.assertEqual(self.box._rules_now(), [
            {"name": "Newsletters", "on": False, "order": 1, "sent": False}, {"name": "Sent copies", "on": True, "order": 2, "sent": True}])

    def test_a_rule_is_switched_and_outlook_is_told_to_keep_it(self):
        self.assertEqual(self.arrange(what="rule", name="Newsletters", on=True), {"ok": True, "on": True})
        self.assertEqual((self.outlook.rules[0].Enabled, self.outlook.rules.saved), (True, 1))
        with self.assertRaises(MailError):
            self.arrange(what="rule", name="No such rule", on=True)
        self.assertEqual(self.outlook.rules.saved, 1)

    def test_a_rule_for_whoever_wrote_a_mail(self):
        self.box._item = lambda mail_id: Message()
        result = self.arrange(what="ruleFrom", id=ID, to=FOLDER)
        rule = self.outlook.rules[-1]
        self.assertEqual((result["name"], rule.Name, rule.RuleType), ("Rina Wijaya \u2192 Projects", "Rina Wijaya \u2192 Projects", 0))
        self.assertEqual(([r.Name for r in rule.Conditions.From.Recipients], rule.Conditions.From.Enabled), (["rina@example.com"], True))
        self.assertEqual((rule.Actions.MoveToFolder.Folder, rule.Actions.MoveToFolder.Enabled, self.outlook.rules.saved), (self.outlook.own, True, 1))
        for bad in ({"id": "", "to": FOLDER}, {"id": ID, "to": "inbox"}, {"id": ID, "to": "../x"}):
            with self.assertRaises(MailError):
                clean_arrange_request({"what": "ruleFrom", **bad})

    def test_a_folder_is_made_inside_the_inbox_next_to_it_or_inside_one_of_your_own(self):
        made = self.arrange(what="folder", name="Tooling", **{"in": "inbox"})
        self.assertEqual((made["name"], [child.Name for child in self.outlook.inbox.Folders]), ("Tooling", ["Suppliers", "Tooling"]))
        self.assertTrue(mailbox.FOLDER_ID.fullmatch(made["id"]))
        self.arrange(what="folder", name="Archive 2027")
        self.assertEqual([child.Name for child in self.outlook.inbox.Parent.Folders], ["Inbox", "Archive 2027"])
        self.arrange(what="folder", name="Phase 2", **{"in": FOLDER})
        self.assertEqual([child.Name for child in self.outlook.own.Folders], ["Phase 2"])
        with self.assertRaises(MailError) as caught:
            self.arrange(what="folder", name="suppliers", **{"in": "inbox"})
        self.assertIn("already", str(caught.exception))

    def test_a_folder_of_your_own_is_renamed_and_only_an_empty_one_taken_out(self):
        self.assertEqual(self.arrange(what="rename", id=FOLDER, name="Projects 2027"), {"ok": True, "name": "Projects 2027"})
        self.assertEqual(self.outlook.own.Name, "Projects 2027")
        self.assertEqual(self.arrange(what="unfolder", id=FOLDER), {"ok": True, "removed": True})
        self.assertTrue(self.outlook.own.deleted)
        for items, inside in ((3, ()), (0, (Child("Inner"),))):
            self.outlook.own = Child("Full", "AB" * 12, items=items, inside=inside)
            with self.assertRaises(MailError) as caught:
                self.arrange(what="unfolder", id=FOLDER)
            self.assertIn("not empty", str(caught.exception))
            self.assertFalse(self.outlook.own.deleted)

    def test_outlooks_own_folders_are_never_renamed_or_taken_out(self):
        inbox_id = "f.0123456789." + self.outlook.inbox.EntryID
        for order in ({"what": "rename", "name": "Mine"}, {"what": "unfolder"}):
            with self.assertRaises(MailError) as caught:
                self.arrange(id=inbox_id, **order)
            self.assertIn("Outlook's own folders", str(caught.exception))
        self.assertEqual((self.outlook.inbox.Name, self.outlook.inbox.deleted), ("Inbox", False))

    def test_a_category_is_made_once(self):
        self.assertEqual(self.arrange(what="category", name="Urgent"), {"ok": True, "name": "Urgent"})
        self.assertEqual([category.Name for category in self.outlook.Categories], ["Customer", "Urgent"])
        with self.assertRaises(MailError):
            self.arrange(what="category", name="customer")

    def test_names_that_cannot_be_and_actions_that_are_not(self):
        for bad in ({"what": "folder", "name": ""}, {"what": "folder", "name": "a/b"}, {"what": "category", "name": "a,b"}, {"what": "rename", "id": "inbox", "name": "x"},
                    {"what": "folder", "name": "x", "in": "outbox"}, {"what": "rule", "name": ""}, {"what": "delete-everything"}):
            with self.assertRaises(MailError, msg=bad):
                clean_arrange_request(bad)
        self.assertEqual(len(clean_arrange_request({"what": "folder", "name": "x" * 500})["name"]), mailbox.NAME_MOST)

    def test_it_goes_first_and_the_folders_are_asked_for_again(self):
        box = OutlookMailbox(read_ahead=False)
        asked = []
        box._do = lambda fn, *args, timeout=20, first=False: asked.append((fn.__name__, first)) or {"ok": True}
        box._folder_list = (0.0, {})
        box.arrange({"what": "category", "name": "Urgent"})
        self.assertEqual((asked, box._folder_list), ([("_arrange", True)], None))
        box._do = lambda fn, *args, timeout=20, first=False: (_ for _ in ()).throw(OutlookBusy(started=False))
        with self.assertRaises(MailError) as caught:
            box.arrange({"what": "category", "name": "Urgent"})
        self.assertIn("nothing was changed", str(caught.exception))


class Demo(unittest.TestCase):
    def test_a_shared_calendar_rules_a_folder_and_a_category(self):
        box = DemoMailbox()
        shared = box.calendars()["calendars"][0]
        events = box.calendar(which=shared["id"])["events"]
        self.assertEqual((shared["name"], len(events), {event["mine"] for event in events}), ("Priya Nair", 3, {False}))
        self.assertIn("Customer", box.event(events[0]["id"])["required"])
        with self.assertRaises(MailError):
            box.calendar(which="f.0000000000.ABCDEF0123456789AB")
        rules = box.rules()["rules"]
        box.arrange({"what": "rule", "name": rules[1]["name"], "on": True})
        self.assertTrue(box.rules()["rules"][1]["on"])
        made = box.arrange({"what": "folder", "in": "inbox", "name": "Tooling"})
        self.assertEqual(box.folders()["folders"][-1], {"id": made["id"], "name": "Tooling", "in": "Inbox", "file": "", "unread": 0})
        self.assertEqual(box.list(made["id"])["mail"], [])
        box.arrange({"what": "rename", "id": made["id"], "name": "Tooling 2027"})
        box.arrange({"what": "unfolder", "id": made["id"]})
        with self.assertRaises(MailError):
            box.arrange({"what": "unfolder", "id": box.folders()["folders"][0]["id"]})  # it holds mail
        box.arrange({"what": "category", "name": "Urgent"})
        self.assertIn("Urgent", box.folders()["categories"])
        first = box.list("inbox")["mail"][0]
        self.assertEqual(box.arrange({"what": "ruleFrom", "id": first["id"], "to": "junk"})["name"], "Mateo Silva \u2192 Junk")

    def test_formatting_a_receipt_and_an_entry_that_repeats(self):
        box = DemoMailbox()
        box.send({"mode": "new", "to": ["a@mail.example"], "subject": "Marked", "body": "**Now**", "format": "marks", "receipt": True})
        sent = box.get(box.list("sent", query="Marked")["mail"][0]["id"])
        self.assertIn("<b>Now</b>", sent["html"])
        start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
        order = {"what": "save", "subject": "Weekly", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "end": (start + dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "repeat": {"every": "week", "times": 3}, "reminder": 30}
        box.plan(order)
        rows = [event for event in box.calendar(0, 30)["events"] if event["subject"] == "Weekly"]
        self.assertEqual(([event["reminder"] for event in rows], {event["recurring"] for event in rows}), ([30, 30, 30], {True}))
        with self.assertRaises(MailError):
            box.plan({**order, "id": rows[0]["id"]})  # this one, or all of them?
        box.plan({**order, "id": rows[0]["id"], "scope": "one", "occurrence": rows[1]["start"], "subject": "Weekly, moved"})
        self.assertEqual([event["subject"] for event in box.calendar(0, 30)["events"] if event["id"] == rows[0]["id"]], ["Weekly", "Weekly, moved", "Weekly"])
        box.plan({"what": "remove", "id": rows[0]["id"], "scope": "series"})
        self.assertEqual([event for event in box.calendar(0, 30)["events"] if event["id"] == rows[0]["id"]], [])


if __name__ == "__main__":
    unittest.main()
