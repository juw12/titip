"""The parts of the Gmail reader that need no browser."""
import re
import unittest
from unittest import mock

from titip import gmail


class Helpers(unittest.TestCase):
    def test_the_search_address_escapes_the_query(self):
        url = gmail.search_url(1, "in:inbox is:unread newer_than:2d")
        self.assertEqual(url, "https://mail.google.com/mail/u/1/#search/in%3Ainbox%20is%3Aunread%20newer_than%3A2d")

    def test_a_row_keeps_its_key_when_read_and_over_midnight(self):
        today = "unread, Tomas Lind , Your template , 14:31 , Hi, is it free to use?"
        self.assertTrue(gmail.is_unread(today))
        self.assertFalse(gmail.is_unread("Tomas Lind , Your template"))
        for later in (
            "Tomas Lind , Your template , 14:31 , Hi, is it free to use?",
            "unread, Tomas Lind , Your template , Nov 17 , Hi, is it free to use?",
            "unread, Tomas Lind , Your template , 17 Nov , Hi, is it free to use?",
            "unread, Tomas Lind , Your template , 2:31 PM , Hi, is it free to use?",
        ):
            self.assertEqual(gmail.row_key(today), gmail.row_key(later), later)
        answer = "unread, Tomas Lind , me 2 , Your template , 15:02 , And for companies?"
        self.assertNotEqual(gmail.row_key(today), gmail.row_key(answer))

    def test_a_mail_with_an_attachment_keeps_its_key_over_midnight(self):
        today = "unread, Mira Voss , The drawing , has attachment, 08:28 , Dear Juwen, see the file"
        tomorrow = "Mira Voss , The drawing , has attachment, 18 Nov , Dear Juwen, see the file"
        self.assertEqual(gmail.row_key(today), "Mira Voss , The drawing , has attachment , Dear Juwen, see the file")
        self.assertEqual(gmail.row_key(tomorrow), gmail.row_key(today))
        # a key saved by an older Titip still held the day: the same function brings it to the key of today
        saved_before = "Mira Voss , The drawing , has attachment, 18 Nov , Dear Juwen, see the file"
        self.assertEqual(gmail.row_key(saved_before), gmail.row_key(today))
        self.assertEqual(gmail.row_key(gmail.row_key(today)), gmail.row_key(today))
        for when in ("18 Nov", "November 18", "18/11/2026", "8:28 PM"):  # also where the row is longer than a key
            long_row = "unread, Mira Voss , The drawing , has attachment, 08:28 , " + "many words " * 40
            later = long_row.replace("08:28", when)
            self.assertEqual(gmail.row_key(later), gmail.row_key(long_row), when)
            saved_before = re.sub(r"^unread,?\s*", "", " ".join(later.split()))[:300]
            self.assertEqual(gmail.row_key(saved_before), gmail.row_key(long_row), when)
        plain = "unread, Tomas Lind , Your template , 14:31 , " + "many words " * 40
        saved_before = "Tomas Lind , Your template , " + ("many words " * 40).strip()
        self.assertEqual(gmail.row_key(saved_before[:300]), gmail.row_key(plain))

    def test_no_mail_is_believed_only_when_gmail_says_so(self):
        self.assertEqual(gmail.results_state(2, []), "rows")
        self.assertEqual(gmail.results_state(0, ["No messages matched your search. Try using search options"]), "empty")
        self.assertEqual(gmail.results_state(0, ["Loading"]), "loading")

    def test_the_window_strip_sits_on_the_right_most_screen(self):
        self.assertEqual(gmail.edge_place([(0, 0, 1920, 1080), (1920, -200, 4480, 1240)]), (4472, -80))

    def test_subject_and_sender_come_from_what_the_page_shows(self):
        title = "Your template - nadia@mail.example - Gmail - Google Chrome"
        self.assertEqual(gmail.subject_from_title(title, "nadia@mail.example"), "Your template")
        self.assertEqual(gmail.subject_from_title("Inbox - Gmail", "nadia@mail.example"), "")
        cells = ["Tomas Lind tomas@lind.example", "Nadia nadia@mail.example", "Tomas Lind tomas@lind.example"]
        self.assertEqual(gmail.last_sender(cells), ("Tomas Lind", "tomas@lind.example"))
        self.assertEqual(gmail.last_sender(cells[:2]), ("Nadia", "nadia@mail.example"))  # you wrote last
        self.assertEqual(gmail.last_sender(["to me"]), ("", ""))
        ticked = ["Harbor HR Verified sender jobs@harbor.example"]  # the tick is not part of the name
        self.assertEqual(gmail.last_sender(ticked), ("Harbor HR", "jobs@harbor.example"))

    def test_the_last_message_gives_the_sender_not_the_last_address_on_the_page(self):
        # Gmail leaves the address out for somebody you have written to. The thread: you, then Mira.
        cells = ["Nadia nadia@mail.example", "Thu 19 Nov, 15:49 (17 hours ago)", "Mira Okafor", "Thu 19 Nov, 16:51 (16 hours ago)"]
        messages = [
            "Nadia nadia@mail.exampleThu 19 Nov, 15:49 (17 hours ago)to miraDear Mira, a gentle check-in.",
            "Mira OkaforThu 19 Nov, 16:51 (16 hours ago)to meDear Nadia, your profile is under review.",
        ]
        self.assertEqual(gmail.last_header(messages, cells), "Mira Okafor")
        self.assertEqual(gmail.last_sender([gmail.last_header(messages, cells)]), ("", ""))  # details must say
        self.assertEqual(gmail.last_header(messages[:1], cells), "Nadia nadia@mail.example")  # you wrote last
        # A bullet point in a mail is a list item too, and so is a recipient in opened details.
        bullets = messages + ["three years of experience", "Nadia <nadia@mail.example>"]
        self.assertEqual(gmail.last_header(bullets, cells), "Mira Okafor")
        # Spacing differs between a cell and the message that starts with it.
        spaced = ["Mira  Okafor Thu 19 Nov, 16:51 (16 hours ago) to me Dear Nadia"]
        self.assertEqual(gmail.last_header(spaced, cells), "Mira Okafor")
        self.assertEqual(gmail.last_header(["something else entirely"], cells), "")
        # A message can carry words its cells do not, and the other way round.
        verified = ["jobs@harbor.example Verified sender", "08:23 (1 hour ago)"]
        letter = ["jobs@harbor.exampleUnsubscribe08:23 (1 hour ago)to me\n\nDear Nadia, thank you for applying."]
        self.assertEqual(gmail.last_header(letter, verified), "jobs@harbor.example Verified sender")
        self.assertEqual(gmail.last_sender([gmail.last_header(letter, verified)]), ("", "jobs@harbor.example"))
        # The date must stand at the start of the message, not somewhere in its text.
        late = ["Mira Okafor wrote a long letter. " * 5 + "Thu 19 Nov, 16:51 (16 hours ago)"]
        self.assertEqual(gmail.last_header(late, cells), "")
        self.assertEqual(gmail.last_header(messages, []), "")

    def test_show_details_names_the_sender_gmail_left_out(self):
        details = [
            ("gridcell", "Nadia nadia@mail.example"), ("gridcell", "Thu 19 Nov, 15:49 (17 hours ago)"),
            ("gridcell", "Mira Okafor"), ("gridcell", "Thu 19 Nov, 16:51 (16 hours ago)"),
            ("row", ""), ("rowheader", "from:"), ("gridcell", "Mira Okafor mira@harbor.example "),
            ("gridcell", "Mira Okafor mira@harbor.example"),
            ("row", ""), ("rowheader", "to:"), ("gridcell", ""),
            ("row", ""), ("rowheader", "subject:"), ("gridcell", "Re: a check-in"),
        ]
        self.assertEqual(gmail.from_details(details, "Mira Okafor"), ("Mira Okafor", "mira@harbor.example"))
        self.assertEqual(gmail.from_details(details, "mira  okafor"), ("Mira Okafor", "mira@harbor.example"))
        # Details that belong to another message are not believed.
        self.assertEqual(gmail.from_details(details, "Tomas Lind"), ("", ""))
        # Not opened yet, or a 'from:' row without an address.
        self.assertEqual(gmail.from_details(details[:4], "Mira Okafor"), ("", ""))
        self.assertEqual(gmail.from_details(details[:6] + [("row", ""), ("rowheader", "to:"), ("gridcell", "x@y.example")], "Mira Okafor"), ("", ""))
        # Two messages with open details: the last one counts.
        earlier = [("row", ""), ("rowheader", "from:"), ("gridcell", "Nadia nadia@mail.example")]
        self.assertEqual(gmail.from_details(earlier + details, "Mira Okafor"), ("Mira Okafor", "mira@harbor.example"))
        self.assertEqual(gmail.from_details(details + earlier, "Mira Okafor"), ("", ""))
        self.assertEqual(gmail.from_details(details, "Mira Okafor Verified sender"), ("Mira Okafor", "mira@harbor.example"))
        self.assertTrue(gmail.DETAILS_BUTTON.match("Show details"))
        self.assertFalse(gmail.DETAILS_BUTTON.match("Hide details"))

    def test_the_reply_form_must_name_the_one_sender(self):
        form = ["Tomas Lind \n(tomas@lind.example)"]
        self.assertEqual(gmail.form_recipients([], form), ["tomas@lind.example"])
        self.assertEqual(gmail.form_recipients([], ["Billing@Harbor.example"]), ["billing@harbor.example"])
        two = ["Tomas Lind (tomas@lind.example), Mallory (mallory@elsewhere.example)"]
        self.assertEqual(gmail.form_recipients([], two), ["tomas@lind.example", "mallory@elsewhere.example"])
        # A line that was on the page before Reply was pressed is not the form's.
        there = ["Mallory (mallory@elsewhere.example)"]
        self.assertEqual(gmail.form_recipients(there, there + form), ["tomas@lind.example"])
        self.assertEqual(gmail.form_recipients(there, there), [])
        self.assertEqual(gmail.form_recipients(form, form + form), ["tomas@lind.example"])  # the same line, new

    def test_a_long_thread_keeps_its_newest_message(self):
        text = gmail.thread_text(["old " * 3000, "", "the newest message"], limit=500)
        self.assertEqual(len(text), 500)
        self.assertTrue(text.endswith("the newest message"))

    def test_written_text_is_compared_without_caring_about_spacing(self):
        self.assertTrue(gmail.same_text("Hello\n\nNadia", "Hello Nadia\n"))
        self.assertFalse(gmail.same_text("Hello", "Goodbye"))
        self.assertFalse(gmail.same_text("", "anything"))

    def test_buttons_are_told_apart(self):
        self.assertTrue(gmail.SEND_BUTTON.match("Send \u202a(Ctrl-Enter)\u202c"))
        self.assertTrue(gmail.SEND_BUTTON.match("Send"))
        self.assertFalse(gmail.SEND_BUTTON.match("Send feedback to Google"))
        self.assertFalse(gmail.SEND_BUTTON.match("More send options"))
        self.assertTrue(gmail.REPLY_BUTTON.match("Reply"))
        self.assertFalse(gmail.REPLY_BUTTON.match("Reply all"))
        self.assertTrue(gmail.DISCARD_BUTTON.match("Discard draft \u202a(Ctrl-Shift-D)\u202c"))


