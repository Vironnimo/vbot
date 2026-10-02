"""TrueType fonts for ReportLab: one text family plus fallbacks for missing characters."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Regular, bold, italic, bold italic and monospace files of each family, in order of preference.
FAMILIES = [
    ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf", "consola.ttf"),
    (
        "NotoSans-Regular.ttf",
        "NotoSans-Bold.ttf",
        "NotoSans-Italic.ttf",
        "NotoSans-BoldItalic.ttf",
        "NotoSansMono-Regular.ttf",
    ),
    (
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
        "DejaVuSans-Oblique.ttf",
        "DejaVuSans-BoldOblique.ttf",
        "DejaVuSansMono.ttf",
    ),
    (
        "LiberationSans-Regular.ttf",
        "LiberationSans-Bold.ttf",
        "LiberationSans-Italic.ttf",
        "LiberationSans-BoldItalic.ttf",
        "LiberationMono-Regular.ttf",
    ),
    ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf", "cour.ttf"),
    ("Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf", "Arial Bold Italic.ttf", "Courier New.ttf"),
]
# Fonts with TrueType outlines that cover scripts the text family lacks. ReportLab cannot use
# fonts with CFF outlines (most .otf files, Noto CJK) or bitmap emoji fonts.
FALLBACKS = [
    "seguisym.ttf",
    "seguiemj.ttf",
    "msyh.ttc",
    "msgothic.ttc",
    "malgun.ttf",
    "Nirmala.ttf",
    "NotoSansSymbols-Regular.ttf",
    "NotoSansSymbols2-Regular.ttf",
    "DejaVuSans.ttf",
    "DroidSansFallbackFull.ttf",
    "wqy-microhei.ttc",
    "wqy-zenhei.ttc",
    "NotoSansArabic-Regular.ttf",
    "NotoSansHebrew-Regular.ttf",
    "NotoSansDevanagari-Regular.ttf",
    "NotoSansThai-Regular.ttf",
    "Arial Unicode.ttf",
]


def font_directories() -> list[Path]:
    if os.name == "nt":
        windows = Path(os.environ.get("WINDIR", "C:/Windows"))
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
        return [windows / "Fonts", local]
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
        ]
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local/share/fonts",
        Path.home() / ".fonts",
    ]


_INDEX: dict[str, Path] | None = None


def _font_index() -> dict[str, Path]:
    global _INDEX
    if _INDEX is None:
        _INDEX = {}
        for directory in font_directories():
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                if path.suffix.lower() in (".ttf", ".ttc") and path.name.lower() not in _INDEX:
                    _INDEX[path.name.lower()] = path
    return _INDEX


def find_font(name: str) -> Path | None:
    return _font_index().get(name.lower())


class Family:
    """Registered ReportLab font names plus per-character fallback fonts."""

    def __init__(self) -> None:
        self.regular = "Helvetica"
        self.bold = "Helvetica-Bold"
        self.italic = "Helvetica-Oblique"
        self.bold_italic = "Helvetica-BoldOblique"
        self.mono = "Courier"
        self.source = "built-in Helvetica (Latin-1 only)"
        self._coverage: set[int] = set(range(0x20, 0x7F)) | set(range(0xA0, 0x100))
        self._fallbacks: list[tuple] = []
        self._fallbacks_loaded = False
        self.missing: set[str] = set()

    def covers(self, character: str) -> bool:
        return ord(character) in self._coverage

    def fallback_for(self, character: str) -> str | None:
        code = ord(character)
        if not self._fallbacks_loaded:
            self._fallbacks_loaded = True
            _load_fallbacks(self)
        for name, coverage in self._fallbacks:
            if code in coverage:
                return str(name)
        return None

    def report_missing(self, text: str) -> None:
        for character in text:
            if character.isspace() or self.covers(character) or self.fallback_for(character):
                continue
            if 0x200B <= ord(character) <= 0x200F or ord(character) in (0xFE0F, 0x2060):
                continue
            self.missing.add(character)


_FAMILY: Family | None = None


def register_family(regular: str | None = None, bold: str | None = None) -> Family:
    """Register the best available family once; explicit files win over discovery."""
    global _FAMILY
    if _FAMILY is not None:
        return _FAMILY
    from reportlab.lib.fonts import addMapping  # type: ignore[import-untyped]
    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]

    family = Family()
    candidates: list[tuple] = []
    if regular:
        candidates.append((regular, bold or regular, regular, bold or regular, None))
    candidates.extend(FAMILIES)
    for files in candidates:
        paths = [
            Path(item) if item and Path(item).is_file() else (find_font(item) if item else None)
            for item in files
        ]
        if paths[0] is None or paths[1] is None:
            continue
        try:
            fonts = {}
            for role, path in zip(("Regular", "Bold", "Italic", "BoldItalic"), paths[:4]):
                font = TTFont(f"Doc{role}", str(path or paths[0]))
                pdfmetrics.registerFont(font)
                fonts[role] = font
        except Exception:
            continue
        family.regular, family.bold = "DocRegular", "DocBold"
        family.italic, family.bold_italic = "DocItalic", "DocBoldItalic"
        addMapping("DocRegular", 0, 0, "DocRegular")
        addMapping("DocRegular", 1, 0, "DocBold")
        addMapping("DocRegular", 0, 1, "DocItalic")
        addMapping("DocRegular", 1, 1, "DocBoldItalic")
        family._coverage = set(fonts["Regular"].face.charToGlyph)
        family.source = paths[0].name
        if paths[4] is not None:
            try:
                pdfmetrics.registerFont(TTFont("DocMono", str(paths[4])))
                family.mono = "DocMono"
            except Exception:
                pass
        break
    _FAMILY = family
    return family


def _load_fallbacks(family: Family) -> None:
    """Load fallback fonts on first need; large CJK collections take a moment."""
    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]

    for number, name in enumerate(FALLBACKS):
        path = find_font(name)
        if path is None or path.name == family.source:
            continue
        try:
            font = TTFont(f"DocFallback{number}", str(path), subfontIndex=0)
            pdfmetrics.registerFont(font)
        except Exception:
            continue
        family._fallbacks.append((f"DocFallback{number}", set(font.face.charToGlyph)))
