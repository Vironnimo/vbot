"""Tests for the System Prompt block contract and the pure assembly engine."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.memory import MEMORY_PROMPT_MODE_AGENT_USER, MemoryPromptMode
from core.prompts import PromptAgent
from core.prompts.blocks import (
    BLOCK_KIND_DATA,
    BLOCK_KIND_TEXT,
    BlockDefinition,
    BlockProducer,
    BlockRenderContext,
    BlockSource,
    CallableOwnerActivity,
    LayoutEntry,
    MappingOverrideResolver,
    PromptError,
    ResolvedBlock,
    assemble_system_prompt,
    dedupe_definitions,
    expand_generated_markers,
    expand_workspace_includes,
    normalize_blocks,
    parse_block_source,
    passes_gates,
    resolve_block_text,
    resolve_layout,
    validate_workspace_include,
    wrap_include_file,
)


@dataclass(frozen=True)
class StubAgent:
    """Minimal agent satisfying the fields the engine reads."""

    id: str = "coder"
    name: str = "Coder Agent"
    model: str = "openai/gpt-5.2"
    workspace: str = ""
    thinking_effort: str | None = "high"
    memory_prompt_mode: MemoryPromptMode = MEMORY_PROMPT_MODE_AGENT_USER
    allowed_tools: list[str] = field(default_factory=lambda: ["*"])
    allowed_skills: list[str] = field(default_factory=lambda: ["*"])
    custom_system_prompt_enabled: bool = False


def _context(agent: StubAgent | None = None, *, scope: str = "default") -> BlockRenderContext:
    return BlockRenderContext(agent=agent or StubAgent(), scope=scope)


def _always_active() -> CallableOwnerActivity:
    return CallableOwnerActivity(lambda owner, agent: True)


def _text_block(
    block_id: str,
    text: str,
    *,
    owner: str = "always",
    default_rank: int = 0,
) -> BlockDefinition:
    return BlockDefinition(
        id=block_id,
        owner=owner,
        kind=BLOCK_KIND_TEXT,
        default_text=text,
        default_rank=default_rank,
    )


# --- Source / namespace parsing --------------------------------------------


@pytest.mark.parametrize(
    ("block_id", "expected"),
    [
        ("core:intro", "core"),
        ("tool:bash", "tool"),
        ("extension:my_ext", "extension"),
        ("user:my-rules", "user"),
        ("memory:guidance", "memory"),  # a domain prefix beyond the common four
        ("plugin:thing", "plugin"),  # open namespace: any non-empty prefix is valid
        ("tool:weird:name", "tool"),  # only the first ":" splits
    ],
)
def test_parse_block_source_returns_prefix(block_id: str, expected: str) -> None:
    assert parse_block_source(block_id) == expected


def test_parse_block_source_rejects_missing_prefix() -> None:
    with pytest.raises(PromptError, match="missing a source prefix"):
        parse_block_source("intro")


def test_parse_block_source_rejects_empty_prefix() -> None:
    with pytest.raises(PromptError, match="missing a source prefix"):
        parse_block_source(":guidance")


def test_block_source_members_carry_canonical_values() -> None:
    # The enum names the canonical sources for typed comparison without literals.
    assert BlockSource.CORE.value == "core"
    assert BlockSource.MEMORY.value == "memory"


# --- BlockDefinition contract ----------------------------------------------


def test_definition_exposes_source_and_editable_for_static_text() -> None:
    definition = _text_block("core:intro", "Hello")

    assert definition.source == "core"
    assert definition.editable is True


def test_dynamic_definition_is_not_editable() -> None:
    definition = BlockDefinition(id="tool:bash", owner="tool:bash", render=lambda ctx: "out")

    assert definition.editable is False
    assert definition.kind == BLOCK_KIND_TEXT  # default kind, but render makes it dynamic


def test_data_block_is_not_editable() -> None:
    definition = BlockDefinition(
        id="core:soul", owner="always", kind=BLOCK_KIND_DATA, default_text="data"
    )

    assert definition.editable is False


def test_definition_requires_exactly_one_of_text_or_render() -> None:
    with pytest.raises(PromptError, match="exactly one of default_text / render"):
        BlockDefinition(id="core:intro", owner="always")
    with pytest.raises(PromptError, match="exactly one of default_text / render"):
        BlockDefinition(id="core:intro", owner="always", default_text="x", render=lambda ctx: "y")


def test_definition_rejects_unprefixed_id_at_construction() -> None:
    with pytest.raises(PromptError, match="missing a source prefix"):
        _text_block("intro", "Hello")


# --- dedupe_definitions -----------------------------------------------------


def test_dedupe_keeps_first_and_diagnoses_collision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _text_block("core:intro", "first")
    # A later definition with the same id must lose; the diagnostic names both
    # the kept and the skipped owner so the collision is traceable.
    second = BlockDefinition(id="core:intro", owner="extension:dup", default_text="second")
    third = _text_block("tool:bash", "kept")

    with caplog.at_level(logging.WARNING):
        result = dedupe_definitions([first, second, third])

    assert result == [first, third]
    assert "already declared" in caplog.text
    assert "core:intro" in caplog.text
    assert "extension:dup" in caplog.text


# --- resolve_layout ---------------------------------------------------------


def test_layout_orders_by_explicit_entries() -> None:
    a = _text_block("core:a", "a")
    b = _text_block("core:b", "b")
    layout = [LayoutEntry(id="core:b"), LayoutEntry(id="core:a")]

    resolved = resolve_layout([a, b], layout)

    assert [block.definition.id for block in resolved] == ["core:b", "core:a"]
    assert all(block.enabled for block in resolved)


def test_layout_inserts_unknown_definition_at_default_rank() -> None:
    # core:b is laid out; core:a (rank 5) and core:c (rank 1) are not — they
    # append after the laid-out block in (rank, id) order.
    a = _text_block("core:a", "a", default_rank=5)
    b = _text_block("core:b", "b")
    c = _text_block("core:c", "c", default_rank=1)
    layout = [LayoutEntry(id="core:b")]

    resolved = resolve_layout([a, b, c], layout)

    assert [block.definition.id for block in resolved] == ["core:b", "core:c", "core:a"]
    assert resolved[1].enabled is True  # defaulted-in blocks are enabled


def test_layout_tiebreaks_equal_default_rank_by_id() -> None:
    high = _text_block("core:zeta", "z", default_rank=0)
    low = _text_block("core:alpha", "a", default_rank=0)

    resolved = resolve_layout([high, low], [])

    assert [block.definition.id for block in resolved] == ["core:alpha", "core:zeta"]


def test_layout_skips_inert_unknown_entry() -> None:
    a = _text_block("core:a", "a")
    # The layout remembers a contributor that is gone — skipped, never an error.
    layout = [LayoutEntry(id="core:gone"), LayoutEntry(id="core:a")]

    resolved = resolve_layout([a], layout)

    assert [block.definition.id for block in resolved] == ["core:a"]


def test_layout_keeps_disabled_flag() -> None:
    a = _text_block("core:a", "a")
    layout = [LayoutEntry(id="core:a", enabled=False)]

    resolved = resolve_layout([a], layout)

    assert resolved[0].enabled is False


def test_layout_ignores_duplicate_entry_for_same_id() -> None:
    a = _text_block("core:a", "a")
    layout = [LayoutEntry(id="core:a", enabled=False), LayoutEntry(id="core:a", enabled=True)]

    resolved = resolve_layout([a], layout)

    # Only the first slot wins; the duplicate is inert.
    assert [block.definition.id for block in resolved] == ["core:a"]
    assert resolved[0].enabled is False


# --- The three gates --------------------------------------------------------


def test_gate_user_enabled_blocks_a_disabled_block() -> None:
    block = ResolvedBlock(definition=_text_block("core:a", "text"), enabled=False)

    assert passes_gates(block, StubAgent(), _always_active(), "text") is False


def test_gate_owner_active_blocks_an_inactive_owner() -> None:
    block = ResolvedBlock(definition=_text_block("core:a", "text", owner="memory"), enabled=True)
    inactive = CallableOwnerActivity(lambda owner, agent: False)

    assert passes_gates(block, StubAgent(), inactive, "text") is False


def test_gate_non_empty_blocks_blank_text() -> None:
    block = ResolvedBlock(definition=_text_block("core:a", ""), enabled=True)

    assert passes_gates(block, StubAgent(), _always_active(), "   \n  ") is False


def test_all_three_gates_pass_renders_block() -> None:
    block = ResolvedBlock(definition=_text_block("core:a", "text"), enabled=True)

    assert passes_gates(block, StubAgent(), _always_active(), "text") is True


def test_owner_activity_receives_owner_and_agent() -> None:
    seen: list[tuple[str, str]] = []

    def predicate(owner: str, agent: PromptAgent) -> bool:
        seen.append((owner, agent.id))
        return True

    block = ResolvedBlock(
        definition=_text_block("tool:bash", "text", owner="tool:bash"), enabled=True
    )
    passes_gates(block, StubAgent(id="builder"), CallableOwnerActivity(predicate), "text")

    assert seen == [("tool:bash", "builder")]


# --- normalize_blocks -------------------------------------------------------


def test_normalize_joins_with_single_blank_line() -> None:
    assert normalize_blocks(["one", "two", "three"]) == "one\n\ntwo\n\nthree"


def test_normalize_trims_each_block_and_drops_empties() -> None:
    rendered = ["  leading", "", "   ", "trailing  \n"]

    assert normalize_blocks(rendered) == "leading\n\ntrailing"


def test_normalize_has_no_leading_or_trailing_blank_lines() -> None:
    result = normalize_blocks(["\n\nbody\n\n"])

    assert result == "body"
    assert not result.startswith("\n")
    assert not result.endswith("\n")


def test_normalize_empty_input_is_empty_string() -> None:
    assert normalize_blocks([]) == ""
    assert normalize_blocks(["", "  "]) == ""


# --- {generated:NAME} expansion --------------------------------------------


def test_generated_marker_expands_known_producer() -> None:
    producers = {"tool_list": lambda ctx: "- bash: run"}

    result = expand_generated_markers("Tools:\n{generated:tool_list}", producers, _context())

    assert result == "Tools:\n- bash: run"


def test_generated_marker_unknown_renders_empty_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = expand_generated_markers("a{generated:nope}b", {}, _context())

    assert result == "ab"
    assert "unknown generated marker" in caplog.text


def test_generated_marker_empty_producer_leaves_no_residue() -> None:
    producers = {"skill_list": lambda ctx: ""}

    result = expand_generated_markers("{generated:skill_list}", producers, _context())

    assert result == ""


def test_generated_producer_receives_context() -> None:
    seen: list[str] = []

    def producer(context: BlockRenderContext) -> str:
        seen.append(context.agent.id)
        return "ok"

    expand_generated_markers("{generated:x}", {"x": producer}, _context(StubAgent(id="builder")))

    assert seen == ["builder"]


# --- {include:filename} expansion ------------------------------------------


def test_include_wraps_existing_workspace_file(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text("Soul text", encoding="utf-8")

    result = expand_workspace_includes("{include:SOUL.md}", str(tmp_path))

    assert result == '<file name="SOUL.md">\nSoul text\n</file>'


def test_include_missing_file_is_dropped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = expand_workspace_includes("a{include:GONE.md}b", str(tmp_path))

    assert result == "ab"
    assert "Skipping missing workspace include" in caplog.text


def test_include_unreadable_file_is_dropped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A directory standing where the file is expected fails to read on every OS.
    (tmp_path / "SOUL.md").mkdir()

    with caplog.at_level(logging.WARNING):
        result = expand_workspace_includes("{include:SOUL.md}", str(tmp_path))

    assert result == ""
    assert "Skipping unreadable workspace include" in caplog.text


def test_include_empty_workspace_drops_without_read_or_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # An empty workspace ("" for a config agent) must NEVER resolve against
    # Path("") == Path(".") and read from the process CWD. A decoy proves it.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "SOUL.md").write_text("LEAKED", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = expand_workspace_includes("a{include:SOUL.md}b", "")

    assert result == "ab"
    assert "LEAKED" not in result
    assert "Skipping" not in caplog.text


def test_include_unsafe_path_raises(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="Unsafe workspace include"):
        expand_workspace_includes("{include:../secret.md}", str(tmp_path))


@pytest.mark.parametrize("filename", ["SOUL.md", "my-notes.txt", "notes.json"])
def test_validate_workspace_include_accepts_flat_names(filename: str) -> None:
    validate_workspace_include(filename)  # should not raise


@pytest.mark.parametrize("filename", ["../foo", "foo/bar", "/etc/passwd", "C:\\Windows\\cmd.exe"])
def test_validate_workspace_include_rejects_unsafe(filename: str) -> None:
    with pytest.raises(PromptError, match="Unsafe workspace include"):
        validate_workspace_include(filename)


def test_wrap_include_file_uses_canonical_frame() -> None:
    assert wrap_include_file("A.md", "body") == '<file name="A.md">\nbody\n</file>'


# --- resolve_block_text -----------------------------------------------------


def test_resolve_static_text_expands_generated_then_include(tmp_path: Path) -> None:
    (tmp_path / "EXTRA.md").write_text("extra", encoding="utf-8")
    block = ResolvedBlock(
        definition=_text_block("core:a", "{generated:x}\n{include:EXTRA.md}"), enabled=True
    )
    context = _context(StubAgent(workspace=str(tmp_path)))

    result = resolve_block_text(
        block,
        context,
        override_resolver=MappingOverrideResolver(),
        producers={"x": lambda ctx: "PRODUCED"},
    )

    assert result == 'PRODUCED\n<file name="EXTRA.md">\nextra\n</file>'


def test_resolve_static_text_prefers_override() -> None:
    block = ResolvedBlock(definition=_text_block("core:a", "default"), enabled=True)
    overrides = MappingOverrideResolver({("default", "core:a"): "OVERRIDDEN"})

    result = resolve_block_text(block, _context(), override_resolver=overrides, producers={})

    assert result == "OVERRIDDEN"


def test_resolve_data_block_is_verbatim_and_unexpanded() -> None:
    # A data block's text is inserted literally — its "{...}" is never interpreted.
    body = "Use {generated:x} and {include:SOUL.md} literally; also {custom}."
    block = ResolvedBlock(
        definition=BlockDefinition(
            id="core:agent_body", owner="always", kind=BLOCK_KIND_DATA, default_text=body
        ),
        enabled=True,
    )

    result = resolve_block_text(
        block,
        _context(),
        override_resolver=MappingOverrideResolver(),
        producers={"x": lambda ctx: "SHOULD-NOT-APPEAR"},
    )

    assert result == body


def test_resolve_dynamic_block_calls_render() -> None:
    block = ResolvedBlock(
        definition=BlockDefinition(
            id="tool:bash", owner="tool:bash", render=lambda ctx: f"dyn:{ctx.agent.id}"
        ),
        enabled=True,
    )

    result = resolve_block_text(
        block,
        _context(StubAgent(id="builder")),
        override_resolver=MappingOverrideResolver(),
        producers={},
    )

    assert result == "dyn:builder"


def test_resolve_dynamic_block_raise_is_isolated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(context: BlockRenderContext) -> str:
        raise RuntimeError("render exploded")

    block = ResolvedBlock(
        definition=BlockDefinition(id="tool:bash", owner="tool:bash", render=boom),
        enabled=True,
    )

    with caplog.at_level(logging.WARNING):
        result = resolve_block_text(
            block, _context(), override_resolver=MappingOverrideResolver(), producers={}
        )

    assert result == ""
    assert "Dropping dynamic block 'tool:bash'" in caplog.text
    assert "render exploded" in caplog.text


# --- assemble_system_prompt (end to end) ------------------------------------


def _assemble(
    definitions: list[BlockDefinition],
    layout: list[LayoutEntry],
    *,
    agent: StubAgent | None = None,
    owner_activity: CallableOwnerActivity | None = None,
    overrides: MappingOverrideResolver | None = None,
    producers: dict[str, BlockProducer] | None = None,
) -> str:
    return assemble_system_prompt(
        definitions,
        layout,
        _context(agent),
        owner_activity=owner_activity or _always_active(),
        override_resolver=overrides or MappingOverrideResolver(),
        producers=producers or {},
    )


def test_assemble_all_on_in_layout_order() -> None:
    definitions = [_text_block("core:a", "Alpha"), _text_block("core:b", "Beta")]
    layout = [LayoutEntry(id="core:b"), LayoutEntry(id="core:a")]

    assert _assemble(definitions, layout) == "Beta\n\nAlpha"


def test_assemble_skips_user_disabled_block() -> None:
    definitions = [_text_block("core:a", "Alpha"), _text_block("core:b", "Beta")]
    layout = [LayoutEntry(id="core:a", enabled=False), LayoutEntry(id="core:b")]

    assert _assemble(definitions, layout) == "Beta"


def test_assemble_skips_owner_inactive_block() -> None:
    definitions = [
        _text_block("core:a", "Always"),
        _text_block("memory:guidance", "Memory", owner="memory"),
    ]
    layout = [LayoutEntry(id="core:a"), LayoutEntry(id="memory:guidance")]
    owner_activity = CallableOwnerActivity(lambda owner, agent: owner != "memory")

    assert _assemble(definitions, layout, owner_activity=owner_activity) == "Always"


def test_assemble_collapses_empty_blocks_without_residue() -> None:
    definitions = [
        _text_block("core:a", "Alpha"),
        _text_block("core:empty", "{generated:nothing}"),
        _text_block("core:b", "Beta"),
    ]
    layout = [
        LayoutEntry(id="core:a"),
        LayoutEntry(id="core:empty"),
        LayoutEntry(id="core:b"),
    ]

    result = _assemble(definitions, layout, producers={"nothing": lambda ctx: ""})

    assert result == "Alpha\n\nBeta"


def test_assemble_unknown_marker_collapses_block_to_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    definitions = [
        _text_block("core:a", "Alpha"),
        _text_block("core:b", "{generated:missing}"),
    ]
    layout = [LayoutEntry(id="core:a"), LayoutEntry(id="core:b")]

    with caplog.at_level(logging.WARNING):
        result = _assemble(definitions, layout)

    assert result == "Alpha"
    assert "unknown generated marker" in caplog.text


def test_assemble_raising_dynamic_block_is_dropped() -> None:
    def boom(context: BlockRenderContext) -> str:
        raise RuntimeError("nope")

    definitions = [
        _text_block("core:a", "Alpha"),
        BlockDefinition(id="tool:bash", owner="tool:bash", render=boom),
    ]
    layout = [LayoutEntry(id="core:a"), LayoutEntry(id="tool:bash")]

    assert _assemble(definitions, layout) == "Alpha"


def test_assemble_inserts_verbatim_body_last_in_resolution() -> None:
    # A data block carrying placeholder-looking text is emitted verbatim, in its
    # laid-out position, never re-expanded.
    body = "I am {generated:tool_list} verbatim."
    definitions = [
        BlockDefinition(
            id="core:agent_body", owner="always", kind=BLOCK_KIND_DATA, default_text=body
        ),
        _text_block("core:b", "After"),
    ]
    layout = [LayoutEntry(id="core:agent_body"), LayoutEntry(id="core:b")]

    result = _assemble(definitions, layout, producers={"tool_list": lambda ctx: "EXPANDED"})

    assert result == f"{body}\n\nAfter"
    assert "EXPANDED" not in result


def test_assemble_is_deterministic_for_same_inputs() -> None:
    definitions = [_text_block("core:a", "A"), _text_block("core:b", "B")]
    layout = [LayoutEntry(id="core:a"), LayoutEntry(id="core:b")]

    first = _assemble(definitions, layout)
    second = _assemble(definitions, layout)

    assert first == second == "A\n\nB"


def test_assemble_defaults_in_block_absent_from_layout() -> None:
    # A newly added contributor with no layout entry appears (enabled) at its rank.
    definitions = [_text_block("core:a", "A"), _text_block("core:new", "New")]
    layout = [LayoutEntry(id="core:a")]

    assert _assemble(definitions, layout) == "A\n\nNew"
