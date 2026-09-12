"""Edit: batch behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tools.edit import (
    edit_handler,
    register_edit_tool,
)
from core.tools.file_state import FileReadState
from core.tools.tools import ToolRegistry, tool_success
from core.utils.paths import model_path
from tests.core.tools.edit_helpers import (
    assert_failure_envelope,
    make_context,
)


def test_multi_edit_applies_items_in_order_across_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("alpha beta\n", encoding="utf-8")
    second.write_text("one\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "edits": [
                {"path": "first.txt", "old_string": "alpha", "new_string": "ALPHA"},
                {"path": "first.txt", "old_string": "ALPHA beta", "new_string": "done"},
                {"path": "second.txt", "old_string": "one", "new_string": "two"},
            ]
        },
    )

    data = result["data"]
    assert result["ok"] is True
    assert isinstance(data, dict)
    assert data["status"] == "success"
    assert data["succeeded"] == 3
    assert data["failed"] == 0
    assert [item["index"] for item in data["results"]] == [0, 1, 2]
    assert all("message" not in item for item in data["results"])
    assert first.read_text(encoding="utf-8") == "done\n"
    assert second.read_text(encoding="utf-8") == "two\n"


def test_multi_edit_uses_top_level_path_as_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "gamma", "new_string": "GAMMA"},
            ],
        },
    )

    data = result["data"]
    assert result["ok"] is True
    assert isinstance(data, dict)
    assert data["status"] == "success"
    assert data["succeeded"] == 2
    assert target.read_text(encoding="utf-8") == "ALPHA beta GAMMA\n"


def test_multi_edit_explicit_path_overrides_top_level(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    default_file = workspace / "default.txt"
    other = workspace / "other.txt"
    default_file.write_text("alpha\n", encoding="utf-8")
    other.write_text("beta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "default.txt",
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"path": "other.txt", "old_string": "beta", "new_string": "BETA"},
            ],
        },
    )

    data = result["data"]
    assert result["ok"] is True
    assert isinstance(data, dict)
    assert data["status"] == "success"
    assert data["succeeded"] == 2
    assert default_file.read_text(encoding="utf-8") == "ALPHA\n"
    assert other.read_text(encoding="utf-8") == "BETA\n"


def test_multi_edit_rejects_item_without_any_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = edit_handler(
        make_context(workspace),
        {
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
            ],
        },
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "path" in error["message"]


def test_multi_edit_top_level_path_counts_as_file_in_display_facts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_edit_tool(registry, file_state=FileReadState())
    arguments = {
        "path": "a.txt",
        "edits": [
            {"old_string": "secret old", "new_string": "secret new"},
            {"old_string": "x", "new_string": "y"},
        ],
    }
    result = tool_success(
        {"status": "success", "total": 2, "succeeded": 2, "failed": 0, "results": []}
    )

    payload = registry.display_for_call("edit", arguments, result=result)

    assert payload["summary"] == "a.txt"
    assert payload["facts"] == [
        {"kind": "count", "value": 2, "unit": "edits", "at_least": False},
        {"kind": "count", "value": 1, "unit": "files", "at_least": False},
    ]


def test_multi_edit_continues_after_an_item_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")
    context = make_context(workspace)

    result = edit_handler(
        context,
        {
            "edits": [
                {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
                {"path": "notes.txt", "old_string": "missing", "new_string": "nope"},
                {"path": "notes.txt", "old_string": "gamma", "new_string": "GAMMA"},
            ]
        },
    )

    data = result["data"]
    assert result["ok"] is True
    assert isinstance(data, dict)
    assert data["status"] == "partial"
    assert data["succeeded"] == 2
    assert data["failed"] == 1
    assert data["results"][1]["error"]["code"] == "text_not_found"
    assert target.read_text(encoding="utf-8") == "ALPHA beta GAMMA\n"
    assert context.presentation_facts == [
        {"kind": "line_change", "change": "added", "value": 2},
        {"kind": "line_change", "change": "removed", "value": 2},
    ]


@pytest.mark.asyncio
async def test_registered_multi_edit_validates_each_item_without_rejecting_batch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    registry = ToolRegistry()
    register_edit_tool(registry, file_state=FileReadState())

    result = await registry.dispatch(
        make_context(workspace),
        {
            "edits": [
                {"path": "notes.txt", "old_string": "", "new_string": "bad"},
                {"old_string": "alpha", "new_string": "bad"},
                "not an object",
                {"path": "notes.txt", "old_string": "beta", "new_string": "BETA"},
            ]
        },
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert data["status"] == "partial"
    assert data["succeeded"] == 1
    assert data["failed"] == 3
    assert [item["error"]["code"] for item in data["results"][:3]] == [
        "invalid_arguments",
        "invalid_arguments",
        "invalid_arguments",
    ]
    assert data["results"][3]["ok"] is True
    assert target.read_text(encoding="utf-8") == "alpha BETA\n"


@pytest.mark.asyncio
async def test_registered_edit_rejects_retired_flat_shape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\n", encoding="utf-8")
    registry = ToolRegistry()
    register_edit_tool(registry, file_state=FileReadState())

    result = await registry.dispatch(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
    )

    assert_failure_envelope(result, "invalid_arguments")
    assert target.read_text(encoding="utf-8") == "alpha\n"


def test_multi_edit_continues_after_directory_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("folder").mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "edits": [
                {"path": "folder", "old_string": "x", "new_string": "y"},
                {"path": "notes.txt", "old_string": "alpha", "new_string": "ALPHA"},
            ]
        },
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    assert data["status"] == "partial"
    assert data["results"][0]["error"]["code"] == "not_a_file"
    assert data["results"][1]["ok"] is True
    assert target.read_text(encoding="utf-8") == "ALPHA\n"


def test_multi_edit_preview_keeps_only_first_and_last_distant_regions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text(
        "target\nkeep\nkeep\nkeep\ntarget\nkeep\nkeep\nkeep\ntarget\n",
        encoding="utf-8",
    )

    result = edit_handler(
        make_context(workspace),
        {
            "edits": [
                {
                    "path": "notes.txt",
                    "old_string": "target",
                    "new_string": "changed",
                    "replace_all": True,
                }
            ]
        },
    )

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    item = data["results"][0]
    assert item["first_changed_line"] == 1
    assert item["last_changed_line"] == 9
    assert item["preview_omitted_regions"] == 1
    assert item["preview"] == [
        {
            "before": ["1| target", "2| keep"],
            "after": ["1| changed", "2| keep"],
        },
        {
            "before": ["8| keep", "9| target"],
            "after": ["8| keep", "9| changed"],
        },
    ]


def test_multi_edit_reports_new_string_gutter_normalization_per_item(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "edits": [
                {
                    "path": "notes.txt",
                    "old_string": "alpha\nbeta",
                    "new_string": "20| ALPHA\n21| BETA",
                }
            ]
        },
    )

    data = result["data"]
    assert result["ok"] is True
    assert isinstance(data, dict)
    assert data["results"][0]["normalization_warning"] == (
        "Removed read line-number gutters from new_string before applying the edit."
    )
    assert target.read_text(encoding="utf-8") == "ALPHA\nBETA\n"


def test_multi_edit_retry_reports_precise_already_applied_noop(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("setting = old\n", encoding="utf-8")
    arguments = {
        "edits": [
            {
                "path": "notes.txt",
                "old_string": "setting = old",
                "new_string": "setting = new",
            }
        ]
    }

    first = edit_handler(make_context(workspace), arguments)
    retry_context = make_context(workspace)
    second = edit_handler(retry_context, arguments)

    assert first["ok"] is True
    data = second["data"]
    assert second["ok"] is True
    assert isinstance(data, dict)
    assert data["results"] == [
        {
            "index": 0,
            "ok": True,
            "path": model_path(target.resolve()),
            "replacements": 0,
            "already_applied": True,
        }
    ]
    assert retry_context.presentation_facts == []
    assert target.read_text(encoding="utf-8") == "setting = new\n"


def test_multi_edit_does_not_infer_already_applied_without_shared_context(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("omega\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"edits": [{"path": "notes.txt", "old_string": "alpha", "new_string": "omega"}]},
    )

    assert_failure_envelope(result, "text_not_found")


def test_multi_edit_keeps_ambiguous_approximate_match_safe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("value = 2\nvalue = 2\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "edits": [
                {
                    "path": "notes.txt",
                    "old_string": "value = 1",
                    "new_string": "value = 2",
                }
            ]
        },
    )

    assert_failure_envelope(result, "ambiguous_match")


def test_multi_edit_does_not_infer_already_applied_deletion(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("keep\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"edits": [{"path": "notes.txt", "old_string": "removed\n", "new_string": ""}]},
    )

    assert_failure_envelope(result, "text_not_found")


def test_multi_edit_replace_all_retry_reports_already_applied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")
    arguments = {
        "edits": [
            {
                "path": "notes.txt",
                "old_string": "value = 1",
                "new_string": "value = 2",
                "replace_all": True,
            }
        ]
    }

    assert edit_handler(make_context(workspace), arguments)["ok"] is True
    retry = edit_handler(make_context(workspace), arguments)

    assert retry["ok"] is True
    assert retry["data"]["results"][0]["already_applied"] is True
    assert retry["data"]["results"][0]["replacements"] == 0
    assert target.read_text(encoding="utf-8") == "value = 2\nvalue = 2\n"


def test_multi_edit_returns_failure_when_every_item_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("alpha\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "edits": [
                {"path": "notes.txt", "old_string": "missing", "new_string": "one"},
                {"path": "absent.txt", "old_string": "old", "new_string": "new"},
            ]
        },
    )

    error = assert_failure_envelope(result, "all_edits_failed")
    assert "Edit 0 (notes.txt): [text_not_found]" in error["message"]
    assert "Edit 1 (absent.txt): [file_not_found]" in error["message"]


def test_multi_edit_display_hides_bodies_and_summarizes_batch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_edit_tool(registry, file_state=FileReadState())
    arguments = {
        "edits": [
            {"path": "a.txt", "old_string": "secret old", "new_string": "secret new"},
            {"path": "b.txt", "old_string": "x", "new_string": "y"},
            {"path": "a.txt", "old_string": "z", "new_string": "q"},
        ]
    }
    result = tool_success(
        {"status": "partial", "total": 3, "succeeded": 2, "failed": 1, "results": []}
    )

    payload = registry.display_for_call("edit", arguments, result=result)

    assert payload["summary"] == "a.txt"
    assert payload["hidden_argument_keys"] == ["edits", "new_string", "old_string"]
    assert payload["facts"] == [
        {"kind": "count", "value": 3, "unit": "edits", "at_least": False},
        {"kind": "count", "value": 2, "unit": "files", "at_least": False},
        {"kind": "count", "value": 1, "unit": "failures", "at_least": False},
    ]
