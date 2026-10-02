"""Create, check, render, edit and fill PDF files. Run with --help for the commands."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf_tools.cli import main  # type: ignore[import-not-found]  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
