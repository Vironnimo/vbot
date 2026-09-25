from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.config_validation import JsonDiagnostic, add_error
from core.json_documents import (
    FORMAT_VERSION_FIELD,
    OPAQUE,
    JsonDocumentFormat,
    JsonDocumentWriteError,
    durable_document_paths,
    is_durable_document_path,
    json_document,
    json_list,
    json_map,
    json_object,
    preserve_unknown_fields,
    render_json_document,
    strip_unknown_fields,
    validate_collection_root,
    validate_format_version,
    warn_unknown_fields,
    write_json_document,
)

ENTRY = json_object({"id", "name", "options"}, {"options": json_object({"level"})})
SHAPE = json_document(
    {"title", "entries", "tools", "blob"},
    {
        "entries": json_list(ENTRY, key="id"),
        "tools": json_map(json_object({"enabled"}), known={"bash": json_object({"env"})}),
        "blob": OPAQUE,
    },
)


def _validate(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        add_error(diagnostics, "$", "must be an object")
        return diagnostics
    if not validate_format_version(diagnostics, data, 1):
        return diagnostics
    if "title" in data and not isinstance(data["title"], str):
        add_error(diagnostics, "$.title", "must be a string")
    return diagnostics


FORMAT = JsonDocumentFormat(name="test document", version=1, shape=SHAPE, validate=_validate)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_strip_unknown_fields_keeps_only_modeled_fields_at_every_level() -> None:
    raw = {
        "format_version": 1,
        "title": "t",
        "future": 1,
        "entries": [{"id": "a", "name": "A", "extra": True, "options": {"level": 1, "x": 2}}],
        "tools": {"bash": {"env": [], "shell": "zsh"}, "other": {"enabled": True, "y": 1}},
        "blob": {"anything": {"goes": 1}},
    }

    assert strip_unknown_fields(raw, SHAPE) == {
        "format_version": 1,
        "title": "t",
        "entries": [{"id": "a", "name": "A", "options": {"level": 1}}],
        "tools": {"bash": {"env": []}, "other": {"enabled": True}},
        "blob": {"anything": {"goes": 1}},
    }


def test_strip_unknown_fields_returns_an_independent_copy() -> None:
    raw = {"blob": {"nested": [1]}}

    stripped = strip_unknown_fields(raw, SHAPE)
    stripped["blob"]["nested"].append(2)

    assert raw == {"blob": {"nested": [1]}}


def test_preserve_unknown_fields_restores_unknowns_where_the_object_remains() -> None:
    previous = {
        "title": "old",
        "future": {"keep": True},
        "entries": [
            {"id": "a", "name": "A", "extra": 1, "options": {"level": 1, "x": 2}},
            {"id": "gone", "name": "G", "extra": 2},
        ],
        "tools": {"bash": {"env": [], "shell": "zsh"}, "other": {"enabled": True, "y": 1}},
        "blob": {"old": True},
    }
    current = {
        "title": "new",
        "entries": [{"id": "a", "name": "A2", "options": {"level": 3}}, {"id": "b", "name": "B"}],
        "tools": {"bash": {"env": ["X"]}},
        "blob": {"new": True},
    }

    assert preserve_unknown_fields(previous, current, SHAPE) == {
        "title": "new",
        "entries": [
            {"id": "a", "name": "A2", "options": {"level": 3, "x": 2}, "extra": 1},
            {"id": "b", "name": "B"},
        ],
        "tools": {"bash": {"env": ["X"], "shell": "zsh"}},
        "blob": {"new": True},
        "future": {"keep": True},
    }


def test_preserve_unknown_fields_lets_the_current_value_win() -> None:
    previous = {"future": "old"}
    current = {"future": "owner"}

    assert preserve_unknown_fields(previous, current, SHAPE) == {"future": "owner"}


def test_preserve_unknown_fields_skips_entries_with_ambiguous_identity() -> None:
    previous = {"entries": [{"id": "a", "extra": 1}, {"id": "a", "extra": 2}]}
    current = {"entries": [{"id": "a", "name": "A"}]}

    assert preserve_unknown_fields(previous, current, SHAPE) == {
        "entries": [{"id": "a", "name": "A"}]
    }


def test_warn_unknown_fields_reports_every_modeled_level() -> None:
    diagnostics: list[JsonDiagnostic] = []
    raw = {
        "format_version": 1,
        "future": 1,
        "entries": [{"id": "a", "extra": True, "options": {"x": 2}}],
        "tools": {"bash": {"shell": "zsh"}},
        "blob": {"anything": 1},
    }

    warn_unknown_fields(diagnostics, "$", raw, SHAPE, label="test field")

    assert [(d.severity, d.path, d.message) for d in diagnostics] == [
        ("warning", "$.future", "unknown test field: future"),
        ("warning", "$.entries[0].extra", "unknown test field: extra"),
        ("warning", "$.entries[0].options.x", "unknown test field: x"),
        ("warning", "$.tools.bash.shell", "unknown test field: shell"),
    ]


def test_json_object_rejects_nested_shapes_for_undeclared_fields() -> None:
    with pytest.raises(ValueError, match="undeclared fields: other"):
        json_object({"a"}, {"other": OPAQUE})


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "is required"),
        ({"format_version": True}, "must be a positive integer"),
        ({"format_version": "1"}, "must be a positive integer"),
        ({"format_version": 0}, "must be a positive integer"),
        ({"format_version": 2}, "written by a newer vBot and is left unchanged"),
    ],
)
def test_validate_format_version_rejects_unusable_versions(
    data: dict[str, Any], message: str
) -> None:
    diagnostics: list[JsonDiagnostic] = []

    assert validate_format_version(diagnostics, data, 1) is False
    assert [diagnostic.path for diagnostic in diagnostics] == ["$.format_version"]
    assert message in diagnostics[0].message


