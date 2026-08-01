"""Render every page of a PDF to PNG files for visual inspection."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_DPI = 170
MIN_DPI = 96
MAX_DPI = 300


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Input PDF")
    parser.add_argument("output_dir", type=Path, help="Directory for rendered PNG pages")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="Rendering resolution")
    return parser.parse_args()


def _remove_previous_pages(output_dir: Path, stem: str) -> None:
    for path in output_dir.glob(f"{stem}-page-*.png"):
        if path.is_file():
            path.unlink()


def _render_with_poppler(pdf: Path, output_dir: Path, stem: str, dpi: int) -> list[Path] | None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return None
    prefix = output_dir / f"{stem}-page"
    completed = subprocess.run(
        [executable, "-png", "-r", str(dpi), str(pdf), str(prefix)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"pdftoppm failed with exit code {completed.returncode}: {detail}")
    return sorted(output_dir.glob(f"{stem}-page-*.png"))


def _render_with_pdfium(pdf: Path, output_dir: Path, stem: str, dpi: int) -> list[Path] | None:
    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]
    except ImportError:
        return None
    document = pdfium.PdfDocument(str(pdf))
    rendered: list[Path] = []
    scale = dpi / 72.0
    try:
        for index in range(len(document)):
            page = document[index]
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil()
                output = output_dir / f"{stem}-page-{index + 1:03d}.png"
                image.save(output, format="PNG")
                rendered.append(output)
            finally:
                bitmap.close()
                page.close()
    finally:
        document.close()
    return rendered


def main() -> int:
    arguments = _arguments()
    pdf = arguments.pdf.expanduser().resolve()
    output_dir = arguments.output_dir.expanduser().resolve()
    if not pdf.is_file():
        print(f"PDF not found: {pdf}", file=sys.stderr)
        return 2
    if not MIN_DPI <= arguments.dpi <= MAX_DPI:
        print(f"--dpi must be between {MIN_DPI} and {MAX_DPI}", file=sys.stderr)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf.stem
    try:
        _remove_previous_pages(output_dir, stem)
        poppler_error: str | None = None
        try:
            rendered = _render_with_poppler(pdf, output_dir, stem, arguments.dpi)
        except RuntimeError as error:
            rendered = None
            poppler_error = str(error)
        renderer = "pdftoppm"
        if rendered is None:
            rendered = _render_with_pdfium(pdf, output_dir, stem, arguments.dpi)
            renderer = "pypdfium2"
        if rendered is None:
            attempted = f" The pdftoppm attempt failed: {poppler_error}" if poppler_error else ""
            raise RuntimeError(
                "No working renderer is available. Ask the user before installing Poppler/pdftoppm "
                f"or the pypdfium2 Python package.{attempted}"
            )
        if not rendered:
            raise RuntimeError("The renderer produced no PNG pages")
    except (OSError, RuntimeError) as error:
        print(f"PDF rendering failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "renderer": renderer,
                "dpi": arguments.dpi,
                "page_count": len(rendered),
                "pages": [str(path) for path in rendered],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
