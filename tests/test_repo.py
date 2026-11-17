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
            self.assertIn(mail["folder"], ("inbox", "sent", "drafts"))

    def test_public_demo_is_the_same_web_app(self):
        for name in ("index.html", "app.js", "auto.html", "auto.js", "styles.css", "demo.json", "icon.svg", "manifest.webmanifest"):
            self.assertEqual(
                (ROOT / "docs" / name).read_bytes(), (ROOT / "titip" / "web" / name).read_bytes(),
                f"docs/{name} is stale: run python scripts/sync_docs.py",
            )

    def test_mail_text_never_goes_through_innerhtml(self):
        for name in ("app.js", "auto.js", "desk.js", "pair.js"):
            script = (ROOT / "titip" / "web" / name).read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", script.split("*/", 1)[1], name)
            self.assertNotIn("eval(", script, name)


if __name__ == "__main__":
    unittest.main()
