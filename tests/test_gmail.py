"""The parts of the Gmail reader that need no browser."""
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

    def test_the_reply_form_must_name_the_one_sender(self):
        groups = ["to me Show details", "Tomas Lind tomas@lind.example", "to Tomas Show details"]
        self.assertEqual(gmail.reply_names(groups), ["me", "tomas"])
        self.assertTrue(gmail.names_match("tomas", "Tomas Lind", "tomas@lind.example"))
        self.assertTrue(gmail.names_match("billing", "", "billing@harbor.example"))
        self.assertFalse(gmail.names_match("mallory", "Tomas Lind", "tomas@lind.example"))
        self.assertFalse(gmail.names_match("tomas, mallory", "Tomas Lind", "tomas@lind.example"))
        self.assertFalse(gmail.names_match("", "Tomas Lind", "tomas@lind.example"))

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
