"""Assistant explicit file-marker recognition tests."""

from pathlib import Path

from core.chat.output_files import (
    AssistantFileReference,
    resolve_assistant_file_references,
)


def test_resolves_absolute_and_relative_markers_anywhere_in_a_line(tmp_path: Path) -> None:
    absolute = tmp_path / "absolute-image.png"
    relative = tmp_path / "relative.txt"
    absolute.write_bytes(b"image")
    relative.write_text("file", encoding="utf-8")
    first_line = f"Image: file:{absolute}"
    second_line = "Download file:relative.txt when ready"
    content = f"{first_line}\n{second_line}"

    assert resolve_assistant_file_references(content, cwd=tmp_path) == [
        _reference(first_line, 0, f"file:{absolute}", absolute),
        _reference(second_line, 1, "file:relative.txt", relative),
    ]


def test_supports_multiple_markers_on_one_line(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    report = tmp_path / "report.pdf"
    image.write_bytes(b"image")
    report.write_bytes(b"%PDF")
    line = f"Files: file:{image} file:{report}"

    assert resolve_assistant_file_references(line, cwd=tmp_path) == [
        _reference(line, 0, f"file:{image}", image),
        _reference(line, 0, f"file:{report}", report),
    ]


def test_keeps_trailing_sentence_punctuation_outside_the_marker(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    line = f"Look at file:{image}, then continue."

    assert resolve_assistant_file_references(line, cwd=tmp_path) == [
        _reference(line, 0, f"file:{image}", image)
    ]


def test_single_backtick_pair_is_part_of_the_explicit_marker(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    marker = f"`file:{image}`"
    line = f"Look at {marker} and continue"

    assert resolve_assistant_file_references(line, cwd=tmp_path) == [
        _reference(line, 0, marker, image)
    ]


def test_ignores_unmarked_paths_and_markers_in_code(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    content = f"{image}\n```text\nfile:{image}\n```\n    file:{image}\nValid: file:{image}"
    valid_line = f"Valid: file:{image}"

    assert resolve_assistant_file_references(content, cwd=tmp_path) == [
        _reference(valid_line, 5, f"file:{image}", image)
    ]


def test_ignores_missing_files_directories_urls_and_embedded_prefix(tmp_path: Path) -> None:
    content = "file:missing.png\nfile:.\nfile:https://example.test/image.png\nprofile:notes.txt"

    assert resolve_assistant_file_references(content, cwd=tmp_path) is None


def test_without_workspace_resolves_only_absolute_markers(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    second_line = f"Absolute file:{file_path}"
    content = f"file:report.txt\n{second_line}"

    assert resolve_assistant_file_references(content, cwd=None) == [
        _reference(second_line, 1, f"file:{file_path}", file_path)
    ]


def test_paths_with_whitespace_are_not_implicitly_guessed(tmp_path: Path) -> None:
    file_path = tmp_path / "report final.pdf"
    file_path.write_bytes(b"%PDF")

    assert resolve_assistant_file_references(f"file:{file_path}", cwd=tmp_path) is None


def test_supports_tilde_fences_without_closing_on_shorter_marker(tmp_path: Path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF")
    content = f"~~~~\nfile:{file_path}\n~~~\nfile:{file_path}\n~~~~\nReady file:{file_path}"
    valid_line = f"Ready file:{file_path}"

    assert resolve_assistant_file_references(content, cwd=tmp_path) == [
        _reference(valid_line, 5, f"file:{file_path}", file_path)
    ]


def test_fence_with_info_text_does_not_close_an_open_fence(tmp_path: Path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF")
    content = f"```\n```text\nfile:{file_path}\n```\nReady file:{file_path}"
    valid_line = f"Ready file:{file_path}"

    assert resolve_assistant_file_references(content, cwd=tmp_path) == [
        _reference(valid_line, 4, f"file:{file_path}", file_path)
    ]


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
