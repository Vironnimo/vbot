"""Tests for the Claude Code agent detector."""

from __future__ import annotations

from pathlib import Path

from core.projects.scanners.claude import (
    CLAUDE_AGENTS_SUBPATH,
    CLAUDE_FORMAT_KEY,
    ClaudeDetector,
)


def _write_agent(project_root: Path, relative_path: str, content: str) -> Path:
    agents_dir = project_root.joinpath(*CLAUDE_AGENTS_SUBPATH)
    path = agents_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_detect_parses_frontmatter_and_body(tmp_path: Path) -> None:
    # Arrange
    content = (
        "---\n"
        "name: code-reviewer\n"
        "description: Reviews code for defects.\n"
        "---\n"
        "\n"
        "# Reviewer\n"
        "\n"
        "You review code.\n"
    )
    _write_agent(tmp_path, "reviewer.md", content)

    # Act
    detected = ClaudeDetector().detect(tmp_path)

    # Assert
    assert len(detected) == 1
    agent = detected[0].agent
    assert agent is not None
    # The frontmatter name is the canonical Claude Code identifier, not the stem.
    assert agent.agent_id == "code-reviewer"
    assert agent.display_name == "code-reviewer"
    assert agent.description == "Reviews code for defects."
    assert agent.source_format == CLAUDE_FORMAT_KEY
    assert agent.denied_tools == frozenset()
    assert agent.body == "# Reviewer\n\nYou review code.\n"


def test_detect_falls_back_to_filename_stem_without_name(tmp_path: Path) -> None:
    _write_agent(tmp_path, "helper.md", "---\ndescription: x\n---\nBody.\n")

    detected = ClaudeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.agent_id == "helper"


def test_detect_slugifies_crooked_name(tmp_path: Path) -> None:
    _write_agent(tmp_path, "a.md", "---\nname: Code Reviewer\n---\nBody.\n")

    detected = ClaudeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.agent_id == "code-reviewer"
    # Display name preserves the raw frontmatter name.
    assert detected[0].agent.display_name == "Code Reviewer"


def test_detect_unslugifiable_name_is_parse_failure(tmp_path: Path) -> None:
    _write_agent(tmp_path, "a.md", '---\nname: "___"\n---\nBody.\n')

    detected = ClaudeDetector().detect(tmp_path)

    assert len(detected) == 1
    assert detected[0].agent is None
    assert detected[0].error_reason is not None


def test_detect_always_drops_model_and_sampling(tmp_path: Path) -> None:
    # Claude model vocabulary (aliases, Anthropic ids, inherit) is never vBot's
    # <provider>/<model-id> form — always dropped, no BAD_MODEL noise. Claude
    # agents carry no temperature/reasoning either.
    _write_agent(tmp_path, "a.md", "---\nname: a\nmodel: sonnet\n---\nBody.\n")

    detected = ClaudeDetector().detect(tmp_path)

    agent = detected[0].agent
    assert agent is not None
    assert agent.model == ""
    assert agent.temperature is None
    assert agent.thinking_effort is None


def test_detect_picks_up_nested_subdirectories(tmp_path: Path) -> None:
    # Claude Code allows agent subfolders — recursive within .claude/agents/ only.
    _write_agent(tmp_path, "top.md", "---\nname: top\n---\nBody.\n")
    _write_agent(tmp_path, "review/deep.md", "---\nname: deep\n---\nBody.\n")

    detected = ClaudeDetector().detect(tmp_path)

    assert [item.raw_name for item in detected] == ["deep", "top"]


def test_detect_sorts_by_relative_posix_path(tmp_path: Path) -> None:
    _write_agent(tmp_path, "zeta.md", "---\nname: zeta\n---\nBody.\n")
    _write_agent(tmp_path, "sub/alpha.md", "---\nname: alpha\n---\nBody.\n")
    _write_agent(tmp_path, "beta.md", "---\nname: beta\n---\nBody.\n")

    detected = ClaudeDetector().detect(tmp_path)

    relative = [
        item.source_path.relative_to(tmp_path.joinpath(*CLAUDE_AGENTS_SUBPATH)).as_posix()
        for item in detected
    ]
    assert relative == ["beta.md", "sub/alpha.md", "zeta.md"]


def test_detect_missing_location_returns_empty(tmp_path: Path) -> None:
    # No .claude/agents/ at all — normal, not an error.
    assert ClaudeDetector().detect(tmp_path) == []


def test_detect_does_not_escape_known_location(tmp_path: Path) -> None:
    # A markdown file elsewhere under .claude/ (or the repo) is never picked up.
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "notes.md").write_text("not an agent", encoding="utf-8")
    (tmp_path / "README.md").write_text("not an agent", encoding="utf-8")
    _write_agent(tmp_path, "real.md", "---\nname: real\n---\nBody.\n")

    detected = ClaudeDetector().detect(tmp_path)

    assert [item.raw_name for item in detected] == ["real"]


