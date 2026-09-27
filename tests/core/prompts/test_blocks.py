"""Tests for the System Prompt block contract and the pure assembly engine."""

import logging
from collections.abc import Callable
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
    CallableOwnerActivity,
    LayoutEntry,
    MappingOverrideResolver,
    PromptError,
    ResolvedBlock,
    apply_replacements,
    assemble_system_prompt,
    dedupe_definitions,
    expand_block_template,
    expand_workspace_includes,
    normalize_blocks,
    parse_block_source,
    passes_gates,
    resolve_block_text,
    resolve_layout,
    validate_workspace_include,
    wrap_include_file,
)
from core.tools.availability import ToolAccess

# Every template marker kind, including an unsafe include that would fail the build if
# it were ever expanded.
_MARKERS = "{include:../secret.md} {include:USER.md} {generated:x} {model} {data_root}"


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
    tools: dict[str, object] = field(default_factory=dict)
    custom_system_prompt_enabled: bool = False

    @property
    def tool_access(self) -> ToolAccess:
        if "*" in self.allowed_tools:
            return ToolAccess(mode="all")
        return ToolAccess(mode="selected", allowed=tuple(self.allowed_tools))


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


def _raise(message: str) -> Callable[[BlockRenderContext], str]:
    def render(_context: BlockRenderContext) -> str:
        raise RuntimeError(message)

    return render


# --- Block ids and definitions ----------------------------------------------


@pytest.mark.parametrize(
    ("block_id", "expected"),
    [
        ("core:intro", "core"),
        ("memory:guidance", "memory"),  # a domain prefix beyond the common four
        ("plugin:thing", "plugin"),  # open namespace: any non-empty prefix is valid
        ("tool:weird:name", "tool"),  # only the first ":" splits
        ("intro", None),
        (":guidance", None),
    ],
)
def test_parse_block_source_returns_the_non_empty_prefix(
    block_id: str, expected: str | None
) -> None:
    if expected is None:
        with pytest.raises(PromptError):
            parse_block_source(block_id)
    else:
        assert parse_block_source(block_id) == expected


@pytest.mark.parametrize(
    ("definition", "editable"),
    [
        (lambda: _text_block("core:intro", "Hello"), True),
        (lambda: BlockDefinition(id="core:intro", owner="always", render=lambda ctx: "out"), False),
        (
            lambda: BlockDefinition(
                id="core:intro", owner="always", kind=BLOCK_KIND_DATA, default_text="data"
            ),
            False,
        ),
    ],
    ids=["static-text", "dynamic", "data"],
)
def test_only_static_text_definitions_are_editable(
    definition: Callable[[], BlockDefinition], editable: bool
) -> None:
    built = definition()

    assert built.source == "core"
    assert built.editable is editable


@pytest.mark.parametrize(
    "build",
    [
        lambda: BlockDefinition(id="core:intro", owner="always"),
        lambda: BlockDefinition(
            id="core:intro", owner="always", default_text="x", render=lambda ctx: "y"
        ),
        lambda: _text_block("intro", "Hello"),
    ],
    ids=["neither-text-nor-render", "text-and-render", "unprefixed-id"],
)
def test_definition_rejects_an_invalid_shape(build: Callable[[], BlockDefinition]) -> None:
    with pytest.raises(PromptError):
        build()


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
    assert "core:intro" in caplog.text
    assert "extension:dup" in caplog.text


# --- resolve_layout ---------------------------------------------------------

_A = _text_block("core:a", "a")
_OPT_IN = BlockDefinition(id="core:opt-in", owner="always", default_text="x", default_enabled=False)


