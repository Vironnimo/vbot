"""Tests for the Claude Code agent detector."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.projects.scanners.claude import (
    CLAUDE_AGENTS_SUBPATH,
    CLAUDE_FORMAT_KEY,
    ClaudeDetector,
)

_BODY_WITH_BRACES = "# Reviewer\n\nUse {include:SOUL.md} and {project_files} literally.\n"


def _write_agent(project_root: Path, relative_path: str, content: str) -> Path:
    agents_dir = project_root.joinpath(*CLAUDE_AGENTS_SUBPATH)
    path = agents_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_detect_parses_frontmatter_and_body(tmp_path: Path) -> None:
    content = (
        "---\n"
        "name: code-reviewer\n"
        "description: Reviews code for defects.\n"
        "model: sonnet\n"
        "---\n"
        "\n"
        f"{_BODY_WITH_BRACES}"
    )
    _write_agent(tmp_path, "reviewer.md", content)

    detected = ClaudeDetector().detect(tmp_path)

    assert len(detected) == 1
    agent = detected[0].agent
    assert agent is not None
    # The frontmatter name is the canonical Claude Code identifier, not the stem.
    assert agent.agent_id == "code-reviewer"
    assert agent.display_name == "code-reviewer"
    assert agent.description == "Reviews code for defects."
    assert agent.source_format == CLAUDE_FORMAT_KEY
    assert agent.denied_tools == frozenset()
    # Claude Model vocabulary (aliases, Anthropic ids, inherit) is never vBot's
    # <provider>/<model-id> form, so it is always dropped; Claude Agents carry no
    # sampling settings either.
    assert (agent.model, agent.temperature, agent.thinking_effort) == ("", None, None)
    # The body is opaque text; placeholders are not expanded here.
    assert agent.body == _BODY_WITH_BRACES


@pytest.mark.parametrize(
    ("filename", "front_matter", "identity"),
    [
        pytest.param("helper.md", "description: x\n", ("helper", "helper"), id="filename-stem"),
        # The display name preserves the raw frontmatter name.
        pytest.param(
            "a.md", "name: Code Reviewer\n", ("code-reviewer", "Code Reviewer"), id="slugified"
        ),
        # Broken frontmatter degrades to no fields and never crashes the scan.
        pytest.param("broken.md", "name: [unclosed\n", ("broken", "broken"), id="malformed-yaml"),
        # A name that slugifies to nothing becomes a parse failure for the report.
        pytest.param("a.md", 'name: "___"\n', None, id="unslugifiable"),
    ],
)
def test_detect_derives_the_agent_id(
    tmp_path: Path, filename: str, front_matter: str, identity: tuple[str, str] | None
) -> None:
    _write_agent(tmp_path, filename, f"---\n{front_matter}---\nBody.\n")

    [detected] = ClaudeDetector().detect(tmp_path)

    if identity is None:
        assert detected.agent is None
        assert detected.error_reason is not None
    else:
        assert detected.agent is not None
        assert (detected.agent.agent_id, detected.agent.display_name) == identity
        assert detected.agent.denied_tools == frozenset()


def test_detect_collects_nested_agents_only_inside_the_known_location(tmp_path: Path) -> None:
    # No .claude/agents/ at all is normal, not an error.
    assert ClaudeDetector().detect(tmp_path) == []

    # Claude Code allows Agent subfolders; files elsewhere are never Agents.
    _write_agent(tmp_path, "zeta.md", "---\nname: zeta\n---\nBody.\n")
    _write_agent(tmp_path, "sub/alpha.md", "---\nname: alpha\n---\nBody.\n")
    _write_agent(tmp_path, "beta.md", "---\nname: beta\n---\nBody.\n")
    (tmp_path / ".claude" / "notes.md").write_text("not an agent", encoding="utf-8")
    (tmp_path / "README.md").write_text("not an agent", encoding="utf-8")

    detected = ClaudeDetector().detect(tmp_path)

    agents_dir = tmp_path.joinpath(*CLAUDE_AGENTS_SUBPATH)
    assert [item.source_path.relative_to(agents_dir).as_posix() for item in detected] == [
        "beta.md",
        "sub/alpha.md",
        "zeta.md",
    ]


_MAPPED_TOOLS = frozenset(
    {
        "read",
        "apply_patch",
        "search_files",
        "bash",
        "process",
        "web_fetch",
        "web_search",
        "subagent",
        "skill",
    }
)


@pytest.mark.parametrize(
    ("front_matter", "denied"),
    [
        # Omitted tools inherit everything.
        pytest.param("description: x\n", frozenset(), id="no-tool-fields"),
        pytest.param(
            "disallowedTools: Bash, WebFetch\n",
            frozenset({"bash", "process", "web_fetch"}),
            id="disallowed-string",
        ),
        pytest.param(
            "disallowedTools:\n  - Write\n  - Edit\n", frozenset({"apply_patch"}), id="yaml-list"
        ),
        # Either file-mutation Tool alone blocks apply_patch.
        pytest.param("disallowedTools: Edit\n", frozenset({"apply_patch"}), id="edit-only"),
        pytest.param("disallowedTools: Write\n", frozenset({"apply_patch"}), id="write-only"),
        pytest.param(
            "disallowedTools: '  BASH , webfetch '\n",
            frozenset({"bash", "process", "web_fetch"}),
            id="case-insensitive-trimmed",
        ),
        # Unknown Claude Tools (MCP names, future Tools) never deny anything.
        pytest.param(
            "disallowedTools: NotebookEdit, mcp__foo\n", frozenset(), id="unknown-disallowed"
        ),
        # An allow-list denies every mapped Tool it does not name; vBot Tools without
        # a Claude counterpart (such as status) are never denied.
        pytest.param(
            "tools: Read, Grep, Glob\n",
            _MAPPED_TOOLS - {"read", "search_files"},
            id="allow-list",
        ),
        pytest.param(
            "tools: Read, Edit, Write\n",
            _MAPPED_TOOLS - {"read", "apply_patch"},
            id="allow-list-with-both-mutation-tools",
        ),
        pytest.param("tools: mcp__foo\n", _MAPPED_TOOLS, id="allow-list-of-unknown-names"),
        # An explicit denial wins over the allow-list entry.
        pytest.param(
            "tools: Read, Write, Bash\ndisallowedTools: Write\n",
            _MAPPED_TOOLS - {"read", "bash", "process"},
            id="allow-list-and-disallowed",
        ),
        # Foreign shapes and an empty tools string are noise, treated as absent.
        pytest.param(
            "tools:\n  read: true\ndisallowedTools: 7\n", frozenset(), id="malformed-shapes"
        ),
        pytest.param("tools: ''\n", frozenset(), id="empty-tools-string"),
    ],
)
def test_denied_tools_map_claude_tool_fields(
    tmp_path: Path, front_matter: str, denied: frozenset[str]
) -> None:
    _write_agent(tmp_path, "a.md", f"---\nname: a\n{front_matter}---\nBody.\n")

    [detected] = ClaudeDetector().detect(tmp_path)

    assert detected.agent is not None
    assert detected.agent.denied_tools == denied


@pytest.mark.parametrize(
    ("front_matter", "rules"),
    [
        pytest.param(
            "tools: Agent(worker, reviewer), Read\n",
            [("*", False), ("worker", True), ("reviewer", True)],
            id="scoped-allow-list",
        ),
        pytest.param("disallowedTools: Agent(worker)\n", [("worker", False)], id="scoped-denial"),
    ],
)
def test_scoped_agent_tool_limits_targets_but_keeps_subagent(
    tmp_path: Path, front_matter: str, rules: list[tuple[str, bool]]
) -> None:
    _write_agent(
        tmp_path, "orchestrator.md", f"---\nname: orchestrator\n{front_matter}---\nBody.\n"
    )

    [detected] = ClaudeDetector().detect(tmp_path)

    agent = detected.agent
    assert agent is not None
    assert "subagent" not in agent.denied_tools
    assert [(rule.pattern, rule.allowed) for rule in agent.agent_target_rules] == rules
