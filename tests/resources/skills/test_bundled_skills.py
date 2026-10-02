"""Bundled Skill packages load, stay available and expose every file they reference."""

import hashlib
import json
import re
import zipfile
from pathlib import Path

from core.skills.skills import SkillRegistry
from tests.core.tools.skill_test_support import SkillTool

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = PROJECT_ROOT / "resources" / "skills"


def local_markdown_links(document: Path) -> list[str]:
    links = re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8"))
    return [target for link in links if (target := link.split("#", 1)[0]) and "://" not in target]


def assert_files_readable_through_the_tool(tool: SkillTool, name: str, files: list[str]) -> None:
    for relative in files:
        result = tool.call({"name": name, "file_path": relative})
        assert result["data"]["status"] == "file_loaded", relative
        assert result["data"]["file_path"] == relative
        expected = (SKILLS_ROOT / name / relative).read_text(encoding="utf-8")
        assert result["data"]["content"] == expected


def test_coding_agents_loads_with_reachable_references() -> None:
    package = SKILLS_ROOT / "coding-agents"
    skill_text = (package / "SKILL.md").read_text(encoding="utf-8")
    references = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((package / "references").glob("*.md"))
    }
    linked_references = set(re.findall(r"references/([\w-]+\.md)", skill_text))
    assert linked_references == {"codex.md", "claude-code.md", "opencode.md"}
    assert linked_references == set(references)
    assert all(text.strip() for text in references.values())

    registry = SkillRegistry.load(SKILLS_ROOT)
    assert registry.get("coding-agents").requirements.empty
    assert registry.availability_for("coding-agents", ["coding-agents"]).state == "available"


def test_playwright_preserves_upstream_and_reads_every_reference(tmp_path: Path) -> None:
    package = SKILLS_ROOT / "playwright-cli"
    provenance = json.loads((package / "UPSTREAM.json").read_text(encoding="utf-8"))
    for relative, digest in provenance["upstream_sha256_lf"].items():
        content = (package / relative).read_text(encoding="utf-8").encode("utf-8")
        if relative == "SKILL.md":
            text = content.decode("utf-8")
            # The only local addition precedes the original Quick start section.
            start = text.index("## Using this Skill in vBot")
            end = text.index("## Quick start", start)
            content = (text[:start] + text[end:]).encode("utf-8")
        assert hashlib.sha256(content).hexdigest() == digest, relative
        document = package / relative
        for target in local_markdown_links(document):
            if target.endswith(".md"):
                assert (document.parent / target).is_file(), (relative, target)
    assert (package / "LICENSE").is_file()

    registry = SkillRegistry.load(SKILLS_ROOT, environment={"PATH": ""})
    assert registry.get("playwright-cli").requirements.empty
    assert registry.availability_for("playwright-cli", ["playwright-cli"]).state == "available"
    tool = SkillTool(tmp_path, registry)
    activated = tool.call({"name": "playwright-cli"})
    expected = sorted(
        path.relative_to(package).as_posix() for path in (package / "references").glob("*.md")
    )
    expected.extend(["LICENSE", "UPSTREAM.json"])
    assert activated["data"]["status"] == "loaded"
    assert activated["data"]["resource_files"]["files"] == expected
    assert_files_readable_through_the_tool(tool, "playwright-cli", expected)


def test_vbot_cli_exposes_extension_templates_without_loading_their_skill(
    tmp_path: Path,
) -> None:
    package = SKILLS_ROOT / "vbot-cli"
    for relative in (
        "SKILL.md",
        "references/extensions.md",
        "references/extension-pages.md",
        "references/extension-usage.md",
    ):
        for target in local_markdown_links(package / relative):
            resolved = (package / relative).parent.joinpath(target).resolve()
            assert resolved.is_relative_to(package), (relative, target)
            assert resolved.exists(), (relative, target)

    registry = SkillRegistry.load(SKILLS_ROOT)
    # The nested template Skill is a resource, not a discovered Skill.
    assert "workflow" not in {skill.name for skill in registry.list_all()}
    tool = SkillTool(tmp_path, registry)
    activated = tool.call({"name": "vbot-cli"})
    resources = [
        "references/extensions.md",
        "references/extension-pages.md",
        "references/extension-usage.md",
        "assets/extensions/guard_bash.py",
        "assets/extensions/word_count.py",
        "assets/extensions/workflow_command/extension.py",
        "assets/extensions/workflow_command/extension.json",
        "assets/extensions/workflow_command/skills/workflow/SKILL.md",
    ]
    assert activated["data"]["status"] == "loaded"
    assert set(resources) <= set(activated["data"]["resource_files"]["files"])
    assert_files_readable_through_the_tool(tool, "vbot-cli", resources)


def test_retired_browser_is_preserved_outside_discovery_roots() -> None:
    assert not (PROJECT_ROOT / "resources/extensions/browser_use/extension.py").exists()
    with zipfile.ZipFile(PROJECT_ROOT / "archive/browser-use.zip") as archive:
        assert archive.testzip() is None
        assert {
            "resources/extensions/browser_use/extension.py",
            "resources/extensions/browser_use/runtime.py",
            "resources/extensions/browser_use/skills/browser-use/SKILL.md",
            "tests/resources/extensions/test_browser_use.py",
            "tests/resources/extensions/test_browser_runtime.py",
        } <= set(archive.namelist())
