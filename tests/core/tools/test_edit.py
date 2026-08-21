"""Tests for the built-in edit tool."""

import threading
from pathlib import Path

import pytest

from core.tools.change_tracker import ChangeTracker
from core.tools.edit import (
    EDIT_TOOL_DESCRIPTION,
    EDIT_TOOL_NAME,
    EDIT_TOOL_PARAMETERS,
    edit_handler,
    register_edit_tool,
)
from core.tools.file_state import FileReadState
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.utils.paths import model_path


def make_context(
    workspace: Path,
    tool_name: str = EDIT_TOOL_NAME,
    *,
    cwd: Path | None = None,
    session_id: str = "session-1",
    change_tracker: ChangeTracker | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id=session_id,
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
        cwd=cwd,
        change_tracker=change_tracker,
    )


def assert_success_envelope(result: dict[str, object]) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert set(data) == {"message", "path", "first_changed_line", "replacements"}
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]


def test_edit_resolves_relative_path_against_cwd_not_workspace(tmp_path: Path) -> None:
    # The edit must target the repo (cwd) copy; the workspace copy stays untouched.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_file = workspace / "notes.txt"
    workspace_file.write_text("keep me", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    repo_file = repo / "notes.txt"
    repo_file.write_text("old value", encoding="utf-8")

    result = edit_handler(
        make_context(workspace, cwd=repo),
        {"path": "notes.txt", "old_string": "old value", "new_string": "new value"},
    )

    data = assert_success_envelope(result)
    assert repo_file.read_text(encoding="utf-8") == "new value"
    assert workspace_file.read_text(encoding="utf-8") == "keep me"
    assert data["path"] == model_path(repo_file.resolve())


def test_register_edit_tool_exposes_provider_schema() -> None:
    registry = ToolRegistry()

    register_edit_tool(registry, file_state=FileReadState())

    tool = registry.get("edit")
    assert tool.name == EDIT_TOOL_NAME == "edit"
    assert tool.description == EDIT_TOOL_DESCRIPTION
    assert tool.description
    assert tool.parameters == EDIT_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["edit"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "edit"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["path", "old_string", "new_string"]
    assert "additionalProperties" not in parameters
    assert set(parameters["properties"]) == {"path", "old_string", "new_string", "replace_all"}
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in parameters["properties"].values()
    )
    assert "default" not in parameters["properties"]["replace_all"]
    assert "filePath" not in parameters["properties"]
    assert tool.open_input_schema is True


def test_edit_replaces_text_in_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"hello workspace\n")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "workspace", "new_string": "agent"},
    )

    data = assert_success_envelope(result)
    assert target.read_bytes() == b"hello agent\n"
    assert data["replacements"] == 1
    assert data["first_changed_line"] == 1
    assert data["path"] == model_path(target.resolve())
    assert isinstance(data["message"], str)
    assert "OK: updated" in data["message"]


def test_edit_replaces_text_in_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"absolute path\n")

    result = edit_handler(
        make_context(workspace),
        {"path": str(target), "old_string": "absolute", "new_string": "direct"},
    )

    data = assert_success_envelope(result)
    assert target.read_bytes() == b"direct path\n"
    assert data["path"] == model_path(target.resolve())


def test_edit_returns_failure_envelope_for_missing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = edit_handler(
        make_context(workspace),
        {"path": "missing.txt", "old_string": "old", "new_string": "new"},
    )

    error = assert_failure_envelope(result, "file_not_found")
    assert "missing.txt" in error["message"]


def test_edit_returns_failure_envelope_for_directory_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("folder").mkdir()

    result = edit_handler(
        make_context(workspace),
        {"path": "folder", "old_string": "old", "new_string": "new"},
    )

    error = assert_failure_envelope(result, "not_a_file")
    assert "folder" in error["message"]


def test_edit_returns_failure_for_empty_old_string(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "", "new_string": "new"},
    )

    assert_failure_envelope(result, "invalid_arguments")


def test_edit_returns_failure_for_identical_strings(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "hello", "new_string": "hello"},
    )

    assert_failure_envelope(result, "invalid_arguments")


def test_edit_returns_failure_for_not_found_text(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "missing", "new_string": "new"},
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "old_string not found" in error["message"]


def test_edit_no_match_returns_bounded_raw_candidates_without_writing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    original = (
        "def unrelated():\n    return None\n\ndef deploy():\n    timeout = 30\n    retries = 5\n"
    )
    target.write_text(original, encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "module.py",
            "old_string": "def deploy():\n    timeout = 20\n    retries = 5",
            "new_string": "def deploy():\n    timeout = 60\n    retries = 5",
        },
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "Closest raw candidates" in error["message"]
    assert "Candidate 1 (starting line 4; raw text):" in error["message"]
    assert "def deploy():\n    timeout = 30\n    retries = 5" in error["message"]
    assert "4|" not in error["message"]
    assert target.read_text(encoding="utf-8") == original


def test_edit_no_match_omits_weak_candidates(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "totally unrelated locator", "new_string": "x"},
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "Closest raw candidates" not in error["message"]
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_edit_no_match_candidates_use_recovered_gutterfree_pattern(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha = 2\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "1| alpha = 1\n2| beta", "new_string": "x"},
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "after removing read's line-number gutter" in error["message"]
    assert "alpha = 2\nbeta" in error["message"]
    assert "1|" not in error["message"]
    assert target.read_text(encoding="utf-8") == "alpha = 2\nbeta\n"


