"""Create a PDF from an HTML file with a Chromium-based browser, or from Markdown."""

from __future__ import annotations

import html.parser
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from .common import (
    AtomicOutput,
    CommandError,
    human_size,
    output_path,
    require_modules,
    tool_command,
)
from .env import browser_install_advice, find_browser, uncovered_characters

HTML_SUFFIXES = (".html", ".htm", ".xhtml")
MARKDOWN_SUFFIXES = (".md", ".markdown")


def create(
    source_value: str, target_value: str, browser: str, wait: float, timeout: float
) -> list[str]:
    source = Path(source_value).expanduser().resolve()
    if not source.is_file():
        raise CommandError(f"File not found: {source}")
    target = output_path(target_value, inputs=[source])
    replaced = target.exists()
    suffix = source.suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        require_modules("reportlab", "pypdf")
        from .markdown import build_markdown

        engine_lines = build_markdown(source, target)
        return _summary(target, "Markdown without a browser", engine_lines, None, replaced)
    if suffix not in HTML_SUFFIXES:
        raise CommandError(
            f"Unsupported source type {source.suffix or '(none)'}: give an .html file, or a .md "
            "file for the Markdown builder."
        )
    require_modules("pypdf")
    found = find_browser(browser or None)
    if found.path is None:
        advice = " ".join(browser_install_advice())
        raise CommandError(
            f"No browser can print HTML here. {found.problem} {advice} Until a browser is "
            "installed, build the document from Markdown as described in the Skill's "
            "references/without-browser.md."
        )
    _print_with_browser(found.path, source, target, wait, timeout)
    text = _visible_text(source)
    return _summary(target, Path(found.path).name, [], text, replaced)


def _print_with_browser(
    browser: str, source: Path, target: Path, wait: float, timeout: float
) -> None:
    profile = tempfile.mkdtemp(prefix="vbot-pdf-browser-")
    try:
        with AtomicOutput(target) as output:
            output.path.unlink()
            arguments = _arguments(browser, profile, output.path, source, wait)
            completed = _run(arguments, timeout)
            if (
                not output.path.is_file()
                and sys.platform.startswith("linux")
                and "--no-sandbox" not in arguments
            ):
                arguments.insert(1, "--no-sandbox")
                completed = _run(arguments, timeout)
            if not output.path.is_file() or output.path.stat().st_size == 0:
                detail = _relevant_output(completed.stdout + completed.stderr)
                hint = ""
                if os.path.realpath(browser).startswith("/snap/"):
                    hint = (
                        " This browser is a Snap package and can only read and write files in "
                        "the home folder outside hidden folders; move the HTML file and the "
                        "output there."
                    )
                raise CommandError(
                    f"The browser did not write the PDF (exit code {completed.returncode}).{hint}"
                    + (f" Browser output: {detail}" if detail else "")
                )
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def _arguments(browser: str, profile: str, output: Path, source: Path, wait: float) -> list[str]:
    return [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--hide-scrollbars",
        f"--user-data-dir={profile}",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        "--generate-pdf-document-outline",
        "--export-tagged-pdf",
        "--run-all-compositor-stages-before-draw",
        f"--virtual-time-budget={int(wait * 1000)}",
        f"--print-to-pdf={output}",
        source.as_uri(),
    ]


def _run(arguments: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise CommandError(
            f"The browser did not finish within {timeout:g} seconds. Remove scripts or remote "
            "resources that keep loading, or pass a larger --timeout."
        ) from error
    except OSError as error:
        raise CommandError(f"Cannot start the browser {arguments[0]}: {error}") from error


def _relevant_output(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful = [line for line in lines if "ERROR" in line or "error" in line.lower()]
    return " | ".join((useful or lines)[-3:])[:600]


if TYPE_CHECKING:
    from typing_extensions import override
else:  # typing.override needs Python 3.12; these tools also run on 3.9
    _Function = TypeVar("_Function", bound=Callable[..., Any])

    def override(function: _Function) -> _Function:
        return function


class _TextCollector(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    @override
    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _visible_text(source: Path) -> str:
    collector = _TextCollector()
    try:
        collector.feed(source.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""
    return "".join(collector.parts)


def _summary(
    target: Path, engine: str, engine_lines: list[str], source_text: object, replaced: bool
) -> list[str]:
    from .inspection import analyse

    report = analyse(target, check_blank=True)
    first = (
        f"Created {target.name}: {report.page_count} page{'s' if report.page_count != 1 else ''}, "
        f"{report.size_summary()}, {human_size(target.stat().st_size)} ({engine})."
    )
    lines = [first, f"File: {target}"]
    if report.fonts:
        lines.append("Fonts: " + ", ".join(report.fonts))
    problems = list(engine_lines) + report.problems
    if isinstance(source_text, str):
        if not report.title:
            problems.append(
                "The document has no title: add <title> to the HTML head; PDF viewers show it "
                "instead of the file name."
            )
        if not report.language:
            problems.append(
                'The document has no language: add lang to the html tag, e.g. <html lang="de">; '
                "hyphenation and screen readers depend on it."
            )
    if report.missing_glyph_pages and isinstance(source_text, str):
        absent = uncovered_characters(source_text)
        if absent:
            shown = ", ".join(f"{char} (U+{ord(char):04X})" for char, _ in absent[:12])
            packages = " ".join(sorted({package for _, package in absent}))
            problems.append(
                f"No installed font has these characters: {shown}. Replace them, or ask the user "
                "to install fonts (Debian, Ubuntu, Raspberry Pi OS: "
                f"`sudo apt install {packages}`)."
            )
    if problems:
        lines.append("Problems:")
        lines.extend(f"- {problem}" for problem in problems)
    else:
        lines.append("No structural problems found.")
    render = tool_command(f"render {target.as_posix()} <folder>")
    if replaced:
        lines.append(
            f"Next: this replaced the earlier {target.name}, so page images rendered before show "
            f"the old version. Render again with `{render}` and read the new images."
        )
    else:
        lines.append(f"Next: check the look with `{render}` and read the images.")
    return lines
