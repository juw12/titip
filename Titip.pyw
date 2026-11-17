"""Double-click to start Titip without a console window (or open its pairing page if it already runs)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from titip.__main__ import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