class Page(gmail._Window):
    """A thread as UI Automation shows it: (control type, role, name). 'Show details' adds the details rows."""

    def __init__(self, elements, details=()):  # no browser, no UI Automation
        self.elements, self.details = list(elements), list(details)
        self.opened, self.unfolded, self.steps = False, False, []

    def title(self):
        return "Re: a check-in - nadia@mail.example - Gmail - Google Chrome" if self.opened else "Search results - Gmail"

    def find(self, control_type, inside=None):
        shown = self.elements + (self.details if self.unfolded else [])
        return [(name, (role, name)) for kind, role, name in shown if kind == control_type]

    def role(self, element):
        return element[0]

    def press(self, element):
        self.opened = True
        return True

    def expand(self, element, opened=True):
        self.unfolded = opened
        self.steps.append("open details" if opened else "close details")
        return True

    def wait(self, ready, seconds, pause=0.5):
        ready()
        return ready()


YOU = [(gmail.ROW, "gridcell", "Nadia nadia@mail.example"), (gmail.ROW, "gridcell", "Thu 19 Nov, 15:49 (17 hours ago)"),
       (gmail.LISTITEM, "listitem", "Nadia nadia@mail.exampleThu 19 Nov, 15:49 (17 hours ago)to miraDear Mira, a gentle check-in.")]
