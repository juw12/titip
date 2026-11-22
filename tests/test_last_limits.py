"""0.15.0, against stand-ins: a task's start day and reminder, a colour for a category, and an entry that
repeats on several days of a week or on such a weekday of a month."""
import datetime as dt
import re
import unittest
from pathlib import Path

from tests.test_more_of_outlook import Appointment, at
from tests import test_other_pages
from tests.test_other_pages import Thing, day
from tests.test_rest_of_outlook import planned
from titip import mailbox
from titip.mailbox import (COLOURS, DemoMailbox, MailError, OutlookMailbox, clean_arrange_request, clean_keep_request, clean_plan_request,
                           colour_name, takes_place)

WEB = Path(__file__).resolve().parent.parent / "titip" / "web"


def utc(moment: dt.datetime) -> str:
    return moment.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


NONE = dt.datetime(4501, 1, 1, tzinfo=dt.timezone(dt.timedelta(0), "as Outlook hands it over"))  # Outlook's own "no date"


def none(value) -> bool:
    return value.year > 4000


class Task(Thing):
    """A task the way Outlook keeps its days: without a due day it has no start day either, and a start day
    after the due day moves the due day along."""

    def __setattr__(self, name, value):
        if name in ("DueDate", "StartDate") and none(value) and value != NONE:
            raise OSError(22, "Invalid argument")  # what a real Outlook (and pywin32) does with any other time of 4501
        self.__dict__[name] = value
        if name == "DueDate" and none(value):
            self.__dict__["StartDate"] = NONE
        if name == "StartDate" and not none(value) and (none(self.__dict__.get("DueDate", NONE)) or self.__dict__["DueDate"] < value):
            self.__dict__["DueDate"] = value


