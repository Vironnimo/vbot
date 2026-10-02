"""Shared helpers of the PDF command line: errors, page ranges, paths and sizes."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pdf.py"
PACKAGE_NAMES = {"PIL": "pillow", "pypdf": "pypdf", "pypdfium2": "pypdfium2"}
PACKAGE_NAMES.update({"reportlab": "reportlab", "cryptography": "cryptography"})

PAPER_SIZES_MM = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "A6": (105.0, 148.0),
    "Letter": (215.9, 279.4),
    "Legal": (215.9, 355.6),
}


class CommandError(Exception):
    """An expected failure; its message is printed as the command's result."""


def python_command() -> str:
    """The command a user types for this Python on this platform."""
    return "python" if os.name == "nt" else "python3"


def tool_command(arguments: str) -> str:
    return f"{python_command()} {SCRIPT.as_posix()} {arguments}"


def require_modules(*modules: str) -> None:
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return
    packages = ", ".join(PACKAGE_NAMES.get(name, name) for name in missing)
    raise CommandError(
        f"Missing Python packages: {packages}. Run `{tool_command('setup --install')}`, "
        "then repeat this command."
    )


def existing_pdf(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise CommandError(f"File not found: {path}")
    return path


def output_path(value: str, *, inputs: Sequence[Path] = (), suffix: str = ".pdf") -> Path:
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() != suffix:
        raise CommandError(f"The output file must end in {suffix}: {path}")
    for source in inputs:
        if path == source.resolve():
            raise CommandError(
                f"The output would overwrite the input {source.name}. Choose a new file name, "
                f"for example {source.stem}-edited{suffix}."
            )
    if path.is_dir():
        raise CommandError(f"The output is a folder, not a file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class AtomicOutput:
    """A temporary file next to the target that replaces it only after success."""

    def __init__(self, target: Path) -> None:
        self.target = target
        handle, name = tempfile.mkstemp(dir=str(target.parent), suffix=target.suffix)
        os.close(handle)
        self.path = Path(name)

    def __enter__(self) -> AtomicOutput:
        return self

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        if kind is None and self.path.is_file() and self.path.stat().st_size > 0:
            os.replace(self.path, self.target)
            return
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """Turn `1-3,7,9-` into 0-based page indexes, in the given order."""
    if spec is None or spec.strip().lower() in ("", "all"):
        return list(range(page_count))
    indexes: list[int] = []
    for raw in spec.replace(" ", "").split(","):
        if not raw:
            continue
        indexes.extend(_page_range(raw, page_count, spec))
    if not indexes:
        raise CommandError(f"No pages selected by '{spec}'.")
    return indexes


def _page_range(part: str, page_count: int, spec: str) -> Iterator[int]:
    def number(text: str) -> int:
        if text.lower() in ("last", "end"):
            return page_count
        if not text.isdigit():
            raise CommandError(
                f"Invalid page range '{spec}'. Use 1-based page numbers such as 1-3,7,9- "
                "(9- means page 9 to the end)."
            )
        value = int(text)
        if not 1 <= value <= page_count:
            raise CommandError(f"Page {value} does not exist; the PDF has {page_count} pages.")
        return value

    if "-" in part:
        start_text, end_text = part.split("-", 1)
        start = number(start_text) if start_text else 1
        end = number(end_text) if end_text else page_count
        step = 1 if end >= start else -1
        return iter(range(start - 1, end - 1 + step, step))
    return iter([number(part) - 1])


def describe_pages(indexes: Sequence[int]) -> str:
    """Render 0-based indexes as compact 1-based ranges: 1-3, 7."""
    parts: list[str] = []
    start = previous = None
    for index in indexes:
        page = index + 1
        if start is not None and previous is not None and page == previous + 1:
            previous = page
            continue
        if start is not None:
            parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    if start is not None:
        parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(parts)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def paper_name(width_pt: float, height_pt: float) -> str:
    """Name a page size, e.g. `A4 portrait` or `200 x 100 mm`."""
    width_mm = width_pt * 25.4 / 72
    height_mm = height_pt * 25.4 / 72
    short, long = sorted((width_mm, height_mm))
    orientation = "portrait" if height_mm >= width_mm else "landscape"
    for name, (paper_short, paper_long) in PAPER_SIZES_MM.items():
        if abs(short - paper_short) <= 1.5 and abs(long - paper_long) <= 1.5:
            return f"{name} {orientation}"
    return f"{width_mm:.0f} x {height_mm:.0f} mm"


def open_reader(path: Path, password: str | None = None):  # type: ignore[no-untyped-def]
    """Open a PDF with pypdf; an encrypted file needs its password."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as error:  # pypdf raises many undocumented types for broken files
        raise CommandError(f"Cannot read {path.name} as a PDF: {error}") from error
    if reader.is_encrypted and reader.decrypt(password or "") == 0:
        if password:
            raise CommandError(f"Wrong password for {path.name}.")
        raise CommandError(
            f"{path.name} is password-protected. Repeat the command with "
            "`--password <password>`; for a merge input or an overlay, decrypt the file first: "
            f"`{tool_command('decrypt <file.pdf> <decrypted.pdf> --password <password>')}`. "
            "If you do not have the password, ask the user for it."
        )
    return reader


def print_lines(lines: Sequence[str]) -> None:
    sys.stdout.write("\n".join(lines) + "\n")