MIRA = [(gmail.ROW, "gridcell", "Mira Okafor"), (gmail.ROW, "gridcell", "Thu 19 Nov, 16:51 (16 hours ago)"),
        (gmail.LISTITEM, "listitem", "Mira OkaforThu 19 Nov, 16:51 (16 hours ago)to meDear Nadia, your profile is under review."),
        (gmail.BUTTON, "button", "Show details")]


def details_of(name, address):
    return [(gmail.ROW, "row", ""), (gmail.ROW, "rowheader", "from:"), (gmail.ROW, "gridcell", f"{name} {address}"),
            (gmail.ROW, "row", ""), (gmail.ROW, "rowheader", "to:"), (gmail.ROW, "gridcell", ""),
            (gmail.LISTITEM, "listitem", "Nadia <nadia@mail.example>")]


class Reading(unittest.TestCase):
    """Who a mail is from. An answer to your own mail shows a name only; you wrote the message above it."""

    def read(self, elements, details=()):
        source = gmail.ChromeGmail()
        source.window = Page(elements, details)
        mail = source.read(gmail.Row("key", "Mira Okafor , Re: a check-in", "opener"), "nadia@mail.example")
        return mail, source.window

    def test_an_answer_to_your_own_mail_is_from_whoever_answered(self):
        mail, page = self.read(YOU + MIRA, details_of("Mira Okafor", "mira@harbor.example"))
        self.assertEqual((mail.sender, mail.address), ("Mira Okafor", "mira@harbor.example"))
        self.assertEqual(mail.subject, "Re: a check-in")
        self.assertTrue(mail.text.endswith("your profile is under review."))  # read before the details opened
        self.assertEqual(page.steps, ["open details", "close details"])

    def test_details_of_somebody_else_leave_the_sender_unknown(self):
        mail, page = self.read(YOU + MIRA, details_of("Nadia", "nadia@mail.example"))
        self.assertEqual((mail.sender, mail.address), ("", ""))  # never "your own message"
        self.assertEqual(page.steps[-1], "close details")
        mail, page = self.read(YOU + MIRA[:3])  # no details to open
        self.assertEqual((mail.sender, mail.address, page.steps), ("", "", []))

    def test_a_sender_the_page_names_needs_no_details(self):
        mail, page = self.read(YOU)
        self.assertEqual((mail.sender, mail.address, page.steps), ("Nadia", "nadia@mail.example", []))
        stranger = [(gmail.ROW, "gridcell", "Tomas Lind Verified sender tomas@lind.example"), (gmail.ROW, "gridcell", "08:23 (1 hour ago)"),
                    (gmail.LISTITEM, "listitem", "Tomas Lind tomas@lind.exampleUnsubscribe08:23 (1 hour ago)to meHi, is it free to use?"),
                    (gmail.LISTITEM, "listitem", "a bullet point in the mail"), (gmail.BUTTON, "button", "Show details")]
        mail, page = self.read(stranger)
        self.assertEqual((mail.sender, mail.address, page.steps), ("Tomas Lind", "tomas@lind.example", []))


