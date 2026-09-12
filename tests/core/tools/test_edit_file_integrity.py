"""Edit: file integrity behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tools.edit import (
    edit_handler,
)
from core.tools.tools import is_tool_result_envelope
from core.utils.paths import model_path
from tests.core.tools.edit_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
)


def test_edit_warns_when_edit_breaks_syntax_without_blocking(tmp_path: Path) -> None:
    # The edit is still applied (warn, don't block); the result carries a
    # non-fatal syntax warning attributing the break to this edit.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "module.py", "old_string": "value = 1", "new_string": "value = (1"},
    )

    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "value = (1\n"
    data = result["data"]
    assert isinstance(data, dict)
    assert data["syntax_warning"].startswith("Syntax check failed after this edit:")


def test_edit_does_not_blame_preexisting_syntax_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("def f(:\n    return 1\n", encoding="utf-8")  # already broken

    result = edit_handler(
        make_context(workspace),
        {"path": "module.py", "old_string": "return 1", "new_string": "return 2"},
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert "already syntactically invalid before this edit" in data["syntax_warning"]


def test_edit_no_syntax_warning_when_result_is_valid(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "module.py", "old_string": "value = 1", "new_string": "value = 2"},
    )

    data = assert_success_envelope(result)
    assert "syntax_warning" not in data
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_edit_returns_ambiguous_match_without_replace_all(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("same\nother\nsame\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "same", "new_string": "changed"},
    )

    error = assert_failure_envelope(result, "ambiguous_match")
    assert "Found 2 occurrences on lines 1, 3" in error["message"]
    assert "Raw candidate contexts" in error["message"]
    assert "Candidate 1 (around line 1):\nsame\nother" in error["message"]
    assert "Candidate 2 (around line 3):\nother\nsame" in error["message"]
    assert target.read_text(encoding="utf-8") == "same\nother\nsame\n"


def test_edit_bounds_ambiguous_candidate_contexts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    long_line = "x" * 200
    target.write_text(
        f"same\n{long_line}\nsame\nmiddle\nsame\nother\nsame\n",
        encoding="utf-8",
    )

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "same", "new_string": "changed"},
    )

    error = assert_failure_envelope(result, "ambiguous_match")
    assert "Showing the first 3 of 4 candidates" in error["message"]
    assert "Candidate 3" in error["message"]
    assert "Candidate 4" not in error["message"]
    assert "x" * 160 not in error["message"]
    assert "x" * 157 + "..." in error["message"]
    assert target.read_text(encoding="utf-8").count("same") == 4


def test_edit_disambiguates_multiple_matches_within_one_long_line(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text(
        "a" * 100 + "same" + "b" * 100 + "same" + "c" * 100 + "same\n",
        encoding="utf-8",
    )

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "same", "new_string": "changed"},
    )

    error = assert_failure_envelope(result, "ambiguous_match")
    assert "Found 3 occurrences on line 1." in error["message"]
    assert "on lines 1, 1, 1" not in error["message"]
    assert "Candidate 1 (line 1, character 101)" in error["message"]
    assert "Candidate 2 (line 1, character 205)" in error["message"]
    assert "Candidate 3 (line 1, character 309)" in error["message"]
    candidate_contexts = [
        block.split("\n", 1)[1] for block in error["message"].split("Candidate ")[1:]
    ]
    assert len(set(candidate_contexts)) == 3


def test_edit_replaces_ambiguous_matches_with_replace_all(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("same\nother\nsame\n", encoding="utf-8")

    context = make_context(workspace)
    result = edit_handler(
        context,
        {"path": "notes.txt", "old_string": "same", "new_string": "changed", "replace_all": True},
    )

    data = assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "changed\nother\nchanged\n"
    assert data["replacements"] == 2
    assert data["first_changed_line"] == 1
    assert data["last_changed_line"] == 3
    assert context.presentation_facts == [
        {"kind": "line_change", "change": "added", "value": 2},
        {"kind": "line_change", "change": "removed", "value": 2},
    ]


def test_edit_counts_multiline_replacement_facts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("before\nblock\nafter\n", encoding="utf-8")
    context = make_context(workspace)

    result = edit_handler(
        context,
        {
            "path": "notes.txt",
            "old_string": "before\nblock",
            "new_string": "replacement\nwith\nthree lines",
        },
    )

    assert_success_envelope(result)
    assert context.presentation_facts == [
        {"kind": "line_change", "change": "added", "value": 3},
        {"kind": "line_change", "change": "removed", "value": 2},
    ]


def test_edit_preserves_lf_file_line_endings_at_byte_level(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"alpha\nbeta\ngamma\n")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha\nbeta", "new_string": "one\ntwo"},
    )

    data = assert_success_envelope(result)
    assert target.read_bytes() == b"one\ntwo\ngamma\n"
    assert data["replacements"] == 1


def test_edit_normalizes_newlines_for_matching_and_replacement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha\nbeta", "new_string": "one\ntwo"},
    )

    data = assert_success_envelope(result)
    assert target.read_bytes() == b"one\r\ntwo\r\ngamma\r\n"
    assert data["replacements"] == 1


def test_edit_preserves_crlf_for_exact_match_replacement_at_byte_level(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "beta", "new_string": "one\ntwo"},
    )

    data = assert_success_envelope(result)
    assert target.read_bytes() == b"alpha\r\none\r\ntwo\r\ngamma\r\n"
    assert data["replacements"] == 1


def test_edit_matches_smart_quotes_fuzzily(tmp_path: Path) -> None:
    # The file has straight quotes; the model sent curly ones. Fuzzy matching
    # should still find and replace the target.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "config.py"
    target.write_text('name = "value"\n', encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "config.py", "old_string": "name = “value”", "new_string": 'name = "other"'},
    )

    data = assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == 'name = "other"\n'
    assert data["replacements"] == 1


def test_edit_matches_different_indentation_fuzzily(tmp_path: Path) -> None:
    # The file uses 4-space indentation; the model sent 2-space. The match should
    # succeed and the replacement be re-indented to the file's actual style.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "module.py", "old_string": "  return 1", "new_string": "  return 42"},
    )

    data = assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "def f():\n    return 42\n"
    assert data["first_changed_line"] == 2


def test_edit_matches_internal_horizontal_whitespace_fuzzily(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("def f():\n    value  =\t1\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "module.py", "old_string": "  value = 1", "new_string": "  value = 2"},
    )

    data = assert_success_envelope(result)
    assert data["first_changed_line"] == 2
    assert target.read_text(encoding="utf-8") == "def f():\n    value = 2\n"


def test_edit_preserves_utf8_bom(tmp_path: Path) -> None:
    # Editing a BOM-prefixed file must keep the BOM intact on the round-trip.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_bytes(b"\xef\xbb\xbfvalue = 1\n")

    result = edit_handler(
        make_context(workspace),
        {"path": "module.py", "old_string": "value = 1", "new_string": "value = 2"},
    )

    assert_success_envelope(result)
    assert target.read_bytes() == b"\xef\xbb\xbfvalue = 2\n"


def test_edit_rejects_binary_file_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "data.bin"
    original = b"value = old\x00\xffpayload"
    target.write_bytes(original)

    def fail_if_called(_resolved: Path, _payload: bytes) -> None:
        pytest.fail("binary file must be rejected before writing")

    monkeypatch.setattr("core.tools.edit.atomic_write_bytes", fail_if_called)

    result = edit_handler(
        make_context(workspace),
        {"path": "data.bin", "old_string": "value = old", "new_string": "value = new"},
    )

    error = assert_failure_envelope(result, "binary_file")
    assert model_path(target.resolve()) in error["message"]
    assert target.read_bytes() == original


def test_edit_rejects_non_utf8_file_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "legacy.txt"
    original = b"value = old\ncaf\xe9\n"
    target.write_bytes(original)

    def fail_if_called(_resolved: Path, _payload: bytes) -> None:
        pytest.fail("non-UTF-8 file must be rejected before writing")

    monkeypatch.setattr("core.tools.edit.atomic_write_bytes", fail_if_called)

    result = edit_handler(
        make_context(workspace),
        {"path": "legacy.txt", "old_string": "value = old", "new_string": "value = new"},
    )

    error = assert_failure_envelope(result, "unsupported_encoding")
    assert model_path(target.resolve()) in error["message"]
    assert target.read_bytes() == original


def test_edit_returns_failure_envelope_for_filesystem_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"hello\n")

    def raise_permission_error(self: Path) -> bytes:
        raise PermissionError("access denied while reading")

    monkeypatch.setattr(Path, "read_bytes", raise_permission_error)

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "hello", "new_string": "hi"},
    )

    error = assert_failure_envelope(result, "file_read_error")
    assert model_path(target.resolve()) in error["message"]
    assert "access denied while reading" in error["message"]


def test_edit_returns_failure_envelope_for_filesystem_write_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"hello\n")

    def raise_permission_error(_source: Path, _target: Path) -> None:
        raise PermissionError("access denied while writing")

    monkeypatch.setattr("core.tools.file_state.os.replace", raise_permission_error)

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "hello", "new_string": "hi"},
    )

    error = assert_failure_envelope(result, "file_write_error")
    assert model_path(target.resolve()) in error["message"]
    assert "access denied while writing" in error["message"]


def test_edit_returns_failure_for_unknown_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "old_string": "hello",
            "new_string": "hi",
            "filePath": "notes.txt",
        },
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "filePath" in error["message"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"old_string": "old", "new_string": "new"},
        {"path": 123, "old_string": "old", "new_string": "new"},
        {"path": "notes.txt", "new_string": "new"},
        {"path": "notes.txt", "old_string": 123, "new_string": "new"},
        {"path": "notes.txt", "old_string": "old"},
        {"path": "notes.txt", "old_string": "old", "new_string": 123},
        {"path": "notes.txt", "old_string": "old", "new_string": "new", "replace_all": "maybe"},
    ],
)
def test_edit_returns_failure_for_invalid_argument_types(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("old\n", encoding="utf-8")

    result = edit_handler(make_context(workspace), arguments)

    assert_failure_envelope(result, "invalid_arguments")


def test_edit_success_and_failure_results_are_valid_envelopes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("old\n", encoding="utf-8")

    success = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "old", "new_string": "new"},
    )
    failure = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "missing", "new_string": "replacement"},
    )

    assert is_tool_result_envelope(success) is True
    assert is_tool_result_envelope(failure) is True


def test_edit_rejects_string_encoded_replace_all(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("x x x", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "x", "new_string": "y", "replace_all": "true"},
    )

    assert_failure_envelope(result, "invalid_arguments")
    assert target.read_text(encoding="utf-8") == "x x x"