def test_edit_rejects_line_numbered_new_string(tmp_path: Path) -> None:
    # A model that pastes read's ``N| `` gutter into the replacement must be
    # stopped before it writes line-number prefixes into the file.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha\nbeta", "new_string": "1| one\n2| two"},
    )

    error = assert_failure_envelope(result, "line_numbered_content")
    assert "line-number" in error["message"]
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_edit_uses_current_read_gutter_in_old_string(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "1| hello\n2| world", "new_string": "hi"},
    )

    data = assert_success_envelope(result)
    assert data["replacements"] == 1
    assert target.read_text(encoding="utf-8") == "hi\n"


def test_edit_uses_compact_read_gutter_in_old_string(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "5|alpha\n6|beta", "new_string": "updated"},
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "updated\n"


def test_edit_uses_continuation_read_gutter_in_old_string(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("fragment\nnext\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "old_string": "50:50001| fragment\n51| next",
            "new_string": "replacement",
        },
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "replacement\n"


def test_edit_keeps_ambiguity_after_stripping_old_string_gutter(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\nalpha\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "1| alpha\n2| beta", "new_string": "updated"},
    )

    error = assert_failure_envelope(result, "ambiguous_match")
    assert "Found 2 occurrences" in error["message"]
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\nalpha\nbeta\n"

    replace_all_result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "old_string": "1| alpha\n2| beta",
            "new_string": "updated",
            "replace_all": True,
        },
    )

    replace_all_data = assert_success_envelope(replace_all_result)
    assert replace_all_data["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "updated\nupdated\n"


def test_edit_prefers_raw_match_for_real_gutter_shaped_file_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("1| alpha\n2| beta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "1| alpha\n2| beta", "new_string": "updated"},
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "updated\n"


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


def test_edit_allows_unread_existing_file_when_match_is_unique(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "ALPHA beta\n"


def test_edit_guard_allows_after_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "ALPHA beta\n"


def test_edit_uses_current_content_when_file_changed_since_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    # External change after the read (longer content → size drift).
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert "changed since this Session last read it" in data["stale_warning"]
    assert target.read_text(encoding="utf-8") == "ALPHA beta gamma\n"


def test_edit_changed_file_still_fails_when_current_text_no_longer_matches(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())
    target.write_text("gamma beta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )

    assert_failure_envelope(result, "text_not_found")
    assert target.read_text(encoding="utf-8") == "gamma beta\n"


def test_edit_guard_restamps_so_next_edit_needs_no_reread(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-1", target.resolve())

    first = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
        file_state=file_state,
    )
    second = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "beta", "new_string": "BETA"},
        file_state=file_state,
    )

    assert_success_envelope(first)
    assert_success_envelope(second)
    assert target.read_text(encoding="utf-8") == "ALPHA BETA\n"


def test_concurrent_session_edits_serialize_and_merge_current_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    file_state = FileReadState()
    file_state.record_read("session-a", target.resolve())
    file_state.record_read("session-b", target.resolve())

    from core.tools.file_state import atomic_write_bytes as real_atomic_write_bytes

    first_write_started = threading.Event()
    release_first_write = threading.Event()
    second_write_started = threading.Event()
    call_count = 0
    call_count_lock = threading.Lock()

    def observed_atomic_write(path: Path, payload: bytes) -> None:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_write_started.set()
            release_first_write.wait(timeout=1)
        else:
            second_write_started.set()
        real_atomic_write_bytes(path, payload)

    monkeypatch.setattr("core.tools.edit.atomic_write_bytes", observed_atomic_write)
    results: dict[str, dict[str, object]] = {}

    def edit_alpha() -> None:
        results["a"] = edit_handler(
            make_context(workspace, session_id="session-a"),
            {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
            file_state=file_state,
        )

    def edit_beta() -> None:
        results["b"] = edit_handler(
            make_context(workspace, session_id="session-b"),
            {"path": "notes.txt", "old_string": "beta", "new_string": "BETA"},
            file_state=file_state,
        )

    first = threading.Thread(target=edit_alpha)
    second = threading.Thread(target=edit_beta)
    first.start()
    assert first_write_started.wait(timeout=1)
    second.start()
    assert second_write_started.wait(timeout=0.05) is False
    release_first_write.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert results["a"]["ok"] is True
    assert results["b"]["ok"] is True
    second_data = results["b"]["data"]
    assert isinstance(second_data, dict)
    assert "stale_warning" in second_data
    assert target.read_text(encoding="utf-8") == "ALPHA BETA\n"


def test_edit_diffs_against_read_baseline_not_rendered_gutter(tmp_path: Path) -> None:
    # Regression: the read baseline must be the raw file text, never the
    # numbered `N| ` rendering. A single-line edit of a multi-line file must
    # count exactly one added and one removed line, not every line of the file.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tracker = ChangeTracker()
    tracker.record_read("session-1", target.resolve(), "alpha\nbeta\ngamma\n")

    result = edit_handler(
        make_context(workspace, change_tracker=tracker),
        {"path": "notes.txt", "old_string": "beta", "new_string": "BETA"},
    )

    assert_success_envelope(result)
    stats = tracker.take_run_stats("session-1")
    assert stats is not None
    assert stats["files"] == 1
    assert stats["added"] == 1
    assert stats["removed"] == 1
    assert stats["paths"] == [str(target.resolve())]