class ATaskStartsAndReminds(unittest.TestCase):
    box, keep = test_other_pages.Keeping.box, test_other_pages.Keeping.keep

    def test_the_start_day_and_the_reminder_are_set_and_read_back(self):
        box = self.box(make=Task)
        remind = dt.datetime(2026, 12, 1, 8, 30)
        self.keep(box, kind="tasks", what="save", title="Check the torque rig", start="2026-12-01", due="2026-12-04", remind=utc(remind))
        task = box._app.created[0]
        self.assertEqual((task.StartDate.date(), task.DueDate.date()), (dt.date(2026, 12, 1), dt.date(2026, 12, 4)))
        self.assertEqual((task.ReminderSet, task.ReminderTime, task.calls), (True, remind, ["save"]))
        row = OutlookMailbox(read_ahead=False)._thing_row("tasks", task, "0123456789")
        self.assertEqual((row["start"], row["due"]), ("2026-12-01", "2026-12-04"))

    def test_one_task_in_full_says_when_it_reminds(self):
        task = Thing("T1", Subject="Check", Body="", DueDate=day(2026, 12, 4), StartDate=day(2026, 12, 1), Complete=False, Importance=1,
                     ReminderSet=True, ReminderTime=dt.datetime(2026, 12, 1, 8, 30))
        box = self.box(task)
        full = box._thing("tasks", "0123456789." + "AB" * 12)
        self.assertEqual((full["start"], full["remind"]), ("2026-12-01", utc(dt.datetime(2026, 12, 1, 8, 30))))
        task.ReminderSet = False
        self.assertEqual(box._thing("tasks", "0123456789." + "AB" * 12)["remind"], "")

    def test_a_page_from_before_does_not_say_and_nothing_of_it_is_touched(self):
        task = Thing("T1", Subject="Check", Body="", DueDate=day(2026, 12, 4), StartDate=day(2026, 12, 1), Complete=False, Importance=1,
                     ReminderSet=True, ReminderTime=dt.datetime(2026, 12, 1, 8, 30))
        box = self.box(task)
        self.keep(box, kind="tasks", what="save", id="0123456789." + "AB" * 12, title="Check", due="2026-12-04")
        self.assertEqual((task.StartDate, task.ReminderSet, task.ReminderTime), (day(2026, 12, 1), True, dt.datetime(2026, 12, 1, 8, 30)))
        request = clean_keep_request({"kind": "tasks", "what": "save", "title": "Check", "due": "2026-12-04"})
        self.assertFalse("start" in request or "remind" in request)

    def test_the_start_and_the_reminder_are_taken_off_again(self):
        task = Task("T1", Subject="Check", Body="", DueDate=day(2026, 12, 4), StartDate=day(2026, 12, 1), Complete=False, Importance=1,
                    ReminderSet=True, ReminderTime=dt.datetime(2026, 12, 1, 8, 30))
        box = self.box(task)
        self.keep(box, kind="tasks", what="save", id="0123456789." + "AB" * 12, title="Check", due="2026-12-04", start="", remind="")
        self.assertEqual((task.StartDate, task.DueDate.date(), task.ReminderSet), (NONE, dt.date(2026, 12, 4), False))

    def test_no_day_is_outlooks_own_no_date_and_is_not_set_where_there_is_none(self):
        """What the real Outlook showed on 22 November 2026: a plain time in 4501 cannot be handed over at all,
        any other time of that year is refused, and its own value for "no date" is taken."""
        class App:
            def __init__(self):
                self.made = []

            def CreateItem(self, kind):
                self.made.append(Task("NEW", DueDate=NONE, StartDate=NONE))
                return self.made[-1]

        box = self.box()
        box._app = App()
        self.keep(box, kind="tasks", what="save", title="Order hinge samples", start="", remind="")
        new = box._app.made[0]
        self.assertEqual((new.DueDate, new.StartDate, new.calls, len(box._app.made)), (NONE, NONE, ["save"], 1),
                         "a new task has no day: none is set, and Outlook is not asked for its no date")
        task = Task("T1", Subject="Check", Body="", DueDate=day(2026, 12, 4), StartDate=day(2026, 12, 1), Complete=False, Importance=1)
        box = self.box(task)
        box._app = App()
        self.keep(box, kind="tasks", what="save", id="0123456789." + "AB" * 12, title="Check", start="", remind="")
        self.assertEqual((task.DueDate, task.StartDate), (NONE, NONE))
        self.assertEqual(box._app.made[0].calls, [f"close{mailbox.OL_DISCARD}"], "the task it was read off is thrown away")
        self.keep(box, kind="tasks", what="save", id="0123456789." + "AB" * 12, title="Check", due="2026-12-04", start="2026-12-02")
        self.keep(box, kind="tasks", what="save", id="0123456789." + "AB" * 12, title="Check", due="2026-12-04", start="")
        self.assertEqual((task.DueDate.date(), task.StartDate, len(box._app.made)), (dt.date(2026, 12, 4), NONE, 1), "read once, then kept")

    def test_an_outlook_that_does_not_take_the_day_off_is_said(self):
        class Keeps(Thing):
            def __setattr__(self, name, value):
                if not (name == "DueDate" and none(value)):
                    self.__dict__[name] = value

        task = Keeps("T1", Subject="Check", Body="", DueDate=day(2026, 12, 4), Complete=False, Importance=1)
        with self.assertRaises(MailError) as refused:
            self.keep(self.box(task), kind="tasks", what="save", id="0123456789." + "AB" * 12, title="Check")
        self.assertIn("did not take that day off", str(refused.exception))
        self.assertEqual(task.calls, [], "and the task is not saved half changed")

    def test_a_due_day_outlook_moved_along_with_the_start_is_put_right(self):
        class Dragging(Task):
            """Setting the start day moves the due day by as much, as Outlook does for a task that had both."""

            def __setattr__(self, name, value):
                before = self.__dict__.get("StartDate")
                super().__setattr__(name, value)
                if name == "StartDate" and before is not None and not none(before) and not none(value):
                    self.__dict__["DueDate"] = self.__dict__["DueDate"] + (value - before)

        task = Dragging("T1", Subject="Check", Body="", DueDate=day(2026, 12, 4), StartDate=day(2026, 12, 1), Complete=False, Importance=1)
        box = self.box(task)
        self.keep(box, kind="tasks", what="save", id="0123456789." + "AB" * 12, title="Check", due="2026-12-04", start="2026-12-03")
        self.assertEqual((task.StartDate.date(), task.DueDate.date()), (dt.date(2026, 12, 3), dt.date(2026, 12, 4)))

    def test_a_start_day_outlook_does_not_keep_is_said(self):
        class Losing(Task):
            """An Outlook that moves the due day on when a start is set, and takes the start away when the due day is."""

            def __setattr__(self, name, value):
                super().__setattr__(name, value)
                if name == "DueDate":
                    self.__dict__["StartDate"] = NONE
                elif name == "StartDate" and not none(value):
                    self.__dict__["DueDate"] = value + dt.timedelta(days=5)

        box = self.box(make=Losing)
        with self.assertRaises(MailError) as refused:
            self.keep(box, kind="tasks", what="save", title="Check", due="2026-12-04", start="2026-12-01")
        self.assertIn("did not keep the start day", str(refused.exception))
        self.assertEqual(box._app.created[0].calls, [f"close{mailbox.OL_DISCARD}"])

    def test_a_reminder_time_outlook_does_not_take_is_said_in_words_of_a_reminder(self):
        class Stuck(Task):
            def __setattr__(self, name, value):
                super().__setattr__(name, dt.datetime(2020, 1, 1) if name == "ReminderTime" else value)

        box = self.box(make=Stuck)
        with self.assertRaises(MailError) as refused:
            self.keep(box, kind="tasks", what="save", title="Check", due="2026-12-04", remind="2026-12-01T01:30:00Z")
        self.assertEqual(str(refused.exception), "Outlook did not take the time of the reminder. Set it in Outlook itself.")

    def test_what_cannot_be_is_refused_before_outlook_is_asked(self):
        for payload, words in (
            ({"start": "2026-12-01"}, "is due on a day too"),
            ({"start": "2026-12-05", "due": "2026-12-04"}, "due before it starts"),
            ({"start": "soon", "due": "2026-12-04"}, "day could not be read"),
            ({"due": "2026-12-04", "remind": "tomorrow"}, "time of the reminder could not be read"),
        ):
            with self.assertRaises(MailError) as refused:
                clean_keep_request({"kind": "tasks", "what": "save", "title": "Check", **payload})
            self.assertIn(words, str(refused.exception))
        same_day = clean_keep_request({"kind": "tasks", "what": "save", "title": "Check", "start": "2026-12-04", "due": "2026-12-04", "remind": None})
        self.assertEqual((same_day["start"], same_day["remind"]), (dt.date(2026, 12, 4), None))

    def test_the_invented_mailbox_keeps_them(self):
        box = DemoMailbox()
        box.keep({"kind": "tasks", "what": "save", "title": "Check the rig", "start": "2026-12-01", "due": "2026-12-04", "remind": "2026-12-01T01:30:00Z"})
        row = next(one for one in box.things("tasks")["things"] if one["title"] == "Check the rig")
        self.assertEqual((row["start"], box.thing("tasks", row["id"])["remind"]), ("2026-12-01", "2026-12-01T01:30:00Z"))
        box.keep({"kind": "tasks", "what": "save", "id": row["id"], "title": "Check the rig", "due": "2026-12-04"})
        self.assertEqual(box.thing("tasks", row["id"])["start"], "2026-12-01", "an older page leaves it")
        box.keep({"kind": "tasks", "what": "save", "id": row["id"], "title": "Check the rig", "start": "", "remind": ""})
        full = box.thing("tasks", row["id"])
        self.assertEqual((full["start"], full["due"], full["remind"]), ("", "", ""))


