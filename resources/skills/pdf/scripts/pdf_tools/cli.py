"""Create, check, render, edit and fill PDF files.

Every command prints its result on the first line. Page numbers are 1-based; page ranges look
like 1-3,7,9- (9- means page 9 to the end). Commands that write a file never overwrite their input.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .common import SCRIPT, CommandError, print_lines
from .env import use_private_environment

PAGES_HELP = "Pages as 1-based ranges, e.g. 1-3,7,9- (default: all pages)."
PASSWORD_HELP = "Password of an encrypted input PDF."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    setup = commands.add_parser("setup", help="Show what is installed; install missing packages.")
    setup.add_argument(
        "--install",
        action="store_true",
        help="Install the Python packages into a private environment for these tools.",
    )
    setup.add_argument(
        "--install-browser",
        action="store_true",
        help="Also download Chromium for these tools only (about 300 MB, no admin rights).",
    )

    create = commands.add_parser(
        "create", help="Create a PDF from an .html file (browser) or a .md file."
    )
    create.add_argument("source", help="The .html file, or a .md file for the Markdown builder.")
    create.add_argument("output", help="The PDF to write.")
    create.add_argument("--browser", default="", help="Path of a Chromium-based browser to use.")
    create.add_argument(
        "--wait",
        type=float,
        default=5.0,
        help="Seconds the page may load fonts and images before printing (default 5).",
    )
    create.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Seconds before the browser is stopped (default 120).",
    )

    inspect = commands.add_parser(
        "inspect", help="Report pages, fonts, metadata and visible defects."
    )
    inspect.add_argument("pdf")
    inspect.add_argument("--password", help=PASSWORD_HELP)

    render = commands.add_parser("render", help="Render pages to PNG files plus overview sheets.")
    render.add_argument("pdf")
    render.add_argument(
        "folder", help="Folder for the PNG files; old renders of this PDF there are replaced."
    )
    render.add_argument("--pages", help=PAGES_HELP)
    render.add_argument(
        "--size",
        type=int,
        default=1600,
        help="Long edge of each page image in pixels (default 1600).",
    )
    render.add_argument("--dpi", type=int, help="Render at this resolution instead of --size.")
    render.add_argument("--no-overview", action="store_true", help="Skip the overview sheets.")
    render.add_argument(
        "--grid",
        action="store_true",
        help="Draw a millimetre grid over each page to read off positions.",
    )
    render.add_argument(
        "--area",
        help="Render only this part of each page, in millimetres from the top-left corner: "
        "LEFT,TOP,WIDTH,HEIGHT, e.g. 20,80,60,30. --size then applies to the part.",
    )
    render.add_argument("--password", help=PASSWORD_HELP)

    fields = commands.add_parser(
        "fields", help="List the fillable form fields with their values and options."
    )
    fields.add_argument("pdf")
    fields.add_argument(
        "--template", help="Also write the fillable fields with current values to this .json file."
    )
    fields.add_argument("--password", help=PASSWORD_HELP)

    fill = commands.add_parser(
        "fill", help="Fill form fields from a JSON object of field name to value."
    )
    fill.add_argument("pdf")
    fill.add_argument("values", help='.json file: {"Field name": "text", "Checkbox": true, ...}')
    fill.add_argument("output")
    fill.add_argument(
        "--flatten",
        action="store_true",
        help="Turn the values into fixed page content; the result is no longer a form.",
    )
    fill.add_argument(
        "--break-signatures",
        action="store_true",
        help="Allow filling a signed PDF, which invalidates its signatures.",
    )
    fill.add_argument("--password", help=PASSWORD_HELP)

    merge = commands.add_parser("merge", help="Join PDFs in the given order.")
    merge.add_argument("inputs", nargs="+")
    merge.add_argument("-o", "--output", help="The PDF to write.")
    merge.add_argument("--bookmarks", action="store_true", help="Add one bookmark per input file.")

    select = commands.add_parser(
        "select",
        help="Keep, remove, reorder or rotate pages into a new PDF.",
        description=(
            "Write the selected pages in the given order. Remove pages by leaving them out."
        ),
    )
    select.add_argument("pdf")
    select.add_argument("output")
    select.add_argument(
        "--pages", help="Pages to keep, in output order, e.g. 3,1-2 or 1-4,6- (default: all)."
    )
    select.add_argument(
        "--rotate",
        action="append",
        default=[],
        help="PAGES:DEGREES clockwise, e.g. 2:90 or 3-5:180; repeatable; input page numbers.",
    )
    select.add_argument("--password", help=PASSWORD_HELP)

    split = commands.add_parser("split", help="Write one PDF per page or per page range.")
    split.add_argument("pdf")
    split.add_argument("folder")
    split.add_argument(
        "--ranges", help="Page groups separated by commas, one file per group, e.g. 1-3,4-7,8-."
    )
    split.add_argument(
        "--every", type=int, default=1, help="Pages per file when --ranges is absent (default 1)."
    )
    split.add_argument("--password", help=PASSWORD_HELP)

    stamp = commands.add_parser(
        "stamp", help="Put text or another PDF page on pages (watermark, letterhead)."
    )
    stamp.add_argument("pdf")
    stamp.add_argument("output")
    stamp.add_argument("--text", help="Text to stamp, e.g. ENTWURF or CONFIDENTIAL.")
    stamp.add_argument(
        "--overlay", help="PDF whose first page (or matching page) is placed on each page."
    )
    stamp.add_argument("--pages", help=PAGES_HELP)
    stamp.add_argument(
        "--position",
        default="center",
        choices=["center", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"],
        help="Where --text goes (default center, along the diagonal).",
    )
    stamp.add_argument(
        "--size",
        type=float,
        default=0,
        help="Text size in points (default 72 centered, 11 elsewhere).",
    )
    stamp.add_argument(
        "--angle", type=float, help="Text angle in degrees for center (default: the page diagonal)."
    )
    stamp.add_argument(
        "--opacity", type=float, default=0, help="0.05-1 (default 0.15 centered, 0.8 elsewhere)."
    )
    stamp.add_argument("--color", default="#B91C1C", help="Hex text color (default #B91C1C).")
    stamp.add_argument(
        "--under", action="store_true", help="Put the stamp behind the page content."
    )
    stamp.add_argument("--password", help=PASSWORD_HELP)

    compress = commands.add_parser("compress", help="Shrink a PDF by re-encoding large images.")
    compress.add_argument("pdf")
    compress.add_argument("output")
    compress.add_argument(
        "--quality", type=int, default=72, help="JPEG quality 30-95 (default 72)."
    )
    compress.add_argument(
        "--max-pixels",
        type=int,
        default=2000,
        help="Longest image side in pixels after compression (default 2000).",
    )
    compress.add_argument("--password", help=PASSWORD_HELP)

    encrypt = commands.add_parser("encrypt", help="Protect a PDF with a password (AES-256).")
    encrypt.add_argument("pdf")
    encrypt.add_argument("output")
    encrypt.add_argument("--password", required=True, help="Password needed to open the PDF.")
    encrypt.add_argument("--owner-password", help="Separate password for changing permissions.")

    decrypt = commands.add_parser("decrypt", help="Write a copy without password protection.")
    decrypt.add_argument("pdf")
    decrypt.add_argument("output")
    decrypt.add_argument("--password", required=True)
    return parser


def _run(arguments: argparse.Namespace) -> list[str]:
    command = arguments.command
    if command == "setup":
        from .env import setup

        return setup(arguments.install, arguments.install_browser)
    if command == "create":
        from .create import create

        return create(
            arguments.source, arguments.output, arguments.browser, arguments.wait, arguments.timeout
        )
    if command == "inspect":
        from .inspection import inspect

        return inspect(arguments.pdf, arguments.password)
    if command == "render":
        from .inspection import render

        return render(
            arguments.pdf,
            arguments.folder,
            arguments.pages,
            arguments.size,
            arguments.dpi,
            not arguments.no_overview,
            arguments.password,
            arguments.grid,
            arguments.area,
        )
    if command == "fields":
        from .forms import list_fields

        return list_fields(arguments.pdf, arguments.template, arguments.password)
    if command == "fill":
        from .forms import fill

        return fill(
            arguments.pdf,
            arguments.values,
            arguments.output,
            arguments.flatten,
            arguments.break_signatures,
            arguments.password,
        )
    from .edit import compress, decrypt, encrypt, merge, select, split, stamp

    if command == "merge":
        return merge(arguments.inputs, arguments.output, arguments.bookmarks)
    if command == "select":
        return select(
            arguments.pdf, arguments.output, arguments.pages, arguments.rotate, arguments.password
        )
    if command == "split":
        return split(
            arguments.pdf, arguments.folder, arguments.ranges, arguments.every, arguments.password
        )
    if command == "stamp":
        centered = arguments.position == "center"
        size = arguments.size or (72.0 if centered else 11.0)
        opacity = arguments.opacity or (0.15 if centered else 0.8)
        return stamp(
            arguments.pdf,
            arguments.output,
            arguments.text,
            arguments.overlay,
            arguments.pages,
            arguments.position,
            size,
            arguments.angle,
            opacity,
            arguments.color,
            arguments.under,
            arguments.password,
        )
    if command == "compress":
        if not 30 <= arguments.quality <= 95:
            raise CommandError("--quality must be between 30 and 95.")
        return compress(
            arguments.pdf,
            arguments.output,
            arguments.quality,
            arguments.max_pixels,
            arguments.password,
        )
    if command == "encrypt":
        return encrypt(
            arguments.pdf, arguments.output, arguments.password, arguments.owner_password
        )
    if command == "decrypt":
        return decrypt(arguments.pdf, arguments.output, arguments.password)
    raise CommandError("Name a command; run with --help to list them.")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    use_private_environment(SCRIPT)
    # pypdf logs recoverable defects of input files; the commands report what matters.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    parser = _parser()
    arguments = parser.parse_args()
    if not arguments.command:
        parser.print_help()
        return 2
    try:
        print_lines(_run(arguments))
    except CommandError as error:
        print_lines([f"Failed: {error}"])
        return 1
    except Exception as error:  # report unexpected failures in one readable block
        import traceback

        frame = traceback.extract_tb(error.__traceback__)[-1]
        print_lines(
            [
                f"Failed unexpectedly: {type(error).__name__}: {error}",
                f"At {Path(frame.filename).name}:{frame.lineno}. If the input is unusual, "
                "simplify it and retry; otherwise report this message to the user.",
            ]
        )
        return 1
    return 0
