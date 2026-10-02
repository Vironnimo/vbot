"""Contracts of the bundled pdf Skill's command-line tool, ``scripts/pdf.py``.

The tool runs in-process: the commands are the Agent-visible interface, and every one prints
its result on the first line and exits non-zero after ``Failed:``.
"""

from __future__ import annotations

import importlib
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = PROJECT_ROOT / "resources" / "skills" / "pdf"
SCRIPTS = SKILL_ROOT / "scripts"
MODULES = ("cli", "common", "create", "edit", "env", "fonts", "forms", "inspection", "markdown")

Run = Callable[..., tuple[int, list[str]]]


def _load_package() -> dict[str, ModuleType]:
    """Import every module up front so no command writes bytecode into the Skill."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPTS))
    try:
        return {name: importlib.import_module(f"pdf_tools.{name}") for name in MODULES}
    finally:
        sys.path.remove(str(SCRIPTS))
        sys.dont_write_bytecode = previous


PDF_TOOLS = _load_package()


@pytest.fixture
def pdf(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Run:
    # The commands would otherwise run again inside a private environment of this machine.
    monkeypatch.setenv("VBOT_PDF_TOOLS_REEXEC", "1")

    def run(*arguments: object) -> tuple[int, list[str]]:
        monkeypatch.setattr(sys, "argv", ["pdf.py", *map(str, arguments)])
        code = PDF_TOOLS["cli"].main()
        return code, capsys.readouterr().out.splitlines()

    return run


def _blank_pdf(path: Path, widths: list[int]) -> Path:
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=842)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _widths(path: Path) -> list[int]:
    return [round(float(page.mediabox.width)) for page in PdfReader(path).pages]


def _form_pdf(path: Path) -> Path:
    from reportlab.pdfgen.canvas import Canvas

    canvas = Canvas(str(path))
    form = canvas.acroForm
    form.textfield(name="name", x=100, y=700, width=200, height=20, maxlen=10)
    form.checkbox(name="agree", x=100, y=650, buttonStyle="check")
    form.radio(name="size", value="small", x=100, y=600, selected=True)
    form.radio(name="size", value="large", x=150, y=600)
    form.choice(
        name="colour", options=["red", "green"], value="red", x=100, y=550, width=100, height=20
    )
    canvas.showPage()
    canvas.save()
    return path


def test_page_operations_follow_the_requested_pages(pdf: Run, tmp_path: Path) -> None:
    source = _blank_pdf(tmp_path / "in.pdf", [100, 200, 300, 400])

    code, lines = pdf(
        "select", source, tmp_path / "picked.pdf", "--pages", "3,1", "--rotate", "1:90"
    )
    assert code == 0 and lines[0].startswith("Kept pages")
    picked = PdfReader(tmp_path / "picked.pdf")
    assert _widths(tmp_path / "picked.pdf") == [300, 100]
    assert [page.rotation for page in picked.pages] == [0, 90]

    code, _ = pdf(
        "merge", source, tmp_path / "picked.pdf", "-o", tmp_path / "joined.pdf", "--bookmarks"
    )
    assert code == 0
    joined = PdfReader(tmp_path / "joined.pdf")
    assert _widths(tmp_path / "joined.pdf") == [100, 200, 300, 400, 300, 100]
    assert len(joined.outline) == 2

    code, _ = pdf("split", source, tmp_path / "parts", "--ranges", "1-2,3-")
    assert code == 0
    parts = sorted((tmp_path / "parts").glob("*.pdf"))
    assert [_widths(part) for part in parts] == [[100, 200], [300, 400]]

    code, lines = pdf("select", source, source, "--pages", "1")
    assert code == 1 and lines[0].startswith("Failed:")
    assert _widths(source) == [100, 200, 300, 400]


def test_stamp_puts_text_or_matching_overlay_pages_on_the_pages(pdf: Run, tmp_path: Path) -> None:
    source = _blank_pdf(tmp_path / "in.pdf", [595, 595, 595])

    code, _ = pdf("stamp", source, tmp_path / "draft.pdf", "--text", "ENTWURF", "--pages", "2")
    assert code == 0
    texts = [page.extract_text() for page in PdfReader(tmp_path / "draft.pdf").pages]
    assert ["ENTWURF" in text for text in texts] == [False, True, False]

    code, lines = pdf(
        "stamp",
        source,
        tmp_path / "bad.pdf",
        "--overlay",
        _blank_pdf(tmp_path / "two.pdf", [595, 595]),
    )
    assert code == 1 and lines[0].startswith("Failed:")
    assert not (tmp_path / "bad.pdf").exists()

    code, _ = pdf(
        "stamp", source, tmp_path / "under.pdf", "--overlay", tmp_path / "draft.pdf", "--under"
    )
    assert code == 0
    texts = [page.extract_text() for page in PdfReader(tmp_path / "under.pdf").pages]
    assert ["ENTWURF" in text for text in texts] == [False, True, False]


def test_form_fill_validates_everything_before_writing(pdf: Run, tmp_path: Path) -> None:
    form = _form_pdf(tmp_path / "form.pdf")
    template = tmp_path / "values.json"

    code, _ = pdf("fields", form, "--template", template)
    assert code == 0
    assert set(json.loads(template.read_text(encoding="utf-8"))) == {
        "name",
        "agree",
        "size",
        "colour",
    }

    wrong = tmp_path / "wrong.json"
    wrong.write_text(
        json.dumps({"name": "far too long a name", "size": "medium", "nmae": "x"}), encoding="utf-8"
    )
    code, lines = pdf("fill", form, wrong, tmp_path / "wrong.pdf")
    assert code == 1 and lines[0].startswith("Failed:")
    assert not (tmp_path / "wrong.pdf").exists()

    values = tmp_path / "filled.json"
    values.write_text(
        json.dumps({"name": "Anna", "agree": True, "size": "large", "colour": "green"}),
        encoding="utf-8",
    )
    code, lines = pdf("fill", form, values, tmp_path / "filled.pdf")
    assert code == 0 and lines[0].startswith("Filled 4 fields")
    fields = PdfReader(tmp_path / "filled.pdf").get_fields() or {}
    assert fields["name"]["/V"] == "Anna"
    assert fields["agree"]["/V"] != "/Off"
    assert fields["size"]["/V"] == "/large"
    assert fields["colour"]["/V"] == "green"

    code, _ = pdf("fill", form, values, tmp_path / "flat.pdf", "--flatten")
    assert code == 0
    flat = PdfReader(tmp_path / "flat.pdf")
    assert not flat.get_fields()
    assert "Anna" in flat.pages[0].extract_text()


def test_password_protection_round_trip(pdf: Run, tmp_path: Path) -> None:
    source = _blank_pdf(tmp_path / "in.pdf", [595, 595])

    assert pdf("encrypt", source, tmp_path / "locked.pdf", "--password", "geheim")[0] == 0
    assert PdfReader(tmp_path / "locked.pdf").is_encrypted
    code, lines = pdf("inspect", tmp_path / "locked.pdf")
    assert code == 1 and "--password <password>" in lines[0]
    assert (
        pdf("decrypt", tmp_path / "locked.pdf", tmp_path / "wrong.pdf", "--password", "falsch")[0]
        == 1
    )

    assert (
        pdf("decrypt", tmp_path / "locked.pdf", tmp_path / "open.pdf", "--password", "geheim")[0]
        == 0
    )
    opened = PdfReader(tmp_path / "open.pdf")
    assert not opened.is_encrypted and len(opened.pages) == 2


def test_markdown_document_carries_title_language_and_bookmarks(pdf: Run, tmp_path: Path) -> None:
    Image.new("RGB", (400, 200), "#0f4c5c").save(tmp_path / "plan.png")
    source = tmp_path / "umzug.md"
    source.write_text(
        '---\ntitle: Umzug\nlang: de\ntoc: true\naccent: "#0F4C5C"\n---\n\n'
        "## Termine\n\n| Datum | Schritt |\n|:---|---:|\n| 6. November | Kisten |\n\n"
        "> [!NOTE] Hinweis\n> Bitte packen.\n\n- [x] erledigt\n- [ ] offen\n\n"
        "### Anfahrt\n\n![Lageplan](plan.png)\n",
        encoding="utf-8",
    )

    code, lines = pdf("create", source, tmp_path / "umzug.pdf")

    assert code == 0 and lines[0].startswith("Created umzug.pdf")
    assert "No structural problems found." in lines
    reader = PdfReader(tmp_path / "umzug.pdf")
    assert reader.metadata is not None and reader.metadata.title == "Umzug"
    assert reader.root_object["/Lang"] == "de"
    assert len(reader.pages) == 2  # the table of contents takes the first page
    assert "Seite 1 von 2" in reader.pages[0].extract_text()
    assert reader.outline

    code, lines = pdf("create", source, tmp_path / "umzug.pdf")
    assert code == 0 and "page images rendered before show the old version" in lines[-1]


def test_inspect_and_render_report_what_an_agent_must_look_at(pdf: Run, tmp_path: Path) -> None:
    scan = tmp_path / "scan.pdf"
    pages = [Image.new("RGB", (600, 850), colour) for colour in ("#d0d0d0", "#a0a0a0")]
    pages[0].save(scan, "PDF", save_all=True, append_images=pages[1:], resolution=72)

    code, lines = pdf("inspect", scan)
    assert code == 0 and lines[0].startswith("scan.pdf: 2 pages")
    assert "Problems:" in lines  # no text layer: the pages must be read as images

    code, lines = pdf("render", scan, tmp_path / "pages", "--size", "400", "--grid")
    assert code == 0 and lines[0].startswith("Rendered 2 pages")
    rendered = sorted(path.name for path in (tmp_path / "pages").iterdir())
    assert rendered == ["scan-overview-1.png", "scan-page-001.png", "scan-page-002.png"]
    assert max(Image.open(tmp_path / "pages" / "scan-page-001.png").size) == 400

    pdf("render", scan, tmp_path / "pages", "--pages", "2", "--no-overview")
    assert sorted(path.name for path in (tmp_path / "pages").iterdir()) == ["scan-page-002.png"]

    # 600 x 850 pt page: the area is 50.8 x 25.4 mm, rendered at 1600 px on its long edge.
    code, lines = pdf(
        "render", scan, tmp_path / "part", "--pages", "1", "--area", "10,10,50.8,25.4"
    )
    assert code == 0 and "area from 10,10 mm to 60.8,35.4 mm" in lines[2]
    assert Image.open(tmp_path / "part" / "scan-page-001.png").size == (1600, 800)


def test_compress_reencodes_large_images(pdf: Run, tmp_path: Path) -> None:
    noise = random.Random(7).randbytes(900 * 600 * 3)
    source = tmp_path / "photo.pdf"
    Image.frombytes("RGB", (900, 600), noise).save(source, "PDF", quality=95)

    code, lines = pdf(
        "compress", source, tmp_path / "small.pdf", "--quality", "40", "--max-pixels", "600"
    )

    assert code == 0 and lines[0].startswith("Compressed photo.pdf")
    assert (tmp_path / "small.pdf").stat().st_size < source.stat().st_size / 2
    image = PdfReader(tmp_path / "small.pdf").pages[0].images[0].image
    assert image is not None and max(image.size) == 600


def test_html_document_is_printed_and_checked(pdf: Run, tmp_path: Path) -> None:
    """The only test that starts a browser (about 2 s): it guards the main creation engine."""
    if PDF_TOOLS["env"].find_browser(None).path is None:
        pytest.skip("no Chromium-based browser on this host")
    source = tmp_path / "letter.html"
    source.write_text(
        '<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>Brief</title>'
        "<style>@page { size: A5; margin: 15mm; }</style></head>"
        "<body><h1>Hallo {{Name}}</h1><p>Text \U0010fffd</p></body></html>",
        encoding="utf-8",
    )

    code, lines = pdf("create", source, tmp_path / "letter.pdf")

    assert code == 0 and lines[0].startswith("Created letter.pdf: 1 page, A5 portrait")
    reader = PdfReader(tmp_path / "letter.pdf")
    assert reader.metadata is not None and reader.metadata.title == "Brief"
    problems = lines[lines.index("Problems:") + 1 :]
    assert any("{{Name}}" in line for line in problems)
    assert any(line.startswith("- Page 1: 1 character") for line in problems)
