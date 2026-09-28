"""The block-model prompt store: ``layout.json`` plus per-block text overrides per scope."""

import json
from pathlib import Path

import pytest

from core.prompts import LayoutEntry
from core.storage import DataDirectoryLayout, StorageError, StorageManager
from core.storage.prompt_blocks import PromptBlockStore


def make_store(tmp_path: Path) -> PromptBlockStore:
    """Build a store with a real ensure-directories hook over a temp data dir."""

    def ensure_directories() -> None:
        layout = DataDirectoryLayout(tmp_path)
        for directory in (layout.atomic_temporary, layout.prompts, layout.agents):
            directory.mkdir(parents=True, exist_ok=True)

    return PromptBlockStore(data_dir=tmp_path, ensure_directories=ensure_directories)


def _no_staging_leftovers(tmp_path: Path) -> bool:
    return list(DataDirectoryLayout(tmp_path).atomic_temporary.iterdir()) == []


# --------------------------------------------------------------------------
# id -> path mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "block_id", "relative_path"),
    [
        (None, "tool:bash", "prompts/blocks/tool/bash.md"),
        (None, "memory:guidance", "prompts/blocks/memory/guidance.md"),
        (None, "user:my_rules", "prompts/blocks/user/my_rules.md"),
        (None, "user:Block1", "prompts/blocks/user/Block1.md"),
        # The agent-id rule constrains only the first character.
        (None, "user:trailing-", "prompts/blocks/user/trailing-.md"),
        ("assistant", "extension:weather", "agents/assistant/prompts/blocks/extension/weather.md"),
    ],
)
def test_block_override_path_maps_namespace_and_slug_without_a_colon(
    tmp_path: Path, scope: str | None, block_id: str, relative_path: str
) -> None:
    path = make_store(tmp_path).block_override_path(scope, block_id)

    assert path == tmp_path / relative_path
    assert ":" not in str(path.relative_to(tmp_path))


@pytest.mark.parametrize(
    ("scope", "block_id"),
    [
        (None, "user:../escape"),
        (None, "tool:sub/dir"),
        (None, "tool:sub\\dir"),
        (None, "user:/absolute"),
        (None, "user:C:\\windows"),
        (None, "user:.."),
        (None, "user:has space"),
        (None, "user:-leading"),
        # The agent-id rule caps a slug at 64 characters.
        (None, "user:" + "x" * 65),
        (None, "bogus:thing"),
        (None, "noprefix"),
        ("../escape", "tool:bash"),
    ],
)
def test_block_override_path_rejects_unsafe_ids_and_scopes(
    tmp_path: Path, scope: str | None, block_id: str
) -> None:
    with pytest.raises(StorageError):
        make_store(tmp_path).block_override_path(scope, block_id)


# --------------------------------------------------------------------------
# layout.json
# --------------------------------------------------------------------------


