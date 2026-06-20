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
    assert agent.tools == ("*",)
    assert agent.skills == ("*",)
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
