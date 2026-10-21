"""Assistant explicit file-marker recognition tests."""

from pathlib import Path

import pytest

from core.chat.output_files import (
    AssistantFileReference,
    resolve_assistant_file_references,
)
from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules

FULL_ACCESS = PermissionBoundary.UNRESTRICTED


def test_resolves_absolute_relative_and_repeated_markers_anywhere_in_a_line(
    tmp_path: Path,
) -> None:
    absolute = tmp_path / "absolute-image.png"
    relative = tmp_path / "relative.txt"
    report = tmp_path / "report.pdf"
    absolute.write_bytes(b"image")
    relative.write_text("file", encoding="utf-8")
    report.write_bytes(b"%PDF")
    first_line = f"Files: file:{absolute} file:{report}"
    second_line = "Download file:relative.txt when ready"

    assert resolve_assistant_file_references(
        f"{first_line}\n{second_line}", cwd=tmp_path, permissions=FULL_ACCESS
    ) == [
        _reference(first_line, 0, f"file:{absolute}", absolute),
        _reference(first_line, 0, f"file:{report}", report),
        _reference(second_line, 1, "file:relative.txt", relative),
    ]


@pytest.mark.parametrize("wrapper", ["", "*", "***", "__", "`"])
def test_markdown_wrapper_pair_belongs_to_the_marker_but_punctuation_does_not(
    tmp_path: Path,
    wrapper: str,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    marker = f"{wrapper}file:{image}{wrapper}"
    line = f"Look at {marker}, then continue."

    assert resolve_assistant_file_references(line, cwd=tmp_path, permissions=FULL_ACCESS) == [
        _reference(line, 0, marker, image)
    ]


@pytest.mark.parametrize(
    ("template", "valid_line_index"),
    [
        ("{path}\n```text\nfile:{path}\n```\n    file:{path}\nReady file:{path}", 5),
        ("~~~~\nfile:{path}\n~~~\nfile:{path}\n~~~~\nReady file:{path}", 5),
        ("```\n```text\nfile:{path}\n```\nReady file:{path}", 4),
    ],
    ids=["unmarked-fenced-and-indented", "shorter-tilde-fence-stays-open", "info-text-stays-open"],
)
def test_ignores_unmarked_paths_and_markers_in_code(
    tmp_path: Path, template: str, valid_line_index: int
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF")
    valid_line = f"Ready file:{file_path}"

    assert resolve_assistant_file_references(
        template.format(path=file_path), cwd=tmp_path, permissions=FULL_ACCESS
    ) == [_reference(valid_line, valid_line_index, f"file:{file_path}", file_path)]


def test_ignores_missing_files_directories_urls_and_embedded_prefix(tmp_path: Path) -> None:
    content = "file:missing.png\nfile:.\nfile:https://example.test/image.png\nprofile:notes.txt"

    assert resolve_assistant_file_references(content, cwd=tmp_path, permissions=FULL_ACCESS) is None


def test_without_workspace_resolves_only_absolute_markers(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    second_line = f"Absolute file:{file_path}"
    content = f"file:report.txt\n{second_line}"

    assert resolve_assistant_file_references(content, cwd=None, permissions=FULL_ACCESS) == [
        _reference(second_line, 1, f"file:{file_path}", file_path)
    ]


def test_paths_with_whitespace_are_not_implicitly_guessed(tmp_path: Path) -> None:
    file_path = tmp_path / "report final.pdf"
    file_path.write_bytes(b"%PDF")

    assert (
        resolve_assistant_file_references(
            f"file:{file_path}", cwd=tmp_path, permissions=FULL_ACCESS
        )
        is None
    )


@pytest.mark.parametrize(
    ("name", "deny", "kept"),
    [
        pytest.param("notes.txt", "Read({workdir}/notes.txt)", False, id="refused-file"),
        pytest.param("notes.txt", "Read({workdir}/drafts/**)", True, id="permitted-file"),
        # An HTML preview serves its whole folder, so a refused name below keeps it text ...
        pytest.param("index.html", "Read({workdir}/drafts/**)", False, id="html-refused-below"),
        # ... unless the preview never serves that name anyway.
        pytest.param("index.html", "Read(**/.env)", True, id="html-dot-name-refused"),
    ],
)
def test_references_the_rules_refuse_stay_plain_text(
    tmp_path: Path, name: str, deny: str, kept: bool
) -> None:
    site = tmp_path / "site"
    (site / "drafts").mkdir(parents=True)
    file_path = site / name
    file_path.write_text("content", encoding="utf-8")
    permissions = PermissionBoundary.build(
        normalize_permission_rules({"deny": [deny]}),
        BoundaryContext(workdir=site, home=tmp_path / "home", data_dir=tmp_path / "data"),
    )

    references = resolve_assistant_file_references(
        f"See file:{name}", cwd=site, permissions=permissions
    )

    assert references == (
        [_reference(f"See file:{name}", 0, f"file:{name}", file_path)] if kept else None
    )


def _reference(
    line: str,
    line_index: int,
    marker: str,
    path: Path,
) -> AssistantFileReference:
    start_index = line.index(marker)
    return AssistantFileReference(
        line_index=line_index,
        path=str(path.resolve()),
        start_index=start_index,
        end_index=start_index + len(marker),
    )
