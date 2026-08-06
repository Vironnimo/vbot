"""Assistant standalone file-path recognition tests."""

from pathlib import Path

from core.chat.output_files import (
    AssistantFileReference,
    resolve_assistant_file_references,
)


def test_resolves_absolute_and_relative_standalone_paths(tmp_path: Path) -> None:
    absolute = tmp_path / "absolute image.png"
    relative = tmp_path / "relative.txt"
    absolute.write_bytes(b"image")
    relative.write_text("file", encoding="utf-8")
    content = f"Here they are:\n{absolute}\n`relative.txt`"

    assert resolve_assistant_file_references(content, cwd=tmp_path) == [
        AssistantFileReference(line_index=1, path=str(absolute.resolve())),
        AssistantFileReference(line_index=2, path=str(relative.resolve())),
    ]


def test_ignores_paths_in_prose_and_fenced_code(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    content = f"The file is {image}\n```text\n{image}\n```\n{image}\n    {image}"

    assert resolve_assistant_file_references(content, cwd=tmp_path) == [
        AssistantFileReference(line_index=4, path=str(image.resolve()))
    ]


def test_ignores_missing_files_directories_and_urls(tmp_path: Path) -> None:
    content = "missing.png\n.\nhttps://example.test/image.png"

    assert resolve_assistant_file_references(content, cwd=tmp_path) is None


def test_without_workspace_resolves_only_absolute_paths(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")

    assert resolve_assistant_file_references(
        f"report.txt\n{file_path}",
        cwd=None,
    ) == [AssistantFileReference(line_index=1, path=str(file_path.resolve()))]


def test_supports_tilde_fences_without_closing_on_shorter_marker(tmp_path: Path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF")
    content = f"~~~~\n{file_path}\n~~~\n{file_path}\n~~~~\n{file_path}"

    assert resolve_assistant_file_references(content, cwd=tmp_path) == [
        AssistantFileReference(line_index=5, path=str(file_path.resolve()))
    ]


def test_fence_with_info_text_does_not_close_an_open_fence(tmp_path: Path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF")
    content = f"```\n```text\n{file_path}\n```\n{file_path}"

    assert resolve_assistant_file_references(content, cwd=tmp_path) == [
        AssistantFileReference(line_index=4, path=str(file_path.resolve()))
    ]