def test_layout_round_trip_preserves_order_and_flags(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.read_layout(None) == []
    assert store.read_layout("assistant") == []
    entries = [
        LayoutEntry(id="core:intro", enabled=True, source="core"),
        LayoutEntry(id="tool:bash", enabled=False, source="tool"),
        LayoutEntry(id="user:my-rules", enabled=True, source="user"),
    ]

    written_path = store.write_layout(None, entries)

    assert written_path == tmp_path / "prompts" / "layout.json"
    assert store.read_layout(None) == entries
    assert _no_staging_leftovers(tmp_path)


def test_layout_file_omits_defaults_and_keeps_unknown_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    layout_path = store.layout_path(None)
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    layout_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "note": "kept",
                "entries": [
                    {"id": "core:intro", "pinned": True},
                    {"id": "tool:bash", "enabled": True, "pinned": False},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert store.read_layout(None) == [
        LayoutEntry(id="core:intro", enabled=True, source=None),
        LayoutEntry(id="tool:bash"),
    ]
    store.write_layout(None, [LayoutEntry(id="core:intro", enabled=False)])

    assert json.loads(layout_path.read_text(encoding="utf-8")) == {
        "format_version": 1,
        "entries": [{"id": "core:intro", "enabled": False, "pinned": True}],
        "note": "kept",
    }


@pytest.mark.parametrize(
    ("scope", "body"),
    [
        (None, b"{not json"),
        ("assistant", b"\xff"),
        (None, b'[{"id":"core:intro"}]'),
        ("assistant", b'{"format_version":1}'),
        (None, b'{"format_version":2,"entries":[]}'),
        (None, b'{"format_version":1,"entries":[{"enabled": true}]}'),
        ("assistant", b'{"format_version":1,"entries":[{"id":"core:intro","enabled":"yes"}]}'),
        (None, b'{"format_version":1,"entries":[{"id":"core:intro","source":42}]}'),
        (None, b'{"format_version":1,"entries":[{"id":"core:intro","enabled":false},null]}'),
    ],
)
def test_invalid_layout_falls_back_and_is_only_replaced_by_a_reset(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, scope: str | None, body: bytes
) -> None:
    store = make_store(tmp_path)
    path = store.layout_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    assert store.read_layout(scope) == []
    assert any(record.levelname == "WARNING" for record in caplog.records)

    with pytest.raises(StorageError, match="Refusing to overwrite Prompt layout"):
        store.write_layout(scope, [LayoutEntry(id="core:intro")])
    with pytest.raises(StorageError, match="Reset the layout"):
        store.prune_layout(scope, [LayoutEntry(id="core:intro")], {"core:intro"})
    assert path.read_bytes() == body

    store.write_layout(scope, [LayoutEntry(id="core:intro")], reset=True)

    assert store.read_layout(scope) == [LayoutEntry(id="core:intro")]


def test_unreadable_layout_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = make_store(tmp_path)
    store.write_layout(None, [LayoutEntry(id="core:intro")])

    def fail_read(*args: object, **kwargs: object) -> str:
        raise PermissionError("test sentinel")

    monkeypatch.setattr(Path, "read_text", fail_read)
    assert store.read_layout(None) == []
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_prune_layout_drops_inert_entries_and_keeps_live_order_and_flags(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [
        LayoutEntry(id="tool:bash", enabled=False, source="tool"),
        LayoutEntry(id="tool:gone", enabled=True, source="tool"),
        LayoutEntry(id="core:intro", enabled=True, source="core"),
    ]

    store.prune_layout(None, entries, {"tool:bash", "core:intro"})
    assert store.read_layout(None) == [
        LayoutEntry(id="tool:bash", enabled=False, source="tool"),
        LayoutEntry(id="core:intro", enabled=True, source="core"),
    ]

    # An unknown entry is omitted, never an error.
    store.prune_layout(None, entries, set())
    assert store.read_layout(None) == []


# --------------------------------------------------------------------------
# per-block text overrides
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "relative_path"),
    [
        (None, "prompts/blocks/user/notes.md"),
        ("assistant", "agents/assistant/prompts/blocks/user/notes.md"),
    ],
)
def test_block_override_lifecycle(tmp_path: Path, scope: str | None, relative_path: str) -> None:
    store = make_store(tmp_path)
    assert store.read_block_override(scope, "user:notes") is None

    written_path = store.write_block_override(scope, "user:notes", "Always be terse.")

    assert written_path == tmp_path / relative_path
    assert written_path.read_text(encoding="utf-8") == "Always be terse."
    assert store.read_block_override(scope, "user:notes") == "Always be terse."
    assert _no_staging_leftovers(tmp_path)
    assert store.remove_block_override(scope, "user:notes") is True
    assert store.remove_block_override(scope, "user:notes") is False
    assert store.read_block_override(scope, "user:notes") is None


def test_case_variant_blocks_never_read_write_or_remove_each_others_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_block_override(None, "user:Notes", "upper")
    probe = tmp_path / "prompts" / "blocks" / "user" / "notes.md"

    if probe.exists():  # case-insensitive filesystem: one file would serve both ids
        with pytest.raises(StorageError):
            store.write_block_override(None, "user:notes", "lower")
        assert store.read_block_override(None, "user:notes") is None
        assert store.remove_block_override(None, "user:notes") is False
    else:  # case-sensitive: the variants keep separate files
        store.write_block_override(None, "user:notes", "lower")
        assert store.read_block_override(None, "user:notes") == "lower"
        assert store.remove_block_override(None, "user:notes") is True

    assert store.read_block_override(None, "user:Notes") == "upper"


# --------------------------------------------------------------------------
# agent-scope seeding
# --------------------------------------------------------------------------


def test_seed_agent_layout_copies_only_the_default_layout(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_block_override(None, "tool:bash", "default text override")
    default_layout = [
        LayoutEntry(id="core:intro", enabled=True, source="core"),
        LayoutEntry(id="tool:bash", enabled=False, source="tool"),
    ]

    written = store.seed_agent_layout("assistant", default_layout)

    assert written == tmp_path / "agents" / "assistant" / "prompts" / "layout.json"
    assert store.read_layout("assistant") == default_layout
    # The Agent inherits text until it overrides; seeding copies only the layout.
    assert store.read_block_override("assistant", "tool:bash") is None


def test_seed_agent_layout_keeps_an_existing_layout_unless_overwriting(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    existing = [LayoutEntry(id="user:custom", enabled=True, source="user")]
    store.write_layout("assistant", existing)
    new_default = [LayoutEntry(id="core:intro", enabled=True, source="core")]

    assert store.seed_agent_layout("assistant", new_default) is None
    assert store.read_layout("assistant") == existing

    store.seed_agent_layout("assistant", new_default, overwrite=True)
    assert store.read_layout("assistant") == new_default


def test_storage_manager_exposes_the_block_store(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    default_layout = [LayoutEntry(id="core:intro", source="core")]

    storage.seed_agent_block_layout("assistant", default_layout)
    storage.write_block_layout(None, [LayoutEntry(id="core:intro", enabled=False)])
    storage.prune_block_layout(
        None,
        [LayoutEntry(id="core:intro", source="core"), LayoutEntry(id="tool:gone", source="tool")],
        {"core:intro"},
    )
    storage.write_block_override(None, "tool:bash", "default override")

    assert storage.read_block_layout("assistant") == default_layout
    assert storage.read_block_layout(None) == default_layout
    assert storage.read_block_override(None, "tool:bash") == "default override"
    assert storage.remove_block_override(None, "tool:bash") is True
