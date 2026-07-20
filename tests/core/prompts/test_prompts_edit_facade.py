"""System Prompt block-edit facade and scope tests."""

from .prompts_test_support import (
    LayoutEntry,
    Path,
    PromptError,
    StubBlockStore,
    _agent,
    _facade_manager,
    pytest,
)


def test_list_blocks_returns_metadata_in_layout_order(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    blocks = manager.list_blocks()

    # Layout order follows the bundled default layout (resources/prompts/layout.json).
    assert [block["id"] for block in blocks] == [
        "core:soul",
        "memory:guidance",
        "core:runtime",
        "core:tools",
        "core:tools_list",
        "core:channels",
        "core:skills",
        "core:skill_maintenance",
        "core:agent_body",
        "core:project_files",
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
    # The opt-in tool list ships disabled but stays a normal editable text block.
    tools_list = by_id["core:tools_list"]
    assert tools_list["enabled"] is False
    assert tools_list["editable"] is True
    assert "text" in tools and tools["is_modified"] is False
    # The default scope omits the inheritance badge (T5 is agent-scope only).
    assert "inheritance" not in tools
    # A data block: non-editable, no text payload, channel-owner for channels.
    soul = by_id["core:soul"]
    assert soul["kind"] == "data"
    assert soul["editable"] is False
    assert "text" not in soul
    assert by_id["core:channels"]["owner"] == "channel"
    # The memory block ships under the memory source/owner.
    assert by_id["memory:guidance"]["source"] == "memory"
    assert by_id["memory:guidance"]["owner"] == "memory"


def test_list_blocks_agent_scope_carries_inheritance_flags(tmp_path: Path) -> None:
    # The agent owns an override on the tools block; the default scope overrides the
    # runtime block; the skills block is untouched → owner default.
    store = StubBlockStore(
        overrides={
            ("agent:coder", "core:tools"): "agent tools text",
            ("default", "core:runtime"): "default runtime override",
        }
    )
    agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    manager = _facade_manager(tmp_path, store=store, agents=[agent])

    blocks = {
        block["id"]: block for block in manager.list_blocks({"type": "agent", "agent_id": "coder"})
    }

    assert blocks["core:tools"]["inheritance"] == "agent_override"
    assert blocks["core:tools"]["text"] == "agent tools text"
    assert blocks["core:tools"]["is_modified"] is True
    assert blocks["core:runtime"]["inheritance"] == "default_override"
    assert blocks["core:runtime"]["is_modified"] is True
    assert blocks["core:skills"]["inheritance"] == "owner_default"
    assert blocks["core:skills"]["is_modified"] is False


def test_update_block_writes_override_and_returns_state(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    result = manager.update_block("core:tools", "## My Tools")

    assert store.read_block_override("default", "core:tools") == "## My Tools"
    assert result["id"] == "core:tools"
    assert result["text"] == "## My Tools"
    assert result["is_modified"] is True


def test_update_block_rejects_data_block(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    with pytest.raises(PromptError, match="not editable"):
        manager.update_block("core:soul", "nope")


def test_reset_block_removes_override_back_to_default(tmp_path: Path) -> None:
    store = StubBlockStore(overrides={("default", "core:tools"): "custom"})
    manager = _facade_manager(tmp_path, store=store)

    result = manager.reset_block("core:tools")

    assert store.read_block_override("default", "core:tools") is None
    assert result["is_modified"] is False


def test_reset_block_agent_scope_falls_back_to_inherited(tmp_path: Path) -> None:
    store = StubBlockStore(
        overrides={
            ("agent:coder", "core:tools"): "agent text",
            ("default", "core:tools"): "default text",
        }
    )
    agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    manager = _facade_manager(tmp_path, store=store, agents=[agent])

    result = manager.reset_block("core:tools", {"type": "agent", "agent_id": "coder"})

    # The agent override is gone; the effective text falls back to the inherited
    # default-scope override (T5 "reset → back to inherited").
    assert store.read_block_override("agent:coder", "core:tools") is None
    assert result["text"] == "default text"
    assert result["inheritance"] == "default_override"


def test_reset_block_rejects_user_block(tmp_path: Path) -> None:
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "my note"},
    )
    manager = _facade_manager(tmp_path, store=store)

    with pytest.raises(PromptError, match="no default to reset"):
        manager.reset_block("user:note")


def test_set_layout_persists_order_and_prunes_inert_id(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    result = manager.set_layout(
        [
            {"id": "core:skills", "enabled": False},
            {"id": "core:tools", "enabled": True},
            {"id": "extension:gone", "enabled": True},
        ]
    )

    # The contributor-gone id is pruned (tolerate-and-prune, never an error); the
    # live entries keep their order + toggles.
    persisted = store.read_layout("default")
    assert [entry.id for entry in persisted] == ["core:skills", "core:tools"]
    assert persisted[0].enabled is False
    assert [entry["id"] for entry in result["layout"]] == ["core:skills", "core:tools"]


def test_set_layout_keeps_existing_user_block(tmp_path: Path) -> None:
    # A custom block has no contributor definition, so set_layout must not prune it.
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "kept"},
    )
    manager = _facade_manager(tmp_path, store=store)

    result = manager.set_layout(
        [
            {"id": "user:note", "enabled": True},
            {"id": "core:tools", "enabled": True},
        ]
    )

    assert {entry["id"] for entry in result["layout"]} == {"user:note", "core:tools"}


def test_create_block_rejects_bad_slug(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    with pytest.raises(PromptError, match="invalid custom block slug"):
        manager.create_block("../etc/passwd")


def test_create_block_rejects_collision(tmp_path: Path) -> None:
    store = StubBlockStore(layouts={"default": [LayoutEntry(id="user:note", source="user")]})
    manager = _facade_manager(tmp_path, store=store)

    with pytest.raises(PromptError, match="already exists"):
        manager.create_block("note")


def test_create_block_writes_override_and_layout_entry(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    result = manager.create_block("greeting", "Hello.")

    assert store.read_block_override("default", "user:greeting") == "Hello."
    assert any(entry.id == "user:greeting" for entry in store.read_layout("default"))
    assert result["id"] == "user:greeting"
    assert result["owner"] == "always"
    assert result["kind"] == "text"
    assert result["editable"] is True


def test_created_empty_custom_block_is_returned_by_list_blocks(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    manager.create_block("empty-note")

    blocks = {block["id"]: block for block in manager.list_blocks()}
    custom = blocks["user:empty-note"]
    assert custom["source"] == "user"
    assert custom["kind"] == "text"
    assert custom["editable"] is True
    assert custom["enabled"] is True
    assert custom["text"] == ""


def test_create_block_inserts_at_requested_position(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    manager.create_block("first", "front", position=0)

    assert store.read_layout("default")[0].id == "user:first"


def test_remove_block_rejects_non_user_id(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    with pytest.raises(PromptError, match="only custom user blocks"):
        manager.remove_block("core:tools")


def test_remove_block_deletes_override_and_layout_entry(tmp_path: Path) -> None:
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "my note"},
    )
    manager = _facade_manager(tmp_path, store=store)

    result = manager.remove_block("user:note")

    assert store.read_block_override("default", "user:note") is None
    assert all(entry.id != "user:note" for entry in store.read_layout("default"))
    assert all(entry["id"] != "user:note" for entry in result["layout"])


def test_reset_layout_restores_bundled_default(tmp_path: Path) -> None:
    store = StubBlockStore(layouts={"default": [LayoutEntry(id="core:tools", enabled=False)]})
    manager = _facade_manager(tmp_path, store=store)

    result = manager.reset_layout()

    persisted = store.read_layout("default")
    # The bundled default layout is restored (the disabled-tools custom layout gone).
    assert [entry.id for entry in persisted] == [
        "core:soul",
        "memory:guidance",
        "core:runtime",
        "core:tools",
        "tool:subagent",
        "core:tools_list",
        "core:channels",
        "core:skills",
        "core:skill_maintenance",
        "core:agent_body",
        "core:project_files",
    ]
    # Everything ships enabled except the opt-in tool list.
    disabled_ids = {entry.id for entry in persisted if not entry.enabled}
    assert disabled_ids == {"core:tools_list"}
    assert [entry["id"] for entry in result["layout"]] == [entry.id for entry in persisted]


def test_list_scopes_includes_enabled_agent_scopes(tmp_path: Path) -> None:
    enabled = _agent(tmp_path, custom_system_prompt_enabled=True)
    disabled = _agent(tmp_path, agent_id="plain", custom_system_prompt_enabled=False)
    manager = _facade_manager(tmp_path, agents=[disabled, enabled])

    # A scope with no saved layout or override reports has_customizations=False.
    assert manager.list_scopes() == [
        {"type": "default", "label": "Default"},
        {
            "type": "agent",
            "agent_id": "coder",
            "label": "Coder Agent",
            "has_customizations": False,
        },
    ]


def test_list_scopes_flags_agent_scope_with_saved_layout(tmp_path: Path) -> None:
    enabled = _agent(tmp_path, custom_system_prompt_enabled=True)
    store = StubBlockStore(layouts={"agent:coder": [LayoutEntry(id="core:tools", enabled=False)]})
    manager = _facade_manager(tmp_path, store=store, agents=[enabled])

    agent_scope = next(scope for scope in manager.list_scopes() if scope["type"] == "agent")
    assert agent_scope["has_customizations"] is True


def test_list_scopes_flags_agent_scope_with_block_override(tmp_path: Path) -> None:
    enabled = _agent(tmp_path, custom_system_prompt_enabled=True)
    store = StubBlockStore(overrides={("agent:coder", "core:tools"): "agent text"})
    manager = _facade_manager(tmp_path, store=store, agents=[enabled])

    agent_scope = next(scope for scope in manager.list_scopes() if scope["type"] == "agent")
    assert agent_scope["has_customizations"] is True


def test_list_scopes_ignores_default_scope_override_for_agent_flag(tmp_path: Path) -> None:
    # A default-scope override is not the agent scope's own customization, so it
    # must not flag the agent scope.
    enabled = _agent(tmp_path, custom_system_prompt_enabled=True)
    store = StubBlockStore(overrides={("default", "core:tools"): "default text"})
    manager = _facade_manager(tmp_path, store=store, agents=[enabled])

    agent_scope = next(scope for scope in manager.list_scopes() if scope["type"] == "agent")
    assert agent_scope["has_customizations"] is False


def test_edit_facade_rejects_disabled_agent_scope(tmp_path: Path) -> None:
    disabled = _agent(tmp_path, custom_system_prompt_enabled=False)
    manager = _facade_manager(tmp_path, agents=[disabled])

    with pytest.raises(PromptError, match="not enabled"):
        manager.list_blocks({"type": "agent", "agent_id": "coder"})
