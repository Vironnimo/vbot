"""Edit: matching behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tools.edit import (
    EDIT_TOOL_DESCRIPTION,
    EDIT_TOOL_NAME,
    EDIT_TOOL_PARAMETERS,
    edit_handler,
    register_edit_tool,
)
from core.tools.file_state import FileReadState
from core.tools.read import render_text_file
from core.tools.tools import ToolRegistry
from core.utils.paths import model_path
from tests.core.tools.edit_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    make_context,
)


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
    assert tool.description == (
        "Apply ordered text replacements across files in one call. Every edit is "
        "attempted even if another fails."
    )
    assert tool.description
    assert tool.parameters == EDIT_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["edit"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "edit"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["edits"]
    assert "additionalProperties" not in parameters
    assert set(parameters["properties"]) == {"path", "edits"}
    assert parameters["properties"]["path"]["description"] == (
        "Default file to edit; used by edits that omit their own path."
    )
    edits_schema = parameters["properties"]["edits"]
    assert edits_schema["minItems"] == 1
    assert edits_schema["description"] == "Replacements to attempt in order."
    item_schema = edits_schema["items"]
    assert item_schema["required"] == ["old_string", "new_string"]
    assert set(item_schema["properties"]) == {
        "path",
        "old_string",
        "new_string",
        "replace_all",
    }
    assert item_schema["properties"]["path"]["description"] == (
        "File to edit, relative to the working directory or absolute. "
        "Omit to use the top-level path."
    )
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in item_schema["properties"].values()
    )
    assert "default" not in item_schema["properties"]["replace_all"]
    assert item_schema["properties"]["new_string"]["description"] == (
        "Replacement text. Use an empty string to delete old_string."
    )
    assert "filePath" not in parameters["properties"]
    assert tool.open_input_schema is True
    assert tool.handler_validates_arguments is True


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
    assert data["last_changed_line"] == 1
    assert data["preview"] == [{"before": ["1| hello workspace"], "after": ["1| hello agent"]}]
    assert data["path"] == model_path(target.resolve())


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


def test_edit_preview_centers_a_change_inside_a_long_line(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("a" * 500 + "old" + "z" * 500 + "\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "old", "new_string": "new"},
    )

    data = assert_success_envelope(result)
    preview = data["preview"]
    assert isinstance(preview, list)
    region = preview[0]
    assert isinstance(region, dict)
    before = region["before"]
    after = region["after"]
    assert isinstance(before, list)
    assert isinstance(after, list)
    before_line = before[0]
    after_line = after[0]
    assert isinstance(before_line, str)
    assert isinstance(after_line, str)
    assert before_line.startswith("1:")
    assert "old" in before_line
    assert "new" in after_line
    assert len(before_line) < 270


def test_edit_last_changed_line_includes_multiline_replacement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("before\ntarget\nafter\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "target", "new_string": "one\ntwo\nthree"},
    )

    data = assert_success_envelope(result)
    assert data["first_changed_line"] == 2
    assert data["last_changed_line"] == 4
    assert data["preview"] == [
        {
            "before": ["1| before", "2| target", "3| after"],
            "after": ["1| before", "2| one", "3| two", "4| three", "5| after"],
        }
    ]


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
            "old_string": ("def deploy():\n    completely unrelated declaration\n    retries = 5"),
            "new_string": "def deploy():\n    timeout = 60\n    retries = 5",
        },
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "Closest raw candidates" in error["message"]
    assert "Candidate 1 (starting line 4; raw text):" in error["message"]
    assert "def deploy():\n    timeout = 30\n    retries = 5" in error["message"]
    assert "First difference from old_string at line 2, column 5 (file line 5):" in error["message"]
    assert "old_string: 'completely unrelated declaration'" in error["message"]
    assert "file: 'timeout = 30'" in error["message"]
    assert "4|" not in error["message"]
    assert target.read_text(encoding="utf-8") == original


def test_edit_tolerates_the_word_difference_from_reproduced_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "subagent.py"
    original = (
        '    "Continue work that does not depend on the result, or finish the current Run '
        'now. Do not "\n'
    )
    target.write_text(original, encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "subagent.py",
            "old_string": (
                '    "Continue work that does not depend on the result, or finish your current '
                'Run now. Do not "'
            ),
            "new_string": "replacement",
        },
    )

    data = assert_success_envelope(result)
    assert data["replacements"] == 1
    assert target.read_text(encoding="utf-8") == "replacement\n"


def test_edit_no_match_reports_file_end_as_first_difference(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "old_string": "alpha\nbeta extra",
            "new_string": "replacement",
        },
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "First difference from old_string at line 2, column 5 (file line 2):" in error["message"]
    assert "old_string: ' extra'" in error["message"]
    assert "file: '<end of text>'" not in error["message"]
    assert "file: <end of text>" in error["message"]
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


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
        {
            "path": "notes.txt",
            "old_string": "1| alpha = 1234567890\n2| beta",
            "new_string": "x",
        },
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "after removing read's line-number gutter" in error["message"]
    assert "alpha = 2\nbeta" in error["message"]
    assert "1|" not in error["message"]
    assert target.read_text(encoding="utf-8") == "alpha = 2\nbeta\n"


def test_edit_strips_complete_line_number_gutter_from_new_string(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    context = make_context(workspace)

    result = edit_handler(
        context,
        {"path": "notes.txt", "old_string": "alpha\nbeta", "new_string": "1| one\n2| two"},
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert data["normalization_warning"] == (
        "Removed read line-number gutters from new_string before applying the edit."
    )
    assert target.read_text(encoding="utf-8") == "one\ntwo\n"
    assert context.presentation_facts == [
        {"kind": "line_change", "change": "added", "value": 2},
        {"kind": "line_change", "change": "removed", "value": 2},
    ]


@pytest.mark.parametrize(
    "separator",
    ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
    ids=["vt", "ff", "fs", "gs", "rs", "nel", "line-separator", "paragraph-separator"],
)
def test_edit_reuses_read_block_without_normalizing_exotic_separators(
    tmp_path: Path, separator: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    original = f"alpha{separator}\tbeta\n".encode()
    target.write_bytes(original)
    read_content = render_text_file(original)

    result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "old_string": read_content,
            "new_string": read_content.replace("beta", "BETA"),
        },
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert data["normalization_warning"] == (
        "Removed read line-number gutters from new_string before applying the edit."
    )
    assert target.read_bytes() == f"alpha{separator}\tBETA\n".encode()


@pytest.mark.parametrize(
    "new_string",
    [
        "1| one\nplain line\n2| two",
        "1| one\n3| three",
    ],
)
def test_edit_rejects_incomplete_or_nonconsecutive_new_string_gutter(
    tmp_path: Path, new_string: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha\nbeta", "new_string": new_string},
    )

    assert_failure_envelope(result, "line_numbered_content")
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_edit_rejects_continuation_gutter_in_new_string(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "old_string": "alpha\nbeta",
            "new_string": "10:50001| fragment\n11| next",
        },
    )

    error = assert_failure_envelope(result, "line_numbered_content")
    assert "N:C| continuation gutter" in error["message"]
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


def test_edit_strips_single_line_gutter_in_old_string(tmp_path: Path) -> None:
    # The most common gutter mistake: a single-line edit with the read gutter
    # left in place. The block-level detector needs >= 2 lines, so this only
    # works via the per-line fallback.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "module.py", "old_string": "2|     return 1", "new_string": "    return 42"},
    )

    data = assert_success_envelope(result)
    assert data["first_changed_line"] == 2
    assert target.read_text(encoding="utf-8") == "def f():\n    return 42\n"


def test_edit_strips_partial_gutter_in_old_string(tmp_path: Path) -> None:
    # The model pasted a block from read output but dropped the gutter on one
    # line (the one it edited). The block-level detector rejects this because
    # not every line carries a gutter; the per-line fallback handles it.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("def deploy():\n    timeout = 30\n    retries = 5\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "module.py",
            "old_string": "1| def deploy():\n    timeout = 30\n3|     retries = 5",
            "new_string": "def deploy():\n    timeout = 60\n    retries = 5",
        },
    )

    assert_success_envelope(result)
    assert (
        target.read_text(encoding="utf-8") == "def deploy():\n    timeout = 60\n    retries = 5\n"
    )


def test_edit_per_line_gutter_still_detects_ambiguity(tmp_path: Path) -> None:
    # When the per-line fallback strips a single-line gutter and the result
    # matches multiple locations, the edit must still report ambiguity rather
    # than silently picking one.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("same\nother\nsame\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "1| same", "new_string": "changed"},
    )

    error = assert_failure_envelope(result, "ambiguous_match")
    assert "Found 2 occurrences" in error["message"]
    assert target.read_text(encoding="utf-8") == "same\nother\nsame\n"


def test_edit_per_line_gutter_diagnostic_uses_stripped_pattern(tmp_path: Path) -> None:
    # When the per-line fallback strips a gutter but the stripped text still
    # doesn't match, the closest-candidates diagnostic should search with the
    # gutter-free pattern, not the original guttered text.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha = 2\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "old_string": "1| alpha = 1234567890",
            "new_string": "alpha = 9",
        },
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "after removing read's line-number gutter" in error["message"]
    assert "alpha = 2" in error["message"]
    assert "1|" not in error["message"]
    assert target.read_text(encoding="utf-8") == "alpha = 2\nbeta\n"


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
