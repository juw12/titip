"""Repo hygiene: clean text files, no private data, and the web demo matches the Python demo."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = {".py", ".pyw", ".js", ".css", ".html", ".json", ".md", ".toml", ".webmanifest", ".svg", ".ps1", ".cmd", ".yml"}
FILES = [
    path for path in ROOT.rglob("*")
    if path.is_file() and path.suffix in TEXT and not {".git", "__pycache__", "shots"} & set(path.parts)
]
PRIVATE = re.compile(r"pegatron|juwen_lie|10\.175\.|\.pst\b.*D:\\\\", re.IGNORECASE)


class Hygiene(unittest.TestCase):
    def test_text_files_are_utf8_without_bom_or_mojibake(self):
        self.assertGreater(len(FILES), 15)
        for path in FILES:
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"BOM in {path.name}")
            text = raw.decode("utf-8")
            for bad in ("\u00c2\u00b7", "\u00e2\u20ac", "\ufffd"):
                self.assertNotIn(bad, text, f"mojibake in {path.name}")

    def test_no_company_or_personal_data_in_the_repo(self):
        for path in FILES:
            if path.name == "test_repo.py":
                continue
            match = PRIVATE.search(path.read_text(encoding="utf-8"))
            self.assertIsNone(match, f"{path.name}: {match.group(0) if match else ''}")

    def test_demo_mail_is_invented(self):
        demo = json.loads((ROOT / "titip" / "web" / "demo.json").read_text(encoding="utf-8"))
        self.assertTrue(demo["me"]["address"].endswith(".example"))
        for mail in demo["mail"]:
            self.assertTrue(mail["fromAddress"].endswith(".example"), mail["subject"])
            self.assertIn(mail["folder"], ("inbox", "sent", "drafts", "outbox"))

    def test_public_demo_is_the_same_web_app(self):
        for name in ("index.html", "app.js", "far.js", "auto.html", "auto.js", "home.html", "home.js", "styles.css", "demo.json", "icon.svg", "icon-192.png", "manifest.webmanifest",
                     "instrument-serif.woff2", "geist.woff2", "geist-mono.woff2"):
            self.assertEqual(
                (ROOT / "docs" / name).read_bytes(), (ROOT / "titip" / "web" / name).read_bytes(),
                f"docs/{name} is stale: run python scripts/sync_docs.py",
            )

    def test_the_phone_page_names_the_version_it_is(self):
        """A Home Screen icon can keep an old page for days; the version under the list tells which one runs."""
        from titip import __version__

        script = (ROOT / "titip" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn(f'var VERSION = "{__version__}";', script)

    def test_mail_text_never_goes_through_innerhtml(self):
        for name in ("app.js", "far.js", "auto.js", "home.js", "desk.js", "pair.js"):
            script = (ROOT / "titip" / "web" / name).read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", script.split("*/", 1)[1], name)
            self.assertNotIn("eval(", script, name)

    def test_the_iphone_home_screen_page_keeps_its_whole_address(self):
        """An iPhone icon gets its own storage; the key reaches it only in the address of the page the
        icon was made from, and an iPhone keeps that whole address only when the page has no manifest."""
        web = ROOT / "titip" / "web"
        page = (web / "home.html").read_text(encoding="utf-8")
        self.assertNotIn('rel="manifest"', page)
        self.assertIn('name="apple-mobile-web-app-capable"', page)
        self.assertIn('rel="apple-touch-icon"', page)
        self.assertIn('content="no-referrer"', page)
        for name in ("far.js", "auto.js"):  # an iPhone that scans the code is sent on to that page
            self.assertIn('location.replace("home.html', (web / name).read_text(encoding="utf-8"), name)
        script = (web / "home.js").read_text(encoding="utf-8")
        self.assertIn("READY_MS = 300000", script)  # the address holds the key for five minutes, no longer
        self.assertNotIn("document.cookie", script)  # a cookie would travel to the server that hosts the page


if __name__ == "__main__":
    unittest.main()