class Form(Page):
    """An open mail whose Reply button opens a form for the given recipient lines."""

    def __init__(self, recipients, lines_before=()):
        super().__init__([(gmail.BUTTON, "button", "Reply")] + [(gmail.TEXT, "description", line) for line in lines_before])
        self.recipients, self.written, self.sent = list(recipients), "", False

    def press(self, element):
        name = element[1]
        if name == "Reply":
            self.elements += [(gmail.TEXT, "description", line) for line in self.recipients]
            self.elements += [(gmail.EDIT, "textbox", gmail.REPLY_BOX), (gmail.BUTTON, "button", "Send"),
                              (gmail.BUTTON, "button", "Discard draft")]
        elif name == "Send":
            self.sent = True
            self.elements = [entry for entry in self.elements if entry[0] not in (gmail.EDIT,) and entry[2] != "Send"]
        elif name == "Discard draft":
            self.steps.append("discarded")
            self.elements = [entry for entry in self.elements if entry[0] != gmail.EDIT and entry[2] != "Send"]
        return True

    def text_of(self, element):
        return self.written

    def set_text(self, element, text):
        self.written = text
        return True


class Answering(unittest.TestCase):
    """An answer leaves only when Gmail's own form names the address the mail came from, and no other."""

    def answer(self, recipients, lines_before=()):
        source = gmail.ChromeGmail()
        source.window = page = Form(recipients, lines_before)
        mail = gmail.brain.Mail("nadia@mail.example", "Tomas Lind", "Tomas@lind.example", "Your template", "Hi")
        try:
            source.reply(mail, "Thank you, Tomas.\n\nNadia")
        except gmail.SourceError as error:
            return page, str(error)
        return page, ""

    def test_the_answer_goes_to_the_sender(self):
        page, error = self.answer(["Tomas Lind \n(tomas@lind.example)"])
        self.assertEqual((error, page.sent, page.written), ("", True, "Thank you, Tomas.\n\nNadia"))

    def test_nothing_is_sent_to_another_or_a_second_address(self):
        for recipients in (
            ["Mallory (mallory@elsewhere.example)"],
            ["Tomas Lind (tomas@lind.example)", "Mallory (mallory@elsewhere.example)"],
            [],  # the form names nobody
        ):
            page, error = self.answer(recipients)
            self.assertIn("not addressed to Tomas@lind.example alone", error, recipients)
            self.assertEqual((page.sent, page.written, page.steps), (False, "", ["discarded"]), recipients)

    def test_a_line_from_before_the_form_does_not_count(self):
        line = "Tomas Lind (tomas@lind.example)"
        page, error = self.answer([], lines_before=[line])  # on the page already, the form itself names nobody
        self.assertTrue(error)
        self.assertFalse(page.sent)
        page, error = self.answer(["Tomas Lind \n(tomas@lind.example)"], lines_before=["Mallory (mallory@elsewhere.example)"])
        self.assertEqual((error, page.sent), ("", True))


