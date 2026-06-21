"""Tests for the OpenCode agent detector."""

from __future__ import annotations

from pathlib import Path

from core.projects.scanners.opencode import (
    OPENCODE_AGENTS_SUBPATH,
    OPENCODE_FORMAT_KEY,
    OpenCodeDetector,
)


def _write_agent(project_root: Path, filename: str, content: str) -> Path:
    agents_dir = project_root.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def test_detect_parses_frontmatter_and_body(tmp_path: Path) -> None:
    # Arrange
    content = (
        "---\n"
        "description: Writes code and tests.\n"
        "model: opencode-go/minimax-m3\n"
        "temperature: 0.4\n"
        "reasoningEffort: high\n"
        "permission:\n"
        "  task: deny\n"
        "---\n"
        "\n"
        "# builder Agent\n"
        "\n"
        "You write code.\n"
    )
    _write_agent(tmp_path, "builder.md", content)

    # Act
    detected = OpenCodeDetector().detect(tmp_path)

    # Assert
    assert len(detected) == 1
    agent = detected[0].agent
    assert agent is not None
    assert agent.agent_id == "builder"
    assert agent.display_name == "builder"
    assert agent.description == "Writes code and tests."
    assert agent.model == "opencode-go/minimax-m3"
    assert agent.temperature == 0.4
    assert agent.thinking_effort == "high"
    assert agent.source_format == OPENCODE_FORMAT_KEY
    # permission.task: deny → the subagent tool is the only thing turned off.
    assert agent.denied_tools == frozenset({"subagent"})
    assert agent.body == "# builder Agent\n\nYou write code.\n"


def test_detect_takes_model_one_to_one(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "planner.md",
        "---\nmodel: anthropic/claude-opus-4\n---\nBody.\n",
    )

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    # The model string must be carried verbatim, never rewritten.
    assert detected[0].agent.model == "anthropic/claude-opus-4"


def test_detect_keeps_body_verbatim_with_braces(tmp_path: Path) -> None:
    # The body may contain {...}; it must be opaque text, not expanded here.
    body_with_braces = "Use {include:SOUL.md} and {project_files} literally.\n"
    _write_agent(
        tmp_path,
        "orchestrator.md",
        f"---\ndescription: x\n---\n{body_with_braces}",
    )

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.body == body_with_braces