@pytest.mark.parametrize(
    ("definitions", "layout", "expected"),
    [
        (
            [_A, _text_block("core:b", "b")],
            [LayoutEntry(id="core:b"), LayoutEntry(id="core:a")],
            [("core:b", True), ("core:a", True)],
        ),
        # Unlisted definitions append after the laid-out ones in (rank, id) order.
        (
            [
                _text_block("core:a", "a", default_rank=5),
                _text_block("core:b", "b"),
                _text_block("core:c", "c", default_rank=1),
                _text_block("core:zeta", "z", default_rank=1),
            ],
            [LayoutEntry(id="core:b")],
            [("core:b", True), ("core:c", True), ("core:zeta", True), ("core:a", True)],
        ),
        # A remembered contributor that is gone is skipped, never an error.
        ([_A], [LayoutEntry(id="core:gone"), LayoutEntry(id="core:a")], [("core:a", True)]),
        # Only the first entry for an id counts; its disabled flag is kept.
        (
            [_A],
            [LayoutEntry(id="core:a", enabled=False), LayoutEntry(id="core:a", enabled=True)],
            [("core:a", False)],
        ),
        # An opt-in block defaults in off, even under an older persisted layout ...
        ([_OPT_IN], [], [("core:opt-in", False)]),
        # ... and an explicit layout entry wins over that default.
        ([_OPT_IN], [LayoutEntry(id="core:opt-in", enabled=True)], [("core:opt-in", True)]),
    ],
    ids=[
        "layout-order",
        "unlisted-by-rank-then-id",
        "inert-entry",
        "duplicate-entry",
        "opt-in-default",
        "opt-in-enabled",
    ],
)
def test_resolve_layout_orders_and_enables_blocks(
    definitions: list[BlockDefinition],
    layout: list[LayoutEntry],
    expected: list[tuple[str, bool]],
) -> None:
    resolved = resolve_layout(definitions, layout)

    assert [(block.definition.id, block.enabled) for block in resolved] == expected


# --- Gates and normalization ------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "owner_active", "text", "expected"),
    [
        (False, True, "text", False),
        (True, False, "text", False),
        (True, True, "   \n  ", False),
        (True, True, "text", True),
    ],
    ids=["user-disabled", "owner-inactive", "blank-text", "all-gates-pass"],
)
def test_block_renders_only_when_all_three_gates_pass(
    enabled: bool, owner_active: bool, text: str, expected: bool
) -> None:
    seen: list[tuple[str, str]] = []

    def predicate(owner: str, agent: PromptAgent) -> bool:
        seen.append((owner, agent.id))
        return owner_active

    block = ResolvedBlock(
        definition=_text_block("tool:bash", "text", owner="tool:bash"), enabled=enabled
    )

    assert (
        passes_gates(block, StubAgent(id="builder"), CallableOwnerActivity(predicate), text)
        is expected
    )
    assert seen == ([("tool:bash", "builder")] if enabled else [])


@pytest.mark.parametrize(
    ("rendered", "expected"),
    [
        (["one", "two", "three"], "one\n\ntwo\n\nthree"),
        (["  leading", "", "   ", "trailing  \n"], "leading\n\ntrailing"),
        (["\n\nbody\n\n"], "body"),
        (["", "  "], ""),
        ([], ""),
    ],
)
def test_normalize_trims_blocks_and_joins_them_with_one_blank_line(
    rendered: list[str], expected: str
) -> None:
    assert normalize_blocks(rendered) == expected