class Named(list):
    """Outlook's list of categories."""

    def __init__(self, *pairs):
        super().__init__(type("Category", (), {"Name": name, "Color": colour})() for name, colour in pairs)
        self.added = []

    def Add(self, name, colour=None):
        self.added.append((name, colour))
        self.append(type("Category", (), {"Name": name, "Color": 0 if colour is None else colour})())


class ACategoryHasAColour(unittest.TestCase):
    def test_the_colour_goes_to_outlook_as_its_number(self):
        box = OutlookMailbox(read_ahead=False)
        box._ns = type("Namespace", (), {"Categories": Named(("Customer", 8))})()
        self.assertEqual(box._arrange(clean_arrange_request({"what": "category", "name": "Urgent", "colour": "red"})),
                         {"ok": True, "name": "Urgent", "colour": "red"})
        self.assertEqual(box._arrange(clean_arrange_request({"what": "category", "name": "Later", "colour": ""})),
                         {"ok": True, "name": "Later", "colour": ""})
        box._arrange(clean_arrange_request({"what": "category", "name": "Plain"}))
        box._arrange(clean_arrange_request({"what": "category", "name": "Odd", "colour": "ultraviolet"}))
        self.assertEqual(box._ns.Categories.added, [("Urgent", 1), ("Later", 0), ("Plain", None), ("Odd", None)],
                         "no colour said, or one that is none: Outlook picks, as before 0.15.0")

    def test_every_colour_outlook_has_is_shown_as_one_the_phone_knows(self):
        self.assertEqual([colour_name(number) for number in (0, 1, 2, 4, 5, 6, 8, 9, 13)], list(COLOURS))
        self.assertEqual({colour_name(number) for number in range(26)}, set(COLOURS))
        self.assertEqual((colour_name(23), colour_name(16), colour_name(15), colour_name(99), colour_name(None), colour_name("x")),
                         ("blue", "red", "gray", "", "", ""))

    def test_the_list_of_categories_comes_with_their_colours(self):
        class Unreadable:
            Name = "Old"

            @property
            def Color(self):
                raise RuntimeError("no colour")

        box = OutlookMailbox(read_ahead=False)
        names = Named(("Customer", 8), ("Supplier", 20), ("Plain", 0))
        names.append(Unreadable())
        box._ns = type("Namespace", (), {"Categories": names, "Stores": [],
                                         "GetDefaultFolder": lambda self, number: type("Folder", (), {"StoreID": "MAIN"})()})()
        got = box._folders_now()
        self.assertEqual(got["categories"], ["Customer", "Supplier", "Plain", "Old"])
        self.assertEqual(got["colours"], {"Customer": "blue", "Supplier": "green", "Plain": ""})

    def test_the_invented_mailbox(self):
        box = DemoMailbox()
        self.assertEqual(box.folders()["colours"]["Customer"], "blue")
        box.arrange({"what": "category", "name": "Urgent", "colour": "red"})
        self.assertEqual(box.folders()["colours"]["Urgent"], "red")


