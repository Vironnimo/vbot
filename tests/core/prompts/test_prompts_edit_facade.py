"""System Prompt block-edit facade and scope tests."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.prompts.blocks import LayoutEntry
from core.prompts.prompts import PromptError, SystemPromptManager
from core.runtime._prompt_blocks import _StorageManagerBlockStore
from core.storage import StorageError, StorageManager
from tests.core.prompts.prompts_test_support import StubBlockStore, _agent, _facade_manager

_AGENT_SCOPE = {"type": "agent", "agent_id": "coder"}
_BUNDLED_LAYOUT = [
    "core:soul",
    "memory:guidance",
    "core:runtime",
    "core:identity_runtime",
    "core:tools",
    "core:system_reminders",
    "core:subagent_role",
    "tool:project",
    "tool:subagent",
    "core:tools_list",
    "tool:load_tools",
    "core:channels",
    "core:skills",
    "core:skill_maintenance",
    "core:agent_body",
    "core:working_project",
]


def _user_note_store(**layouts: list[LayoutEntry]) -> StubBlockStore:
    return StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")], **layouts},
        overrides={("default", "user:note"): "my note"},
    )


def test_list_blocks_returns_metadata_in_layout_order(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    blocks = manager.list_blocks()

    # Layout order follows the bundled default layout; its tool:* entries have no
    # registered definition here, so they are inert.
    assert [block["id"] for block in blocks] == [
        block_id for block_id in _BUNDLED_LAYOUT if not block_id.startswith("tool:")
    ]
    # Ranks are the layout positions, 0-based and contiguous.
    assert [block["rank"] for block in blocks] == list(range(len(blocks)))
    by_id = {block["id"]: block for block in blocks}
    # A core text block: editable, source core, owner always, carries its text.
    tools = by_id["core:tools"]
    assert tools["kind"] == "text"
    assert tools["editable"] is True
    assert tools["source"] == "core"
    assert tools["owner"] == "always"
    assert tools["enabled"] is True
    assert "text" in tools and tools["is_modified"] is False
    # The default scope omits the inheritance badge (agent scope only).
    assert "inheritance" not in tools
    # The opt-in tool list ships disabled but stays a normal editable text block.
    assert by_id["core:tools_list"]["enabled"] is False
    assert by_id["core:tools_list"]["editable"] is True
    # Skill maintenance is an editable core text block owned by its Tool.
    maintenance = by_id["core:skill_maintenance"]
    assert (maintenance["source"], maintenance["owner"]) == ("core", "tool:skill_manage")
    assert maintenance["editable"] is True
    # A data block: non-editable, no text payload.
    soul = by_id["core:soul"]
    assert soul["kind"] == "data"
    assert soul["editable"] is False
    assert "text" not in soul
    assert by_id["core:channels"]["owner"] == "channel"
    assert by_id["core:identity_runtime"]["owner"] == "identity"
    assert by_id["core:identity_runtime"]["editable"] is True
    assert by_id["core:working_project"]["editable"] is False
    assert by_id["memory:guidance"]["source"] == "memory"
    assert by_id["memory:guidance"]["owner"] == "memory"


def test_empty_layout_retains_defaults_and_explicit_disable_removes_blocks(
    tmp_path: Path,
) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)
    agent = _agent(tmp_path)
    default_prompt = manager.build_system_prompt(agent)
    manager.set_layout([])
    assert store.read_layout("default") == []
    assert manager.build_system_prompt(agent) == default_prompt
    manager.set_layout([{"id": block["id"], "enabled": False} for block in manager.list_blocks()])
    assert manager.build_system_prompt(agent) == ""


def test_list_blocks_agent_scope_carries_inheritance_flags(tmp_path: Path) -> None:
    store = StubBlockStore(
        overrides={
            ("agent:coder", "core:tools"): "agent tools text",
            ("default", "core:runtime"): "default runtime override",
        }
    )
    agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    manager = _facade_manager(tmp_path, store=store, agents=[agent])

    blocks = {block["id"]: block for block in manager.list_blocks(_AGENT_SCOPE)}

    assert blocks["core:tools"]["inheritance"] == "agent_override"
    assert blocks["core:tools"]["text"] == "agent tools text"
    assert blocks["core:tools"]["is_modified"] is True
    assert blocks["core:runtime"]["inheritance"] == "default_override"
    assert blocks["core:runtime"]["is_modified"] is True
    assert blocks["core:skills"]["inheritance"] == "owner_default"
    assert blocks["core:skills"]["is_modified"] is False


def test_update_block_writes_an_override_that_reset_block_removes(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    updated = manager.update_block("core:tools", "## My Tools")

    assert store.read_block_override("default", "core:tools") == "## My Tools"
    assert (updated["id"], updated["text"], updated["is_modified"]) == (
        "core:tools",
        "## My Tools",
        True,
    )

    reset = manager.reset_block("core:tools")

    assert store.read_block_override("default", "core:tools") is None
    assert reset["is_modified"] is False


def test_reset_block_agent_scope_falls_back_to_inherited(tmp_path: Path) -> None:
    store = StubBlockStore(
        overrides={
            ("agent:coder", "core:tools"): "agent text",
            ("default", "core:tools"): "default text",
        }
    )
    agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    manager = _facade_manager(tmp_path, store=store, agents=[agent])

    result = manager.reset_block("core:tools", _AGENT_SCOPE)

    assert store.read_block_override("agent:coder", "core:tools") is None
    assert result["text"] == "default text"
    assert result["inheritance"] == "default_override"


@pytest.mark.parametrize(
    "edit",
    [
        lambda manager: manager.update_block("core:soul", "nope"),
        lambda manager: manager.reset_block("user:note"),
        lambda manager: manager.create_block("../etc/passwd"),
        lambda manager: manager.create_block("note"),
        lambda manager: manager.create_block("Note"),
        lambda manager: manager.create_block("nul"),
        lambda manager: manager.remove_block("core:tools"),
        lambda manager: manager.list_blocks({"type": "agent", "agent_id": "plain"}),
    ],
    ids=[
        "update-data-block",
        "reset-user-block",
        "create-bad-slug",
        "create-collision",
        "create-case-variant-collision",
        "create-windows-reserved-slug",
        "remove-core-block",
        "custom-prompt-disabled-agent",
    ],
)
def test_edit_facade_rejects_an_invalid_edit(
    tmp_path: Path, edit: Callable[[SystemPromptManager], Any]
) -> None:
    plain = _agent(tmp_path, agent_id="plain", custom_system_prompt_enabled=False)
    manager = _facade_manager(tmp_path, store=_user_note_store(), agents=[plain])

    with pytest.raises(PromptError):
        edit(manager)


def test_set_layout_persists_order_keeps_user_blocks_and_prunes_inert_ids(
    tmp_path: Path,
) -> None:
    store = _user_note_store()
    manager = _facade_manager(tmp_path, store=store)

    result = manager.set_layout(
        [
            {"id": "core:skills", "enabled": False},
            {"id": "user:note", "enabled": True},
            {"id": "core:tools", "enabled": True},
            {"id": "extension:gone", "enabled": True},
        ]
    )

    # A contributor-gone id is pruned, never an error; a custom block has no
    # contributor definition and is kept.
    persisted = store.read_layout("default")
    assert [(entry.id, entry.enabled) for entry in persisted] == [
        ("core:skills", False),
        ("user:note", True),
        ("core:tools", True),
    ]
    assert [entry["id"] for entry in result["layout"]] == [entry.id for entry in persisted]


def test_layouts_holding_case_variant_custom_blocks_keep_every_entry(tmp_path: Path) -> None:
    # Only creation rejects a case variant; a layout that already holds both spellings
    # is never rewritten or pruned, and each entry can still be reordered and removed.
    store = StubBlockStore(
        layouts={
            "default": [
                LayoutEntry(id="user:Note", source="user"),
                LayoutEntry(id="user:note", source="user"),
            ]
        }
    )
    manager = _facade_manager(tmp_path, store=store)

    listed = [block["id"] for block in manager.list_blocks() if block["id"].startswith("user:")]
    manager.set_layout(
        [{"id": "user:note", "enabled": False}, {"id": "user:Note", "enabled": True}]
    )

    assert listed == ["user:Note", "user:note"]
    assert [(entry.id, entry.enabled) for entry in store.read_layout("default")] == [
        ("user:note", False),
        ("user:Note", True),
    ]
    manager.remove_block("user:note")
    assert [entry.id for entry in store.read_layout("default")] == ["user:Note"]


def test_create_and_remove_manage_custom_user_blocks(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    created = manager.create_block("greeting", "Hello.")
    manager.create_block("first", position=0)

    assert store.read_block_override("default", "user:greeting") == "Hello."
    assert store.read_layout("default")[0].id == "user:first"
    assert (created["id"], created["owner"], created["kind"], created["editable"]) == (
        "user:greeting",
        "always",
        "text",
        True,
    )
    custom = {block["id"]: block for block in manager.list_blocks()}["user:first"]
    assert (custom["source"], custom["kind"], custom["editable"], custom["enabled"]) == (
        "user",
        "text",
        True,
        True,
    )
    assert custom["text"] == ""

    removed = manager.remove_block("user:greeting")

    assert store.read_block_override("default", "user:greeting") is None
    assert all(entry.id != "user:greeting" for entry in store.read_layout("default"))
    assert all(entry["id"] != "user:greeting" for entry in removed["layout"])


def test_reset_layout_restores_bundled_default(tmp_path: Path) -> None:
    store = StubBlockStore(layouts={"default": [LayoutEntry(id="core:tools", enabled=False)]})
    manager = _facade_manager(tmp_path, store=store)

    result = manager.reset_layout()

    persisted = store.read_layout("default")
    assert [entry.id for entry in persisted] == _BUNDLED_LAYOUT
    # Everything ships enabled except the opt-in tool list.
    assert {entry.id for entry in persisted if not entry.enabled} == {"core:tools_list"}
    assert [entry["id"] for entry in result["layout"]] == _BUNDLED_LAYOUT


@pytest.mark.parametrize(
    ("store", "has_customizations"),
    [
        (StubBlockStore(), False),
        (StubBlockStore(layouts={"agent:coder": [LayoutEntry(id="core:tools")]}), True),
        (StubBlockStore(overrides={("agent:coder", "core:tools"): "agent text"}), True),
        # A default-scope override is not the Agent scope's own customization.
        (StubBlockStore(overrides={("default", "core:tools"): "default text"}), False),
    ],
    ids=["untouched", "agent-layout", "agent-override", "default-override"],
)
def test_list_scopes_lists_custom_prompt_agents_with_their_customization_flag(
    tmp_path: Path, store: StubBlockStore, has_customizations: bool
) -> None:
    enabled = _agent(tmp_path, custom_system_prompt_enabled=True)
    disabled = _agent(tmp_path, agent_id="plain", custom_system_prompt_enabled=False)
    manager = _facade_manager(tmp_path, store=store, agents=[disabled, enabled])

    assert manager.list_scopes() == [
        {"type": "default", "label": "Default"},
        {
            "type": "agent",
            "agent_id": "coder",
            "label": "Coder Agent",
            "has_customizations": has_customizations,
        },
    ]


@pytest.mark.parametrize(
    ("scope", "body"),
    [
        (None, b"{broken"),
        ("coder", b'{"format_version":1,"entries":[{"id":"core:tools","enabled":"false"}]}'),
    ],
    ids=["default-invalid-json", "agent-invalid-entry"],
)
def test_corrupt_persisted_layout_builds_with_defaults_until_it_is_reset(
    tmp_path: Path, scope: str | None, body: bytes
) -> None:
    # Storage owns the validation cases; the manager must neither apply a corrupt
    # layout partially nor overwrite it without an explicit reset.
    storage = StorageManager(data_dir=tmp_path / "data")
    agent = _agent(tmp_path, custom_system_prompt_enabled=scope is not None)
    manager = _facade_manager(tmp_path, store=_StorageManagerBlockStore(storage), agents=[agent])
    edit_scope = None if scope is None else _AGENT_SCOPE
    baseline = manager.build_system_prompt(agent)
    path = (
        storage.data_dir / "prompts" / "layout.json"
        if scope is None
        else storage.data_dir / "agents" / scope / "prompts" / "layout.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)

    assert manager.build_system_prompt(agent) == baseline
    with pytest.raises(StorageError, match="Reset the layout of this prompt scope"):
        manager.set_layout([{"id": "core:tools", "enabled": False}], edit_scope)
    assert path.read_bytes() == body

    result = manager.reset_layout(edit_scope)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["format_version"] == 1
    assert [entry["id"] for entry in document["entries"]] == [
        entry["id"] for entry in result["layout"]
    ]
