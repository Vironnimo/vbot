"""The Markdown files shipped with stable releases and their public asset format."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TypedDict

DIRECTORY = Path(__file__).resolve().parents[2] / "changelog"
ASSET = "vbot-changelog.json"
RELEASES_URL = "https://github.com/Vironnimo/vbot/releases"
MAX_BYTES = 256 * 1024
MAX_RELEASES = 50
SECTIONS = ("Before you update", "Highlights", "Improvements", "Fixes")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}")


class Release(TypedDict):
    version: str
    date: str
    before_update: str | None
    markdown: str


def version_key(version: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(version):
        raise ValueError(f"Expected a release version X.Y.Z, got {version!r}")
    return tuple(map(int, version.split(".")))


def read_release(directory: Path, version: str, *, validate: bool = False) -> Release:
    version_key(version)
    source = (directory / f"{version}.md").read_text(encoding="utf-8").rstrip()
    heading, _, body = source.partition("\n")
    match = re.fullmatch(rf"# v{re.escape(version)} - ([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})", heading)
    if match is None:
        raise ValueError(f"Expected heading '# v{version} - YYYY-MM-DD'")
    published = date.fromisoformat(match[1])
    body = body.strip()
    preamble, *sections = re.split(r"^## ([^\n]+)$", body, flags=re.MULTILINE)
    if preamble.strip() or not sections:
        raise ValueError(f"Expected changelog sections in {version}.md")
    if validate and "**Full Changelog**:" in body:
        raise ValueError("The publisher appends the compare link")
    before = None
    previous = -1
    for name, content in zip(sections[::2], sections[1::2], strict=True):
        if validate:
            if name not in SECTIONS or SECTIONS.index(name) <= previous or not content.strip():
                raise ValueError(f"Unknown, empty, repeated or out-of-order section: {name}")
            previous = SECTIONS.index(name)
        if name == SECTIONS[0]:
            before = content.strip()
    return Release(version=version, date=published.isoformat(), before_update=before, markdown=body)


def bundled(directory: Path | None = None, *, limit: int | None = None) -> list[Release]:
    directory = DIRECTORY if directory is None else directory
    versions = sorted((p.stem for p in directory.glob("*.md")), key=version_key, reverse=True)
    return [read_release(directory, version) for version in versions[:limit]]


def pre_update_text(releases: list[Release]) -> str:
    return "\n\n".join(
        f"v{r['version']}\n{r['before_update']}" for r in releases if r["before_update"]
    )