class Repeating:
    """24 November 2026 is a Tuesday."""

    def order(self, hour=14, **repeat):
        start = at(24, hour)
        return {"what": "save", "subject": "Stand-up", "start": utc(start), "end": utc(start + dt.timedelta(minutes=15)), "repeat": repeat}

    def made(self, **repeat):
        box = OutlookMailbox(read_ahead=False)
        entry = planned(Appointment("NEW", "", at(1), at(1)))
        entry.pattern = type("Pattern", (), {})()
        entry.GetRecurrencePattern = lambda: entry.pattern
        box._app = type("App", (), {"CreateItem": lambda self, kind: entry})()
        box._plan(clean_plan_request(self.order(**repeat)))
        return entry.pattern


class RepeatsOnDaysOfTheWeek(Repeating, unittest.TestCase):
    def test_tuesday_and_thursday_as_outlook_counts_the_days(self):
        pattern = self.made(every="week", days=[2, 4], day=2, times=6)
        self.assertEqual((pattern.RecurrenceType, pattern.DayOfWeekMask, pattern.Occurrences), (1, 4 + 16, 6))
        self.assertFalse(hasattr(pattern, "Instance") or hasattr(pattern, "Interval"))

    def test_the_day_it_starts_on_is_always_one_of_them(self):
        self.assertEqual(clean_plan_request(self.order(every="week", days=[4, 5], day=2))["repeat"]["mask"], 4 + 16 + 32)
        self.assertEqual(clean_plan_request(self.order(every="week", days=[2], day=2))["repeat"]["mask"], 0, "one day: every week, as before")
        self.assertFalse(hasattr(self.made(every="week", days=[2], day=2), "DayOfWeekMask"))

    def test_a_phone_on_another_day_than_the_pc_has_its_days_turned(self):
        # for the phone the entry starts on Wednesday (3) and also takes place on Fridays (5); on this PC's clock
        # the same moment is a Tuesday, so it is Tuesdays and Thursdays here
        self.assertEqual(clean_plan_request(self.order(every="week", days=[3, 5], day=3))["repeat"]["mask"], 4 + 16)
        self.assertEqual(clean_plan_request(self.order(every="week", days=[0, 1], day=1))["repeat"]["mask"], 2 + 4, "Sunday and Monday, a day on")

    def test_what_is_not_a_day_is_left_out_and_other_ways_to_repeat_have_no_days(self):
        self.assertEqual(clean_plan_request(self.order(every="week", days=[2, 4, 9, "5", True, -1], day=2))["repeat"]["days"], [2, 4])
        self.assertNotIn("mask", clean_plan_request(self.order(every="day", days=[2, 4], day=2))["repeat"])
        self.assertEqual(clean_plan_request(self.order(every="week", days=[2, 4], day="x"))["repeat"]["mask"], 4 + 16, "no day said: taken as this PC's")

    def test_every_second_week_on_two_days(self):
        pattern = self.made(every="week", interval=2, days=[2, 4], day=2)
        self.assertEqual((pattern.DayOfWeekMask, pattern.Interval, pattern.NoEndDate), (20, 2, True))


