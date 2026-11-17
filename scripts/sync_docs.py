"""Copy the web app into docs/ so GitHub Pages can serve the demo (it runs on invented mail there).

    python scripts/sync_docs.py

tests/test_repo.py fails when docs/ is older than titip/web.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ("index.html", "app.js", "styles.css", "demo.json", "icon.svg", "icon-192.png", "manifest.webmanifest")


def main() -> None:
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    for name in PUBLIC:
        shutil.copyfile(ROOT / "titip" / "web" / name, docs / name)
    (docs / ".nojekyll").write_text("", encoding="utf-8")
    print(f"copied {len(PUBLIC)} files to {docs}")


if __name__ == "__main__":
    main()