def test_validate_format_version_rejects_an_older_version() -> None:
    diagnostics: list[JsonDiagnostic] = []

    assert validate_format_version(diagnostics, {"format_version": 1}, 2) is False
    assert "convert the file first" in diagnostics[0].message


def test_validate_format_version_accepts_the_current_version() -> None:
    diagnostics: list[JsonDiagnostic] = []

    assert validate_format_version(diagnostics, {"format_version": 1}, 1) is True
    assert diagnostics == []


@pytest.mark.parametrize(
    ("data", "path", "message"),
    [
        ([], "$", "Expected a JSON object, got list"),
        ({"entries": []}, "$.format_version", "is required"),
        ({"format_version": 1}, "$.entries", "is required"),
        ({"format_version": 1, "entries": {}}, "$.entries", "must be an array"),
    ],
)
def test_validate_collection_root_rejects_an_unreadable_root(
    data: Any, path: str, message: str
) -> None:
    diagnostics: list[JsonDiagnostic] = []

    assert (
        validate_collection_root(diagnostics, data, version=1, shape=SHAPE, collection="entries")
        is None
    )
    assert [(item.severity, item.path) for item in diagnostics] == [("error", path)]
    assert diagnostics[0].message.startswith(message)


def test_validate_collection_root_returns_the_entries_and_warns_on_unknown_fields() -> None:
    diagnostics: list[JsonDiagnostic] = []
    data = {"format_version": 1, "future": 1, "entries": [{"id": "a"}, "broken"]}

    entries = validate_collection_root(
        diagnostics, data, version=1, shape=SHAPE, collection="entries"
    )

    assert entries == [{"id": "a"}, "broken"]
    assert [(item.severity, item.path) for item in diagnostics] == [("warning", "$.future")]


def test_write_json_document_creates_a_versioned_document(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "doc.json"

    write_json_document(path, {"title": "ünï", "format_version": 99}, FORMAT)

    text = path.read_text(encoding="utf-8")
    assert text == '{\n  "format_version": 1,\n  "title": "ünï"\n}\n'


def test_write_json_document_round_trips_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "title": "old",
                "future": [1, 2],
                "entries": [{"id": "a", "name": "A", "extra": {"deep": 1}}],
            }
        ),
        encoding="utf-8",
    )
    loaded = strip_unknown_fields(_read(path), SHAPE)
    loaded["title"] = "new"

    write_json_document(path, loaded, FORMAT)

    assert _read(path) == {
        "format_version": 1,
        "title": "new",
        "entries": [{"id": "a", "name": "A", "extra": {"deep": 1}}],
        "future": [1, 2],
    }


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"title": "missing version"}),
        json.dumps({"format_version": 2, "title": "newer"}),
        json.dumps({"format_version": 1, "title": 5}),
    ],
)
def test_write_json_document_never_overwrites_a_file_that_failed_to_load(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "doc.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(JsonDocumentWriteError, match="Refusing to overwrite test document"):
        write_json_document(path, {"title": "new"}, FORMAT)

    assert path.read_text(encoding="utf-8") == content


def test_write_json_document_reset_replaces_a_file_that_failed_to_load(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"format_version": 2, "future": 1}), encoding="utf-8")

    write_json_document(path, {"title": "reset"}, FORMAT, reset=True)

    assert _read(path) == {"format_version": 1, "title": "reset"}


def test_render_json_document_sorts_nested_keys_after_the_version() -> None:
    text = render_json_document(
        {"b": {"z": 1, "a": [{"y": 1, "x": 2}]}, "a": 1}, version=1, sort_keys=True
    )

    assert list(json.loads(text)) == [FORMAT_VERSION_FIELD, "a", "b"]
    assert text.index('"x"') < text.index('"y"')
    assert text.index('"a": [') < text.index('"z"')


def test_durable_document_paths_lists_exactly_the_documents_in_scope(tmp_path: Path) -> None:
    documents = [
        "settings.json",
        "agents/main/agent.json",
        "agents/order.json",
        "agents/main/prompts/layout.json",
        "prompts/layout.json",
        "channels/telegram/channel.json",
        "projects/site/project.json",
        "cron/jobs.json",
        "bootstrap/jobs.json",
        "calendar/events.json",
        "calendar/actions.json",
        "skills/policy.json",
        "terminals/launch-history.json",
        "terminals/groups.json",
        "oauth/github-copilot-oauth.json",
        "mcp/connections.json",
    ]
    others = [
        "agents/main/memory.json",
        "agents/.staged/agent.json",
        "agents/main/.agent.json.tmp",
        "oauth/nested/token.json",
        "workspaces/main/settings.json",
        "channels/telegram/state.json",
    ]
    for relative in documents + others:
        path = tmp_path.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    (tmp_path / "cron" / "jobs.json.d").mkdir()

    assert durable_document_paths(tmp_path) == tuple(sorted(documents))
    assert durable_document_paths(tmp_path / "missing") == ()
    assert all(is_durable_document_path(relative) for relative in documents)
    assert not any(is_durable_document_path(relative) for relative in others)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/settings.json",
        "../settings.json",
        "agents//agent.json",
        "agents\\main\\agent.json",
        "C:settings.json",
        "agents/./agent.json",
        "SETTINGS.JSON",
    ],
)
def test_is_durable_document_path_rejects_unsafe_or_foreign_paths(path: str) -> None:
    assert not is_durable_document_path(path)