def test_detect_missing_model_yields_empty_string(tmp_path: Path) -> None:
    _write_agent(tmp_path, "architect.md", "---\ndescription: x\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.model == ""
    assert detected[0].agent.temperature is None


def test_detect_slugifies_crooked_filename(tmp_path: Path) -> None:
    _write_agent(tmp_path, "Build Helper.md", "---\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.agent_id == "build-helper"
    # Display name preserves the raw stem.
    assert detected[0].agent.display_name == "Build Helper"


def test_detect_unslugifiable_name_is_parse_failure(tmp_path: Path) -> None:
    # A filename that is valid on disk but slugifies to nothing (only separators,
    # all edge-trimmed) — the report turns it into an unslugifiable-name finding.
    _write_agent(tmp_path, "___.md", "---\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert len(detected) == 1
    assert detected[0].agent is None
    assert detected[0].error_reason is not None


def test_detect_missing_location_returns_empty(tmp_path: Path) -> None:
    # No .opencode/agents/ at all — normal, not an error.
    detected = OpenCodeDetector().detect(tmp_path)

    assert detected == []


def test_detect_is_non_recursive(tmp_path: Path) -> None:
    # An agent in a nested subdirectory of the known location is NOT collected.
    agents_dir = tmp_path.joinpath(*OPENCODE_AGENTS_SUBPATH)
    nested = agents_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "deep.md").write_text("---\n---\nBody.\n", encoding="utf-8")
    _write_agent(tmp_path, "top.md", "---\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    ids = [item.raw_name for item in detected]
    assert ids == ["top"]


def test_detect_ignores_nested_repo(tmp_path: Path) -> None:
    # A nested project with its own .opencode/agents/ is not picked up by the parent.
    nested_repo = tmp_path / "subproject"
    _write_agent(nested_repo, "child.md", "---\n---\nBody.\n")
    _write_agent(tmp_path, "parent.md", "---\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert [item.raw_name for item in detected] == ["parent"]


def test_detect_sorts_by_filename_not_fs_order(tmp_path: Path) -> None:
    for name in ("zeta.md", "alpha.md", "mid.md"):
        _write_agent(tmp_path, name, "---\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert [item.source_path.name for item in detected] == ["alpha.md", "mid.md", "zeta.md"]


def test_detect_rejects_boolean_temperature(tmp_path: Path) -> None:
    _write_agent(tmp_path, "a.md", "---\ntemperature: true\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.temperature is None


def test_detect_reads_reasoning_effort(tmp_path: Path) -> None:
    _write_agent(tmp_path, "a.md", "---\nreasoningEffort: high\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.thinking_effort == "high"


def test_detect_normalizes_reasoning_effort_case_and_whitespace(tmp_path: Path) -> None:
    _write_agent(tmp_path, "a.md", '---\nreasoningEffort: "  High  "\n---\nBody.\n')

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.thinking_effort == "high"


def test_detect_missing_reasoning_effort_yields_none(tmp_path: Path) -> None:
    _write_agent(tmp_path, "a.md", "---\ndescription: x\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.thinking_effort is None


def test_detect_unknown_reasoning_effort_yields_none(tmp_path: Path) -> None:
    # A foreign effort vBot does not know must fall through silently, not crash.
    _write_agent(tmp_path, "a.md", "---\nreasoningEffort: turbo\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.thinking_effort is None


def _denied_tools_for(tmp_path: Path, front_matter: str) -> frozenset[str]:
    """Parse one agent file's front matter and return its scanned denied_tools."""
    _write_agent(tmp_path, "a.md", f"---\n{front_matter}---\nBody.\n")
    detected = OpenCodeDetector().detect(tmp_path)
    assert detected[0].agent is not None
    return detected[0].agent.denied_tools


def test_denied_tools_empty_when_no_permission_or_tools(tmp_path: Path) -> None:
    # An agent that declares nothing turns nothing off — the ceiling stays whole.
    assert _denied_tools_for(tmp_path, "description: x\n") == frozenset()


def test_denied_tools_permission_edit_denies_edit_and_write(tmp_path: Path) -> None:
    # The edit permission key has no separate write counterpart, so it covers both.
    assert _denied_tools_for(tmp_path, "permission:\n  edit: deny\n") == frozenset(
        {"edit", "write"}
    )


def test_denied_tools_permission_bash_denies_bash_and_process(tmp_path: Path) -> None:
    # OpenCode bash maps to both vBot bash and process — denied together.
    assert _denied_tools_for(tmp_path, "permission:\n  bash: deny\n") == frozenset(
        {"bash", "process"}
    )


def test_denied_tools_permission_task_denies_subagent(tmp_path: Path) -> None:
    assert _denied_tools_for(tmp_path, "permission:\n  task: deny\n") == frozenset({"subagent"})


def test_denied_tools_maps_each_permission_key(tmp_path: Path) -> None:
    front_matter = (
        "permission:\n"
        "  read: deny\n"
        "  grep: deny\n"
        "  glob: deny\n"
        "  webfetch: deny\n"
        "  websearch: deny\n"
    )
    assert _denied_tools_for(tmp_path, front_matter) == frozenset(
        {"read", "grep", "glob", "web_fetch", "web_search"}
    )


def test_denied_tools_allow_and_ask_are_not_denials(tmp_path: Path) -> None:
    # allow / ask never turn a tool off (ask has no per-call gate in vBot).
    front_matter = "permission:\n  edit: allow\n  bash: ask\n  task: allow\n"
    assert _denied_tools_for(tmp_path, front_matter) == frozenset()


def test_denied_tools_ignores_unmapped_permission_keys(tmp_path: Path) -> None:
    # Keys without a vBot counterpart are ignored even on deny, never crashing.
    front_matter = (
        "permission:\n"
        "  list: deny\n"
        "  lsp: deny\n"
        "  todowrite: deny\n"
        "  question: deny\n"
        "  external_directory: deny\n"
        "  doom_loop: deny\n"
        "  skill: deny\n"
    )
    assert _denied_tools_for(tmp_path, front_matter) == frozenset()


def test_denied_tools_granular_all_deny_collapses_to_deny(tmp_path: Path) -> None:
    # A granular map where every entry denies collapses to a full deny.
    front_matter = 'permission:\n  bash:\n    "*": deny\n    "rm *": deny\n'
    assert _denied_tools_for(tmp_path, front_matter) == frozenset({"bash", "process"})


def test_denied_tools_granular_with_any_allow_is_not_denied(tmp_path: Path) -> None:
    # A single allow/ask anywhere in the granular map means the tool stays on.
    front_matter = 'permission:\n  bash:\n    "*": ask\n    "git *": allow\n'
    assert _denied_tools_for(tmp_path, front_matter) == frozenset()


def test_denied_tools_empty_granular_map_is_not_denied(tmp_path: Path) -> None:
    front_matter = "permission:\n  bash: {}\n"
    assert _denied_tools_for(tmp_path, front_matter) == frozenset()


def test_denied_tools_case_insensitive_deny(tmp_path: Path) -> None:
    assert _denied_tools_for(tmp_path, "permission:\n  task: DENY\n") == frozenset({"subagent"})


def test_denied_tools_from_tools_map_false_denies(tmp_path: Path) -> None:
    # tools is deny-by-exception: only an explicit false turns a tool off, and
    # tools.write / tools.edit are separate names (unlike permission.edit).
    front_matter = "tools:\n  write: false\n  read: false\n"
    assert _denied_tools_for(tmp_path, front_matter) == frozenset({"write", "read"})


def test_denied_tools_tools_edit_denies_edit_only(tmp_path: Path) -> None:
    assert _denied_tools_for(tmp_path, "tools:\n  edit: false\n") == frozenset({"edit"})


def test_denied_tools_tools_true_keeps_tool_on(tmp_path: Path) -> None:
    assert _denied_tools_for(tmp_path, "tools:\n  read: true\n") == frozenset()


def test_denied_tools_unions_permission_and_tools(tmp_path: Path) -> None:
    front_matter = "permission:\n  task: deny\ntools:\n  read: false\n"
    assert _denied_tools_for(tmp_path, front_matter) == frozenset({"subagent", "read"})


def test_denied_tools_foreign_permission_shape_fails_open(tmp_path: Path) -> None:
    # A non-string, non-map permission value is foreign — treated as not a deny.
    front_matter = "permission:\n  bash: 123\n"
    assert _denied_tools_for(tmp_path, front_matter) == frozenset()