class RepeatsOnAWeekdayOfTheMonth(Repeating, unittest.TestCase):
    def test_the_fourth_tuesday(self):
        pattern = self.made(every="monthNth", times=3)
        self.assertEqual((pattern.RecurrenceType, pattern.DayOfWeekMask, pattern.Instance, pattern.Occurrences), (3, 4, 4, 3))

    def test_the_last_tuesday_only_when_it_is_the_last(self):
        self.assertEqual(self.made(every="monthNth", last=True).Instance, 5, "the 24th is the last Tuesday of November 2026")
        early = {**self.order(every="monthNth", last=True), "start": utc(at(10, 14)), "end": utc(at(10, 15))}
        how = clean_plan_request(early)["repeat"]
        self.assertEqual((how["nth"], how["mask"]), (2, 4), "the 10th is the second Tuesday, and not the last: the word 'last' is not taken")
        fifth = {**self.order(every="monthNth"), "start": utc(at(29, 14)), "end": utc(at(29, 15))}
        self.assertEqual(clean_plan_request(fifth)["repeat"]["nth"], 5, "a fifth Sunday is the last one: Outlook has no fifth")

    def test_an_entry_that_is_there_already_is_not_made_to_repeat(self):
        self.assertIsNone(clean_plan_request({**self.order(every="monthNth"), "id": "0123456789." + "AB" * 12, "scope": "series"})["repeat"])