class FakeScreen:
    """Just enough of win32gui and win32con for close(): a window that asks "Leave site?" a number of times."""

    HWND_BOTTOM, HWND_TOPMOST, SWP_NOSIZE, SWP_NOACTIVATE, WM_CLOSE = 1, -1, 1, 16, 16

    def __init__(self, questions):
        self.questions, self.open, self.asking, self.closes = questions, True, False, 0

    def PostMessage(self, handle, message, low, high):
        self.closes += 1
        if self.questions > 0:
            self.questions -= 1
            self.asking = True
        else:
            self.open = False

    def IsWindow(self, handle):
        return self.open

    def IsWindowVisible(self, handle):
        return True

    def IsIconic(self, handle):
        return False

    def SetWindowPos(self, *arguments):
        pass

    def SetForegroundWindow(self, handle):
        pass


class FakeWindow(gmail._Window):
    def __init__(self, questions):  # no browser, no UI Automation
        self.gui = self.con = FakeScreen(questions)
        self.handle, self.foreground, self.edge, self.home = 7, 3, (1900, 120), (600, 10)
        self.answers = []

    def owned_by(self, handle):
        return ["question"] if self.gui.asking else []

    def answer(self, dialog, leave):
        self.answers.append("leave" if leave else "stay")
        self.gui.asking = False
        if leave:
            self.gui.open = False
        return True


class Closing(unittest.TestCase):
    """Gmail saves in the background. Closing too early makes Chrome ask "Leave site?" on your screen."""

    def close(self, questions):
        window = FakeWindow(questions)
        with mock.patch("titip.gmail.time.sleep"):
            window.close()
        return window

    def test_a_window_that_just_closes_is_closed_once(self):
        window = self.close(0)
        self.assertEqual((window.gui.closes, window.answers, window.gui.open), (1, [], False))
        self.assertIsNone(window.handle)

    def test_while_gmail_saves_it_stays_then_closes_again(self):
        window = self.close(1)
        self.assertEqual((window.gui.closes, window.answers, window.gui.open), (2, ["stay"], False))

    def test_a_question_that_keeps_coming_is_answered_in_the_end(self):
        window = self.close(99)
        self.assertEqual(window.answers, ["stay", "stay", "leave"])
        self.assertFalse(window.gui.open)

    def test_unread_and_dialog_buttons_are_told_apart(self):
        self.assertTrue(gmail.UNREAD_BUTTON.match("Mark as unread"))
        self.assertFalse(gmail.UNREAD_BUTTON.match("Mark as read"))
        self.assertTrue(gmail.LEAVE_BUTTON.match("Leave"))
        self.assertTrue(gmail.STAY_BUTTON.match("Cancel"))
        self.assertFalse(gmail.STAY_BUTTON.match("Leave"))


if __name__ == "__main__":
    unittest.main()
