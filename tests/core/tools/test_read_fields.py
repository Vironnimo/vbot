"""read calls in other harnesses' shapes, line windows, and search fields."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tools import FileReadState, ToolRegistry, register_read_tool
from core.tools.contracts import ToolContractError
from core.tools.file_state import StaleReason
from tests.core.tools.test_read import _FakeAttachmentStore, _FakeSpeech, make_context

APP = "".join(f"line {number}\n" for number in range(1, 13))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.txt").write_bytes(APP.encode())
    (tmp_path / "src/code.py").write_bytes(
        b"import os\n\n\nclass Greeter:\n    def greet(self):\n        return 'hi'\n\n\n"
        b"def main():\n    Greeter().greet()\n"
    )
    return tmp_path


async def dispatch(root: Path, arguments: dict, *, file_state: FileReadState | None = None):
    registry = ToolRegistry()
    register_read_tool(
        registry,
        attachment_store=_FakeAttachmentStore(),
        speech_service=_FakeSpeech(),
        file_state=file_state or FileReadState(),
        speech_max_size_bytes=20_971_520,
    )
    return await registry.dispatch(make_context(root), arguments)


def content(result: dict) -> str:
    assert result["ok"] is True, result
    return str(result["data"]["content"])


def error(result: dict, code: str = "invalid_arguments") -> str:
    assert result["ok"] is False, result
    assert result["error"]["code"] == code
    return str(result["error"]["message"])


async def rejected(root: Path, arguments: dict, **kwargs) -> str:
    """Return the invalid_arguments message for a call that never reaches the handler."""
    with pytest.raises(ValueError) as caught:
        await dispatch(root, arguments, **kwargs)
    return str(caught.value)


def window(first: int, last: int, total: int = 12) -> str:
    lines = "".join(f"{number}| line {number}\n" for number in range(first, last + 1))
    if last < total:
        hint = f"[Showing lines {first}-{last} of {total}. Use offset={last + 1} to continue.]"
        return lines + hint
    return lines


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path_field", ["path", "file_path", "filePath", "filename", "file", "target_file", "FilePath"]
)
async def test_path_spellings_from_other_harnesses_read_the_file(
    project: Path, path_field: str
) -> None:
    result = await dispatch(project, {path_field: "src/app.txt", "limit": 2})

    assert content(result) == window(1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": 4, "limit": 3},
        {"start_line": 4, "end_line": 6},
        {"line_start": 4, "line_end": 6},
        {"startLine": 4, "endLine": 6},
        {"start_line_one_indexed": 4, "end_line_one_indexed_inclusive": 6},
        {
            "start_line_one_indexed": 4,
            "end_line_one_indexed_inclusive": 6,
            "should_read_entire_file": False,
        },
        {"lines": "4-6"},
        {"lines": "4:6"},
        {"range": "4..6"},
        {"lines": "L4-L6"},
        {"line_range": "4 to 6"},
        {"view_range": [4, 6]},
        {"view_range": ["4", "6"]},
        {"lines": {"start": 4, "end": 6}},
        {"offset": "4-6"},
        {"offset": "4", "limit": "3"},
        {"from": 4, "to": 6},
        {"end_line": 6, "offset": 4},
        {"start_line": 4, "num_lines": 3},
    ],
)
async def test_line_window_spellings_read_the_same_lines(project: Path, arguments: dict) -> None:
    result = await dispatch(project, {"path": "src/app.txt", **arguments})

    assert content(result) == window(4, 6)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{"view_range": [10, -1]}, {"lines": "10-"}, {"lines": "10-end"}, {"start_line": 10}],
)
async def test_open_ended_ranges_read_to_the_end(project: Path, arguments: dict) -> None:
    result = await dispatch(project, {"path": "src/app.txt", **arguments})

    assert content(result) == window(10, 12)


@pytest.mark.asyncio
async def test_end_line_alone_reads_from_the_first_line(project: Path) -> None:
    result = await dispatch(project, {"path": "src/app.txt", "end_line": 2})

    assert content(result) == window(1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": 0},
        {"offset": 1},
        {"offset": ""},
        {"offset": None, "limit": None},
        {"limit": 0},
        {"limit": -1},
        {"should_read_entire_file": True},
        {"should_read_entire_file": True, "start_line_one_indexed": 4, "end_line": 6},
        {"pages": ""},
    ],
)
async def test_start_and_no_limit_placeholders_read_from_the_top(
    project: Path, arguments: dict
) -> None:
    result = await dispatch(project, {"path": "src/app.txt", **arguments})

    assert content(result) == window(1, 12)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "first"),
    [
        ({"offset": -3}, 10),
        ({"offset": "-3"}, 10),
        ({"tail": 3}, 10),
        ({"last_lines": 3}, 10),
        ({"offset": -100}, 1),
    ],
)
async def test_counting_back_from_the_end_reads_the_last_lines(
    project: Path, arguments: dict, first: int
) -> None:
    result = await dispatch(project, {"path": "src/app.txt", **arguments})

    assert content(result) == window(first, 12)


@pytest.mark.asyncio
async def test_last_lines_of_a_crlf_file_without_a_final_break(project: Path) -> None:
    (project / "notes.txt").write_bytes(b"one\r\ntwo\r\nthree")

    result = await dispatch(project, {"path": "notes.txt", "offset": -2, "limit": 1})

    assert content(result) == "2| two\n[Showing lines 2-2 of 3. Use offset=3 to continue.]"


@pytest.mark.asyncio
async def test_continuation_position_from_a_cut_off_read_resumes_mid_line(
    project: Path,
) -> None:
    result = await dispatch(project, {"path": "src/app.txt", "offset": "2:4", "limit": 2})

    assert content(result).startswith("2:4| e 2\n3| line 3\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"offset": 3, "start_line": 5},
            "Conflicting values for offset: offset is 3 and start_line is 5. Send only the "
            "intended one.",
        ),
        (
            {"lines": "4-6", "offset": 2},
            "The requested line windows disagree: offset=2 or offset=4, limit=3. Send one of them.",
        ),
        (
            {"offset": 10, "limit": 5, "end_line": 20},
            "The requested line windows disagree: limit=5 (lines 10-14) or "
            "offset=10, limit=11 (lines 10-20). Send one of them.",
        ),
        (
            {"start_line": 9, "end_line": 4},
            "The line range 9-4 ends before it starts. To read lines 4-9, send offset=4, limit=6.",
        ),
        (
            {"lines": 50},
            "lines=50 could mean the first 50 lines or line 50 alone. Send limit=50 for "
            "the first 50 lines, or offset=50, limit=1 for that line.",
        ),
        (
            {"tail": 3, "offset": 2},
            "The requested line windows disagree: offset=2 or offset=-3 (the last 3 "
            "lines). Send one of them.",
        ),
        (
            {"offset": -5, "end_line": 9},
            "offset=-5 counts from the end, so end_line=9 cannot close it.",
        ),
        ({"lines": "four"}, 'lines="four" is not a line range.'),
    ],
)
async def test_contradictory_or_unclear_windows_fail_with_the_call_to_send(
    project: Path, arguments: dict, message: str
) -> None:
    assert message in await rejected(project, {"path": "src/app.txt", **arguments})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        # Cursor sends an explanation with every call.
        {
            "target_file": "src/app.txt",
            "start_line_one_indexed": 4,
            "end_line_one_indexed_inclusive": 6,
            "should_read_entire_file": False,
            "explanation": "Check the setup.",
        },
        {"command": "view", "path": "src/app.txt", "view_range": [4, 6]},
        {"AbsolutePath": "src/app.txt", "StartLine": 4, "EndLine": 6},
        {
            "AbsolutePath": "src/app.txt",
            "StartLine": 4,
            "EndLine": 6,
            "IncludeSummaryOfOtherLines": False,
        },
        {"paths": ["src/app.txt"], "offset": 4, "limit": 3},
        {"path": ["src/app.txt"], "lines": "4-6"},
        {"files": [{"path": "src/app.txt", "line_ranges": ["4-6"]}]},
        {"files": [{"path": "src/app.txt", "line_ranges": [[4, 6]]}]},
        {"path": "src/app.txt", "files": [{"path": "src/app.txt"}], "offset": 4, "limit": 3},
    ],
)
async def test_notes_and_one_file_lists_read_that_file(project: Path, arguments: dict) -> None:
    result = await dispatch(project, arguments)

    assert content(result) == window(4, 6)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"paths": ["src/app.txt", "src/code.py"]},
            'read shows one file per call. Send one call per file: read(path="src/app.txt"), '
            'read(path="src/code.py").',
        ),
        (
            {
                "files": [
                    {"path": "src/app.txt", "line_ranges": ["4-6"]},
                    {"path": "src/code.py"},
                ]
            },
            'Send one call per file: read(path="src/app.txt", offset=4, limit=3), '
            'read(path="src/code.py").',
        ),
        (
            {"path": "src/app.txt", "line_ranges": ["1-2", "9-12"]},
            'lines=["1-2", "9-12"] names 2 line ranges, and read shows one range per call. '
            "Send one call per range: offset=1, limit=2; offset=9, limit=4.",
        ),
        (
            {"path": "src/code.py", "paths": ["src/app.txt"]},
            "Conflicting values for path; provide one intended value.",
        ),
        (
            {"command": "str_replace", "path": "src/app.txt", "old_str": "a", "new_str": "b"},
            'read has no command "str_replace": it shows the file or lists the directory '
            "given as path. To change a file, call apply_patch.",
        ),
    ],
)
async def test_several_files_or_other_commands_fail_with_the_calls_to_send(
    project: Path, arguments: dict, message: str
) -> None:
    assert message in await rejected(project, arguments)


@pytest.mark.asyncio
async def test_a_requested_summary_of_other_lines_is_not_dropped(project: Path) -> None:
    message = await rejected(
        project, {"path": "src/app.txt", "limit": 3, "IncludeSummaryOfOtherLines": True}
    )

    assert '"IncludeSummaryOfOtherLines" is not a parameter.' in message


@pytest.mark.asyncio
async def test_two_different_paths_are_a_conflict_not_a_choice(project: Path) -> None:
    with pytest.raises(ToolContractError, match="Conflicting values for path"):
        await dispatch(project, {"path": "src/app.txt", "file_path": "src/code.py"})


@pytest.mark.asyncio
async def test_pattern_on_a_file_shows_the_matching_lines(project: Path) -> None:
    file_state = FileReadState()

    result = await dispatch(
        project, {"file_path": "src/code.py", "pattern": "greet"}, file_state=file_state
    )

    assert content(result) == (
        '2 lines of src/code.py match "greet":\n5|     def greet(self):\n'
        "--\n10|     Greeter().greet()\n"
    )
    stale = file_state.check_stale("session-1", (project / "src/code.py").resolve())
    assert stale is None


@pytest.mark.asyncio
async def test_pattern_with_case_and_context_fields(project: Path) -> None:
    result = await dispatch(
        project,
        {"path": "src/code.py", "query": "GREETER", "-i": True, "context_lines": 1},
    )

    assert content(result) == (
        '2 lines of src/code.py match "GREETER":\n3| \n4| class Greeter:\n'
        "5|     def greet(self):\n--\n9| def main():\n10|     Greeter().greet()\n"
    )


@pytest.mark.asyncio
async def test_pattern_that_is_not_a_regex_matches_as_text(project: Path) -> None:
    matched = await dispatch(project, {"path": "src/code.py", "pattern": "greet("})
    missing = await dispatch(project, {"path": "src/code.py", "pattern": "absent"})

    assert content(matched) == (
        '2 lines of src/code.py match "greet(" (as plain text: it is not a valid regex):\n'
        "5|     def greet(self):\n--\n10|     Greeter().greet()\n"
    )
    assert content(missing) == 'No line of src/code.py matches "absent".'


@pytest.mark.asyncio
async def test_pattern_on_a_directory_names_the_search_call(project: Path) -> None:
    result = await dispatch(project, {"path": "src", "pattern": "greet", "ignore_case": True})

    assert error(result) == (
        "src is a directory, and read shows the lines of one file. To find matching lines "
        'in its files, call search_files(pattern="greet", path="src", args=["-i"]).'
    )


@pytest.mark.asyncio
async def test_search_only_fields_name_the_search_call(project: Path) -> None:
    message = await rejected(
        project,
        {"path": "src", "pattern": "greet", "glob": "*.py", "output_mode": "content"},
    )

    assert message == (
        "read shows one file or lists one directory and has no glob, output_mode field. "
        'To search files, call search_files(pattern="greet", path="src", glob="*.py", '
        'output_mode="content").'
    )


@pytest.mark.asyncio
async def test_failed_translation_never_marks_the_file_as_read(project: Path) -> None:
    file_state = FileReadState()

    await rejected(project, {"path": "src/app.txt", "lines": 5}, file_state=file_state)
    stale = file_state.check_stale("session-1", (project / "src/app.txt").resolve())
    assert stale is StaleReason.NEVER_READ