# --- Template markers -------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "producers", "expected", "warnings"),
    [
        (
            "Tools:\n{generated:tool_list}",
            {"tool_list": lambda ctx: f"- bash: run for {ctx.agent.id}"},
            "Tools:\n- bash: run for builder",
            [],
        ),
        ("{generated:skill_catalog}", {"skill_catalog": lambda ctx: ""}, "", []),
        ("a{generated:nope}b", {}, "ab", ["nope"]),
        (
            "before{generated:broken}after",
            {"broken": _raise("producer broke")},
            "beforeafter",
            ["broken", "producer broke"],
        ),
    ],
    ids=["known-producer", "empty-producer", "unknown-producer", "failing-producer"],
)
def test_generated_marker_renders_its_producer_or_warns_and_drops(
    template: str,
    producers: dict[str, BlockProducer],
    expected: str,
    warnings: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = expand_block_template(
            template, _context(StubAgent(id="builder")), producers=producers
        )

    assert result == expected
    assert all(fragment in caplog.text for fragment in warnings)
    assert bool(caplog.records) is bool(warnings)


@pytest.mark.parametrize(
    ("setup", "template", "expected", "warns"),
    [
        ("file", "{include:SOUL.md}", '<file name="SOUL.md">\nSoul text\n</file>', False),
        ("missing", "a{include:SOUL.md}b", "ab", True),
        ("directory", "a{include:SOUL.md}b", "ab", True),
        # An empty workspace never resolves against the process CWD, which holds a decoy.
        ("empty-workspace", "a{include:SOUL.md}b", "ab", False),
    ],
)
def test_include_inlines_a_readable_workspace_file_and_reports_only_that_read(
    setup: str,
    template: str,
    expected: str,
    warns: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    soul = tmp_path / "SOUL.md"
    workspace = str(tmp_path)
    if setup == "file":
        soul.write_text("Soul text", encoding="utf-8")
    elif setup == "directory":
        soul.mkdir()  # fails to read on every OS
    elif setup == "empty-workspace":
        monkeypatch.chdir(tmp_path)
        soul.write_text("LEAKED", encoding="utf-8")
        workspace = ""
    seen: list[Path] = []

    with caplog.at_level(logging.WARNING):
        result = expand_workspace_includes(template, workspace, on_read=seen.append)

    assert result == expected
    assert any(record.levelno == logging.WARNING for record in caplog.records) is warns
    # Only content that reached the prompt is reported, so the chat loop can stamp it read.
    assert seen == ([soul.resolve()] if setup == "file" else [])


@pytest.mark.parametrize(
    ("filename", "safe"),
    [
        ("SOUL.md", True),
        ("my-notes.txt", True),
        ("../foo", False),
        ("foo/bar", False),
        ("/etc/passwd", False),
        ("C:\\Windows\\cmd.exe", False),
    ],
)
def test_validate_workspace_include_accepts_only_flat_names(filename: str, safe: bool) -> None:
    if safe:
        validate_workspace_include(filename)
    else:
        with pytest.raises(PromptError):
            validate_workspace_include(filename)


def test_apply_replacements_is_exact_and_non_recursive() -> None:
    text = "Known {first}; second {second}; unknown {other}."

    result = apply_replacements(text, {"{first}": "{second}", "{second}": "resolved"})

    assert result == "Known {second}; second resolved; unknown {other}."


@pytest.mark.parametrize(
    ("template", "producers", "replacements", "expected", "reads"),
    [
        # Producer output (Memory entries, Skill descriptions, the Tool list) is data.
        (
            "<memory>{generated:memory_files}</memory> {model}",
            {"memory_files": lambda ctx: f"- entry {_MARKERS}", "x": lambda ctx: "PRODUCED"},
            {"{model}": "openai/gpt-5.2", "{data_root}": "/data"},
            f"<memory>- entry {_MARKERS}</memory> openai/gpt-5.2",
            [],
        ),
        # Replacement values are data.
        (
            "{model} | {generated:x}",
            {"x": lambda ctx: "{model}"},
            {"{model}": _MARKERS},
            f"{_MARKERS} | {{model}}",
            [],
        ),
        # Included workspace content is data; only the template's own include is read.
        (
            "{include:EXTRA.md}",
            {"x": lambda ctx: "PRODUCED"},
            {"{model}": "openai/gpt-5.2", "{data_root}": "/data"},
            wrap_include_file("EXTRA.md", _MARKERS),
            ["EXTRA.md"],
        ),
    ],
    ids=["producer-output", "replacement-value", "included-file"],
)
def test_template_expansion_inserts_expanded_values_verbatim(
    template: str,
    producers: dict[str, BlockProducer],
    replacements: dict[str, str],
    expected: str,
    reads: list[str],
    tmp_path: Path,
) -> None:
    (tmp_path / "EXTRA.md").write_text(_MARKERS, encoding="utf-8")
    (tmp_path / "USER.md").write_text("PRIVATE-USER-FILE", encoding="utf-8")
    seen: list[Path] = []
    context = BlockRenderContext(
        agent=StubAgent(workspace=str(tmp_path)), scope="default", read_observer=seen.append
    )

    result = expand_block_template(
        template, context, producers=producers, replacements=replacements
    )

    assert result == expected
    assert seen == [(tmp_path / name).resolve() for name in reads]


def test_template_expansion_expands_every_marker_of_the_template_itself(tmp_path: Path) -> None:
    (tmp_path / "EXTRA.md").write_text("extra", encoding="utf-8")
    context = _context(StubAgent(workspace=str(tmp_path)))

    result = expand_block_template(
        "{generated:x} {include:EXTRA.md} {model} {unknown}",
        context,
        producers={"x": lambda ctx: "PRODUCED"},
        replacements={"{model}": "openai/gpt-5.2"},
    )

    assert result == 'PRODUCED <file name="EXTRA.md">\nextra\n</file> openai/gpt-5.2 {unknown}'
    with pytest.raises(PromptError):
        expand_block_template("{include:../secret.md}", context, producers={})


# --- resolve_block_text and assembly ----------------------------------------


@pytest.mark.parametrize(
    ("definition", "expected", "warnings"),
    [
        (
            _text_block("core:a", "{generated:x}\n{include:EXTRA.md}"),
            'PRODUCED\n<file name="EXTRA.md">\nextra\n</file>',
            [],
        ),
        (_text_block("core:overridden", "default"), "OVERRIDDEN", []),
        # A data block's text is inserted literally; its "{...}" is never interpreted.
        (
            BlockDefinition(
                id="core:agent_body",
                owner="always",
                kind=BLOCK_KIND_DATA,
                default_text="Use {generated:x} and {include:EXTRA.md} literally; also {custom}.",
            ),
            "Use {generated:x} and {include:EXTRA.md} literally; also {custom}.",
            [],
        ),
        (
            BlockDefinition(id="tool:bash", owner="tool:bash", render=lambda ctx: ctx.agent.id),
            "builder",
            [],
        ),
        (
            BlockDefinition(id="tool:bash", owner="tool:bash", render=_raise("render exploded")),
            "",
            ["tool:bash", "render exploded"],
        ),
    ],
    ids=["static-text", "override", "data", "dynamic", "failing-dynamic"],
)
def test_resolve_block_text_by_block_kind(
    definition: BlockDefinition,
    expected: str,
    warnings: list[str],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "EXTRA.md").write_text("extra", encoding="utf-8")
    overrides = MappingOverrideResolver({("default", "core:overridden"): "OVERRIDDEN"})

    with caplog.at_level(logging.WARNING):
        result = resolve_block_text(
            ResolvedBlock(definition=definition, enabled=True),
            _context(StubAgent(id="builder", workspace=str(tmp_path))),
            override_resolver=overrides,
            producers={"x": lambda ctx: "PRODUCED"},
        )

    assert result == expected
    assert all(fragment in caplog.text for fragment in warnings)


def test_assemble_joins_only_blocks_passing_every_gate_in_layout_order() -> None:
    definitions = [
        _text_block("core:a", "Alpha"),
        _text_block("core:b", "Beta"),
        _text_block("core:c", "Gamma"),
        _text_block("memory:guidance", "Memory", owner="memory"),
        _text_block("core:empty", "{generated:nothing}"),
        BlockDefinition(id="tool:bash", owner="tool:bash", render=_raise("nope")),
        _text_block("core:new", "New"),
    ]
    layout = [
        LayoutEntry(id="core:c"),
        LayoutEntry(id="tool:bash"),
        LayoutEntry(id="core:b", enabled=False),
        LayoutEntry(id="memory:guidance"),
        LayoutEntry(id="core:empty"),
        LayoutEntry(id="core:a"),
    ]

    result = assemble_system_prompt(
        definitions,
        layout,
        _context(),
        owner_activity=CallableOwnerActivity(lambda owner, agent: owner != "memory"),
        override_resolver=MappingOverrideResolver(),
        producers={"nothing": lambda ctx: ""},
    )

    # A newly added contributor without a layout entry defaults in at its rank.
    assert result == "Gamma\n\nAlpha\n\nNew"