class WhenItTakesPlace(unittest.TestCase):
    """What the invented mailbox shows for a pattern: the days Outlook would work out."""

    START = dt.datetime(2026, 11, 24, 9, 0)  # a Tuesday

    def days(self, most=12, **repeat):
        how = {"times": 0, "interval": 1, **repeat}
        return [moment.strftime("%a %d %b") for moment in takes_place(self.START, how, most)]

    def test_days_of_the_week(self):
        self.assertEqual(self.days(every="week", mask=4 + 16, times=5), ["Tue 24 Nov", "Thu 26 Nov", "Tue 01 Dec", "Thu 03 Dec", "Tue 08 Dec"])
        self.assertEqual(self.days(every="week", mask=4 + 16, interval=2, times=4), ["Tue 24 Nov", "Thu 26 Nov", "Tue 08 Dec", "Thu 10 Dec"])
        self.assertEqual(self.days(every="week", interval=2, times=3), ["Tue 24 Nov", "Tue 08 Dec", "Tue 22 Dec"])
        self.assertEqual(self.days(every="weekday", times=5), ["Tue 24 Nov", "Wed 25 Nov", "Thu 26 Nov", "Fri 27 Nov", "Mon 30 Nov"])
        self.assertEqual(self.days(every="day", interval=3, times=3), ["Tue 24 Nov", "Fri 27 Nov", "Mon 30 Nov"])

    def test_months_and_years(self):
        self.assertEqual(self.days(every="monthNth", nth=4, times=4), ["Tue 24 Nov", "Tue 22 Dec", "Tue 26 Jan", "Tue 23 Feb"])
        self.assertEqual(self.days(every="monthNth", nth=5, times=3), ["Tue 24 Nov", "Tue 29 Dec", "Tue 26 Jan"])
        self.assertEqual(self.days(every="month", times=3), ["Tue 24 Nov", "Thu 24 Dec", "Sun 24 Jan"])
        self.assertEqual([moment.year for moment in takes_place(self.START, {"every": "year", "times": 3, "interval": 1}, 12)], [2026, 2027, 2028])
        end_of_month = takes_place(dt.datetime(2027, 1, 31, 9), {"every": "month", "times": 3, "interval": 1}, 12)
        self.assertEqual([moment.strftime("%d %b") for moment in end_of_month], ["31 Jan", "31 Mar", "31 May"])

    def test_never_more_than_asked_for_and_once_when_it_does_not_repeat(self):
        self.assertEqual(len(self.days(every="day")), 12)
        self.assertEqual(len(self.days(most=3, every="week", mask=4 + 16)), 3)
        self.assertEqual(takes_place(self.START, None, 12), [self.START])

    def test_the_invented_calendar_shows_them(self):
        box = DemoMailbox()
        start = dt.datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) + dt.timedelta(days=2)
        phone_day = (start.weekday() + 1) % 7
        box.plan({"what": "save", "subject": "Twice a week", "start": utc(start), "end": utc(start + dt.timedelta(minutes=30)),
                  "repeat": {"every": "week", "days": [phone_day, (phone_day + 2) % 7], "day": phone_day, "times": 4}})
        days = sorted(event["day"] for event in box.events if event["subject"] == "Twice a week")
        wanted = [(start + dt.timedelta(days=ahead)).date().isoformat() for ahead in (0, 2, 7, 9)]
        self.assertEqual(days, wanted)


class ThePage(unittest.TestCase):
    def setUp(self):
        self.page = (WEB / "app.js").read_text(encoding="utf-8")
        self.sheet = (WEB / "index.html").read_text(encoding="utf-8")

    def test_what_an_older_pc_cannot_do_is_not_offered(self):
        self.assertIn('NEEDS_LAST = "0.15.0"', self.page)
        self.assertIn('$("thingStartField").hidden = $("thingRemindField").hidden = pcBehind(NEEDS_LAST)', self.page)
        self.assertIn('option.getAttribute("data-needs") === "last" ? pcBehind(NEEDS_LAST)', self.page)
        self.assertIn("if (pcBehind(NEEDS_LAST)) { make(); return; }", self.page)
        self.assertEqual(len(re.findall(r'data-needs="last"', self.sheet)), 2)

    def test_the_phone_knows_the_colours_the_pc_takes(self):
        listed = re.search(r"var COLOURS = \[(.*?)\];", self.page, re.S).group(1)
        self.assertEqual(re.findall(r'\["([a-z]*)", "[A-Za-z ]+"\]', listed), list(COLOURS))
        looks = (WEB / "styles.css").read_text(encoding="utf-8")
        for name in COLOURS:
            if name:
                self.assertIn(f'.hue[data-colour="{name}"]', looks)


if __name__ == "__main__":
    unittest.main()