def test_detect_keeps_body_verbatim_with_braces(tmp_path: Path) -> None:
    body_with_braces = "Use {include:SOUL.md} and {project_files} literally.\n"
    _write_agent(tmp_path, "a.md", f"---\nname: a\n---\n{body_with_braces}")

    detected = ClaudeDetector().detect(tmp_path)

    assert detected[0].agent is not None
    assert detected[0].agent.body == body_with_braces


def test_detect_malformed_yaml_fails_open(tmp_path: Path) -> None:
    # Broken frontmatter degrades to "no fields" — stem id, empty description,
    # nothing denied — and never crashes the scan.
    _write_agent(tmp_path, "broken.md", "---\nname: [unclosed\n---\nBody.\n")

    detected = ClaudeDetector().detect(tmp_path)

    agent = detected[0].agent
    assert agent is not None
    assert agent.agent_id == "broken"
    assert agent.denied_tools == frozenset()


def _denied_tools_for(tmp_path: Path, front_matter: str) -> frozenset[str]:
    """Parse one agent file's front matter and return its scanned denied_tools."""
    _write_agent(tmp_path, "a.md", f"---\nname: a\n{front_matter}---\nBody.\n")
    detected = ClaudeDetector().detect(tmp_path)
    assert detected[0].agent is not None
    return detected[0].agent.denied_tools


def test_denied_tools_empty_without_tools_fields(tmp_path: Path) -> None:
    # Omitted tools = inherit all; nothing is denied.
    assert _denied_tools_for(tmp_path, "description: x\n") == frozenset()


def test_denied_tools_disallowed_tools_deny_their_mapping(tmp_path: Path) -> None:
    assert _denied_tools_for(tmp_path, "disallowedTools: Bash, WebFetch\n") == frozenset(
        {"bash", "process", "web_fetch"}
    )


def test_denied_tools_disallowed_accepts_yaml_list(tmp_path: Path) -> None:
    front_matter = "disallowedTools:\n  - Write\n  - Edit\n"
    assert _denied_tools_for(tmp_path, front_matter) == frozenset({"write", "edit"})


def test_denied_tools_allow_list_inverts_to_denials(tmp_path: Path) -> None:
    # tools present → every mappable Claude tool NOT named is denied.
    denied = _denied_tools_for(tmp_path, "tools: Read, Grep, Glob\n")

    assert denied == frozenset(
        {"write", "edit", "bash", "process", "web_fetch", "web_search", "subagent", "skill"}
    )


def test_denied_tools_allow_list_never_denies_unmappable_vbot_tools(tmp_path: Path) -> None:
    # vBot tools with no Claude counterpart (e.g. status) are never denied by the
    # inversion — only mapped vBot tools can appear.
    denied = _denied_tools_for(tmp_path, "tools: Read\n")

    assert "status" not in denied


def test_denied_tools_names_match_case_insensitive_trimmed(tmp_path: Path) -> None:
    assert _denied_tools_for(tmp_path, "disallowedTools: '  BASH , webfetch '\n") == frozenset(
        {"bash", "process", "web_fetch"}
    )


def test_denied_tools_unknown_names_are_ignored(tmp_path: Path) -> None:
    # Unknown Claude tools (MCP names, future tools) never deny anything.
    assert _denied_tools_for(tmp_path, "disallowedTools: NotebookEdit, mcp__foo\n") == frozenset()


def test_denied_tools_unknown_allow_list_entries_do_not_widen(tmp_path: Path) -> None:
    # An allow-list of only unknown names still denies every mappable tool.
    denied = _denied_tools_for(tmp_path, "tools: mcp__foo\n")

    assert denied == frozenset(
        {
            "read",
            "write",
            "edit",
            "glob",
            "grep",
            "bash",
            "process",
            "web_fetch",
            "web_search",
            "subagent",
            "skill",
        }
    )


def test_denied_tools_unions_allow_list_and_disallowed(tmp_path: Path) -> None:
    front_matter = "tools: Read, Write, Bash\ndisallowedTools: Write\n"
    denied = _denied_tools_for(tmp_path, front_matter)

    # Write is denied explicitly even though the allow-list names it; everything
    # mappable outside the allow-list is denied by inversion.
    assert "write" in denied
    assert "read" not in denied
    assert "bash" not in denied
    assert "edit" in denied


def test_denied_tools_malformed_shapes_fail_open(tmp_path: Path) -> None:
    # A mapping/number where a list is expected is foreign — treated as absent.
    assert _denied_tools_for(tmp_path, "tools:\n  read: true\ndisallowedTools: 7\n") == frozenset()


def test_denied_tools_empty_tools_string_fails_open(tmp_path: Path) -> None:
    # "tools: ''" is more likely noise than an explicit empty allow-list.
    assert _denied_tools_for(tmp_path, "tools: ''\n") == frozenset()
