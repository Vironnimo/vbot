"""Tests for the block-model prompt store (layout.json + per-block overrides)."""

import json
from pathlib import Path

import pytest

from core.prompts import LayoutEntry
from core.storage import StorageError, StorageManager
from core.storage.prompt_blocks import PromptBlockStore


def make_store(tmp_path: Path) -> PromptBlockStore:
    """Build a store with a real ensure-directories hook over a temp data dir."""

    def ensure_directories() -> None:
        for directory in (".tmp", "prompts", "agents"):
            (tmp_path / directory).mkdir(parents=True, exist_ok=True)

    return PromptBlockStore(data_dir=tmp_path, ensure_directories=ensure_directories)


# --------------------------------------------------------------------------
# id -> path mapping
# --------------------------------------------------------------------------


def test_block_override_path_never_contains_a_colon(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    tool_path = store.block_override_path(None, "tool:bash")
    user_path = store.block_override_path(None, "user:my-rules")

    assert ":" not in str(tool_path.relative_to(tmp_path))
    assert ":" not in str(user_path.relative_to(tmp_path))
    assert tool_path == tmp_path / "prompts" / "blocks" / "tool" / "bash.md"
    assert user_path == tmp_path / "prompts" / "blocks" / "user" / "my-rules.md"


def test_block_override_path_uses_agent_scope_root(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    path = store.block_override_path("assistant", "extension:weather")

    assert path == (
        tmp_path / "agents" / "assistant" / "prompts" / "blocks" / "extension" / "weather.md"
    )


@pytest.mark.parametrize(
    "block_id",
    [
        "tool:bash",
        "core:intro",
        "extension:weather",
        "user:my-rules",
        "user:my_rules",
        "memory:guidance",
        "user:Block1",
    ],
)
def test_split_block_id_accepts_valid_ids(tmp_path: Path, block_id: str) -> None:
    store = make_store(tmp_path)

    # Should not raise and should produce a path under the namespace folder.
    path = store.block_override_path(None, block_id)
    namespace, slug = block_id.split(":", 1)
    assert path.parent.name == namespace
    assert path.name == f"{slug}.md"


@pytest.mark.parametrize(
    "block_id",
    [
        "user:../escape",
        "user:../../etc/passwd",
        "tool:sub/dir",
        "tool:sub\\dir",
        "user:/absolute",
        "user:C:\\windows",
        "user:.",
        "user:..",
        "user:has space",
    ],
)
def test_split_block_id_rejects_unsafe_slugs(tmp_path: Path, block_id: str) -> None:
    store = make_store(tmp_path)

    with pytest.raises(StorageError):
        store.block_override_path(None, block_id)


def test_split_block_id_accepts_trailing_hyphen_per_agent_rule(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    # AGENT_ID_PATTERN only constrains the first char, so a trailing hyphen is a
    # valid slug; the path stays inside the namespace folder regardless.
    path = store.block_override_path(None, "user:trailing-")

    assert path == tmp_path / "prompts" / "blocks" / "user" / "trailing-.md"


def test_split_block_id_rejects_unknown_namespace(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(StorageError, match="Unknown block namespace"):
        store.block_override_path(None, "bogus:thing")


def test_split_block_id_rejects_missing_prefix(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(StorageError, match="missing a source prefix"):
        store.block_override_path(None, "noprefix")


def test_split_block_id_rejects_invalid_user_slug_via_agent_rule(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    # Leading hyphen and over-long slugs both fail the canonical agent-id rule
    # (AGENT_ID_PATTERN caps a slug at 64 chars: a lead char plus up to 63 more).
    with pytest.raises(StorageError, match="Unsafe block slug"):
        store.block_override_path(None, "user:-leading")
    with pytest.raises(StorageError, match="Unsafe block slug"):
        store.block_override_path(None, "user:" + "x" * 65)


def test_scope_root_rejects_unsafe_agent_id(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(StorageError, match="Unsafe agent id"):
        store.block_override_path("../escape", "tool:bash")


# --------------------------------------------------------------------------
# layout.json round-trip (atomic)
# --------------------------------------------------------------------------


def test_layout_round_trip_preserves_order_and_flags(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [
        LayoutEntry(id="core:intro", enabled=True, source="core"),
        LayoutEntry(id="tool:bash", enabled=False, source="tool"),
        LayoutEntry(id="user:my-rules", enabled=True, source="user"),
    ]

    written_path = store.write_layout(None, entries)
    read_back = store.read_layout(None)

    assert written_path == tmp_path / "prompts" / "layout.json"
    assert read_back == entries


def test_read_layout_missing_file_is_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    assert store.read_layout(None) == []
    assert store.read_layout("assistant") == []


def test_write_layout_omits_none_source_on_disk(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    store.write_layout(None, [LayoutEntry(id="core:intro")])

    raw = json.loads((tmp_path / "prompts" / "layout.json").read_text(encoding="utf-8"))
    assert raw == [{"id": "core:intro", "enabled": True}]


def test_read_layout_defaults_enabled_and_source(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    layout_path = tmp_path / "prompts" / "layout.json"
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    layout_path.write_text(json.dumps([{"id": "core:intro"}]), encoding="utf-8")

    [entry] = store.read_layout(None)

    assert entry == LayoutEntry(id="core:intro", enabled=True, source=None)


def test_read_layout_rejects_non_array(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    layout_path = tmp_path / "prompts" / "layout.json"
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    layout_path.write_text(json.dumps({"id": "core:intro"}), encoding="utf-8")

    with pytest.raises(StorageError, match="must be a JSON array"):
        store.read_layout(None)


def test_read_layout_rejects_invalid_json(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    layout_path = tmp_path / "prompts" / "layout.json"
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    layout_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(StorageError, match="Invalid layout JSON"):
        store.read_layout(None)


def test_read_layout_rejects_entry_without_id(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    layout_path = tmp_path / "prompts" / "layout.json"
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    layout_path.write_text(json.dumps([{"enabled": True}]), encoding="utf-8")

    with pytest.raises(StorageError, match="missing a string id"):
        store.read_layout(None)


def test_read_layout_rejects_non_boolean_enabled(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    layout_path = tmp_path / "prompts" / "layout.json"
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    layout_path.write_text(json.dumps([{"id": "core:intro", "enabled": "yes"}]), encoding="utf-8")

    with pytest.raises(StorageError, match="non-boolean enabled"):
        store.read_layout(None)


def test_write_layout_leaves_no_temp_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    store.write_layout(None, [LayoutEntry(id="core:intro")])

    assert list((tmp_path / ".tmp").iterdir()) == []


# --------------------------------------------------------------------------
# per-block override round-trip (atomic)
# --------------------------------------------------------------------------


def test_block_override_round_trip(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    written_path = store.write_block_override(None, "tool:bash", "Custom bash guidance.")

    assert written_path == tmp_path / "prompts" / "blocks" / "tool" / "bash.md"
    assert store.read_block_override(None, "tool:bash") == "Custom bash guidance."


def test_read_block_override_absent_is_none(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    assert store.read_block_override(None, "tool:bash") is None


def test_write_block_override_creates_namespace_subfolder(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    store.write_block_override("assistant", "user:notes", "agent notes")

    expected = tmp_path / "agents" / "assistant" / "prompts" / "blocks" / "user" / "notes.md"
    assert expected.read_text(encoding="utf-8") == "agent notes"


def test_remove_block_override_returns_true_then_false(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_block_override(None, "user:notes", "x")

    assert store.remove_block_override(None, "user:notes") is True
    assert store.remove_block_override(None, "user:notes") is False
    assert store.read_block_override(None, "user:notes") is None


def test_write_block_override_leaves_no_temp_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    store.write_block_override(None, "tool:bash", "x")

    assert list((tmp_path / ".tmp").iterdir()) == []


# --------------------------------------------------------------------------
# agent-scope seeding
# --------------------------------------------------------------------------


def test_seed_agent_layout_copies_default_layout(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    default_layout = [
        LayoutEntry(id="core:intro", enabled=True, source="core"),
        LayoutEntry(id="tool:bash", enabled=False, source="tool"),
    ]

    written = store.seed_agent_layout("assistant", default_layout)

    assert written == tmp_path / "agents" / "assistant" / "prompts" / "layout.json"
    assert store.read_layout("assistant") == default_layout


def test_seed_agent_layout_preserves_existing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    existing = [LayoutEntry(id="user:custom", enabled=True, source="user")]
    store.write_layout("assistant", existing)

    result = store.seed_agent_layout("assistant", [LayoutEntry(id="core:intro", source="core")])

    assert result is None
    assert store.read_layout("assistant") == existing


def test_seed_agent_layout_overwrite_replaces_existing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_layout("assistant", [LayoutEntry(id="user:custom", source="user")])
    new_default = [LayoutEntry(id="core:intro", enabled=True, source="core")]

    store.seed_agent_layout("assistant", new_default, overwrite=True)

    assert store.read_layout("assistant") == new_default


def test_seed_agent_layout_does_not_copy_text_overrides(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_block_override(None, "tool:bash", "default text override")

    store.seed_agent_layout("assistant", [LayoutEntry(id="tool:bash", source="tool")])

    # The agent inherits text until it overrides; seeding copies only the layout.
    assert store.read_block_override("assistant", "tool:bash") is None


# --------------------------------------------------------------------------
# inert-entry pruning on write
# --------------------------------------------------------------------------


def test_prune_layout_drops_inert_entries(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [
        LayoutEntry(id="core:intro", source="core"),
        LayoutEntry(id="tool:gone", source="tool"),
        LayoutEntry(id="tool:bash", source="tool"),
    ]
    known_ids = {"core:intro", "tool:bash"}

    store.prune_layout(None, entries, known_ids)

    assert [entry.id for entry in store.read_layout(None)] == ["core:intro", "tool:bash"]


def test_prune_layout_does_not_error_on_unknown_entry(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [LayoutEntry(id="tool:gone", source="tool")]

    # An unknown entry is omitted, never an error -> result is an empty layout.
    store.prune_layout(None, entries, set())

    assert store.read_layout(None) == []


def test_prune_layout_keeps_order_and_flags_of_live_entries(tmp_path: Path) -> None:
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


# --------------------------------------------------------------------------
# custom-block lifecycle (T1): file + layout effects
# --------------------------------------------------------------------------


def test_custom_block_create_writes_file_and_layout(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    # Create = write its override file + add a layout entry (the two halves T1
    # describes; the RPC layer orchestrates them, the store provides both).
    store.write_block_override(None, "user:house-rules", "Always be terse.")
    store.write_layout(None, [LayoutEntry(id="user:house-rules", source="user")])

    assert store.read_block_override(None, "user:house-rules") == "Always be terse."
    assert [entry.id for entry in store.read_layout(None)] == ["user:house-rules"]
    assert (tmp_path / "prompts" / "blocks" / "user" / "house-rules.md").exists()


def test_custom_block_remove_deletes_file_and_drops_layout_entry(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_block_override(None, "user:house-rules", "Always be terse.")
    store.write_layout(
        None,
        [
            LayoutEntry(id="user:house-rules", source="user"),
            LayoutEntry(id="core:intro", source="core"),
        ],
    )

    # Remove = delete the override file + drop the layout entry.
    removed = store.remove_block_override(None, "user:house-rules")
    remaining = [entry for entry in store.read_layout(None) if entry.id != "user:house-rules"]
    store.write_layout(None, remaining)

    assert removed is True
    assert store.read_block_override(None, "user:house-rules") is None
    assert not (tmp_path / "prompts" / "blocks" / "user" / "house-rules.md").exists()
    assert [entry.id for entry in store.read_layout(None)] == ["core:intro"]


# --------------------------------------------------------------------------
# StorageManager delegation (the integration seam Phase 3 consumes)
# --------------------------------------------------------------------------


def test_storage_manager_delegates_layout_round_trip(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    entries = [LayoutEntry(id="core:intro", enabled=False, source="core")]

    storage.write_block_layout(None, entries)

    assert storage.read_block_layout(None) == entries


def test_storage_manager_delegates_block_overrides(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.write_block_override(None, "tool:bash", "default override")

    assert storage.read_block_override(None, "tool:bash") == "default override"
    assert storage.remove_block_override(None, "tool:bash") is True


def test_storage_manager_delegates_seed_and_prune(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    default_layout = [LayoutEntry(id="core:intro", source="core")]

    storage.seed_agent_block_layout("assistant", default_layout)
    assert storage.read_block_layout("assistant") == default_layout

    storage.prune_block_layout(
        None,
        [LayoutEntry(id="core:intro", source="core"), LayoutEntry(id="tool:gone", source="tool")],
        {"core:intro"},
    )
    assert [entry.id for entry in storage.read_block_layout(None)] == ["core:intro"]
