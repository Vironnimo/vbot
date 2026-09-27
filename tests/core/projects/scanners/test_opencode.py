"""Tests for the OpenCode agent detector."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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

    detected = OpenCodeDetector().detect(tmp_path)

    assert len(detected) == 1
    agent = detected[0].agent
    assert agent is not None
    assert agent.agent_id == "builder"
    assert agent.display_name == "builder"
    assert agent.description == "Writes code and tests."
    # The Model string is carried verbatim, never rewritten.
    assert agent.model == "opencode-go/minimax-m3"
    assert agent.temperature == 0.4
    assert agent.thinking_effort == "high"
    assert agent.source_format == OPENCODE_FORMAT_KEY
    # permission.task: deny turns off only the subagent Tool and every target.
    assert agent.denied_tools == frozenset({"subagent"})
    assert [(rule.pattern, rule.allowed) for rule in agent.agent_target_rules] == [("*", False)]
    assert agent.body == "# builder Agent\n\nYou write code.\n"


@pytest.mark.parametrize(
    ("front_matter", "body", "fields"),
    [
        pytest.param(
            "description: x\n",
            "Body.\n",
            {"model": "", "temperature": None, "thinking_effort": None},
            id="no-fields",
        ),
        pytest.param(
            "temperature: true\n", "Body.\n", {"temperature": None}, id="bool-temperature"
        ),
        pytest.param(
            'reasoningEffort: "  High  "\n',
            "Body.\n",
            {"thinking_effort": "high"},
            id="effort-case-and-whitespace",
        ),
        # A foreign effort vBot does not know falls through silently.
        pytest.param(
            "reasoningEffort: turbo\n", "Body.\n", {"thinking_effort": None}, id="unknown-effort"
        ),
        # The body is opaque text; placeholders are not expanded here.
        pytest.param(
            "description: x\n",
            "Use {include:SOUL.md} and {project_files} literally.\n",
            {"body": "Use {include:SOUL.md} and {project_files} literally.\n"},
            id="body-with-braces",
        ),
    ],
)
def test_detect_normalizes_agent_fields(
    tmp_path: Path, front_matter: str, body: str, fields: dict[str, Any]
) -> None:
    _write_agent(tmp_path, "a.md", f"---\n{front_matter}---\n{body}")

    [detected] = OpenCodeDetector().detect(tmp_path)

    assert detected.agent is not None
    assert {field: getattr(detected.agent, field) for field in fields} == fields


@pytest.mark.parametrize(
    ("filename", "identity"),
    [
        # The display name preserves the raw stem.
        pytest.param("Build Helper.md", ("build-helper", "Build Helper"), id="slugified"),
        # Valid on disk but slugifies to nothing: a parse failure for the report.
        pytest.param("___.md", None, id="unslugifiable"),
    ],
)
def test_detect_derives_the_agent_id_from_the_filename(
    tmp_path: Path, filename: str, identity: tuple[str, str] | None
) -> None:
    _write_agent(tmp_path, filename, "---\n---\nBody.\n")

    [detected] = OpenCodeDetector().detect(tmp_path)

    if identity is None:
        assert detected.agent is None
        assert detected.error_reason is not None
    else:
        assert detected.agent is not None
        assert (detected.agent.agent_id, detected.agent.display_name) == identity


def test_detect_collects_only_top_level_agents_sorted_by_filename(tmp_path: Path) -> None:
    # No .opencode/agents/ at all is normal, not an error.
    assert OpenCodeDetector().detect(tmp_path) == []

    for name in ("zeta.md", "alpha.md", "mid.md"):
        _write_agent(tmp_path, name, "---\n---\nBody.\n")
    # Neither a nested directory nor a nested repository is collected.
    nested = tmp_path.joinpath(*OPENCODE_AGENTS_SUBPATH) / "nested"
    nested.mkdir()
    (nested / "deep.md").write_text("---\n---\nBody.\n", encoding="utf-8")
    _write_agent(tmp_path / "subproject", "child.md", "---\n---\nBody.\n")

    detected = OpenCodeDetector().detect(tmp_path)

    assert [item.source_path.name for item in detected] == ["alpha.md", "mid.md", "zeta.md"]


@pytest.mark.parametrize(
    ("front_matter", "denied"),
    [
        # The edit permission covers targeted changes and full replacement.
        pytest.param("permission:\n  edit: deny\n", {"apply_patch"}, id="edit"),
        # OpenCode bash maps to both vBot bash and process.
        pytest.param("permission:\n  bash: deny\n", {"bash", "process"}, id="bash"),
        pytest.param("permission:\n  task: DENY\n", {"subagent"}, id="case-insensitive"),
        pytest.param(
            "permission:\n  read: deny\n  grep: deny\n  glob: deny\n"
            "  webfetch: deny\n  websearch: deny\n",
            {"read", "search_files", "web_fetch", "web_search"},
            id="each-permission-key",
        ),
        # allow and ask never turn a Tool off (ask has no per-call gate in vBot).
        pytest.param(
            "permission:\n  edit: allow\n  bash: ask\n  task: allow\n", set(), id="allow-and-ask"
        ),
        # Keys without a vBot counterpart are ignored even on deny.
        pytest.param(
            "permission:\n  list: deny\n  lsp: deny\n  todowrite: deny\n  question: deny\n"
            "  external_directory: deny\n  doom_loop: deny\n  skill: deny\n",
            set(),
            id="unmapped-keys",
        ),
        # A granular map denies only when every entry denies.
        pytest.param(
            'permission:\n  bash:\n    "*": deny\n    "rm *": deny\n',
            {"bash", "process"},
            id="granular-all-deny",
        ),
        pytest.param(
            'permission:\n  bash:\n    "*": ask\n    "git *": allow\n',
            set(),
            id="granular-any-allow",
        ),
        pytest.param("permission:\n  bash: {}\n", set(), id="granular-empty"),
        # A non-string, non-map permission value is foreign, not a denial.
        pytest.param("permission:\n  bash: 123\n", set(), id="foreign-shape"),
        # tools is deny-by-exception, and tools.write / tools.edit are separate names.
        pytest.param(
            "tools:\n  write: false\n  read: false\n", {"apply_patch", "read"}, id="tools-false"
        ),
        pytest.param("tools:\n  edit: false\n", {"apply_patch"}, id="tools-edit"),
        pytest.param("tools:\n  read: true\n", set(), id="tools-true"),
        pytest.param(
            "permission:\n  task: deny\ntools:\n  read: false\n",
            {"subagent", "read"},
            id="permission-and-tools",
        ),
    ],
)
def test_denied_tools_map_opencode_permissions(
    tmp_path: Path, front_matter: str, denied: set[str]
) -> None:
    _write_agent(tmp_path, "a.md", f"---\n{front_matter}---\nBody.\n")

    [detected] = OpenCodeDetector().detect(tmp_path)

    assert detected.agent is not None
    assert detected.agent.denied_tools == frozenset(denied)


@pytest.mark.parametrize(
    ("front_matter", "rules"),
    [
        pytest.param(
            'permission:\n  task:\n    "*": deny\n    "review-*": allow\n'
            '    "review-legacy": deny\n',
            [("*", False), ("review-*", True), ("review-legacy", False)],
            id="ordered-task-patterns",
        ),
        pytest.param("tools:\n  task: false\n", [("*", False)], id="tools-task-false"),
    ],
)
def test_agent_target_rules_follow_task_permissions(
    tmp_path: Path, front_matter: str, rules: list[tuple[str, bool]]
) -> None:
    _write_agent(tmp_path, "orchestrator.md", f"---\n{front_matter}---\nBody.\n")

    [detected] = OpenCodeDetector().detect(tmp_path)

    assert detected.agent is not None
    assert [(rule.pattern, rule.allowed) for rule in detected.agent.agent_target_rules] == rules
