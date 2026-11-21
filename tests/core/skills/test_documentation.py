"""The composed vbot-docs package exposes only declared, readable usage guides."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from core.extensions import ExtensionDocumentation, ExtensionManifest, ExtensionRecord
from core.skills._documentation import clear_documentation_cache, compose_vbot_docs
from core.skills.skills import SkillRegistry, scan_skill_resources


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "resources" / "skills" / "vbot-docs"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: vbot-docs\ndescription: App documentation.\n---\n"
        "Read references/extension-index.md for Extension usage.\n",
        encoding="utf-8",
    )
    (root / "references").mkdir()
    (root / "references" / "core.md").write_text("Core guide.", encoding="utf-8")
    return root


def _extension(tmp_path: Path, name: str = "demo") -> ExtensionRecord:
    root = tmp_path / "extensions" / name
    root.mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "usage.md").write_text("Usage sentinel.", encoding="utf-8")
    (root / "private.txt").write_text("Undeclared sentinel.", encoding="utf-8")
    return ExtensionRecord(
        name=name,
        root_path=root,
        entry_path=root / "extension.py",
        status="loaded",
        manifest=ExtensionManifest(
            description="Demo description.",
            documentation=(ExtensionDocumentation("docs/usage.md", "Usage", "Use the CLI."),),
        ),
    )


def test_composed_package_keeps_static_entry_and_links_each_declared_guide(tmp_path: Path) -> None:
    source = _source(tmp_path)
    records = [_extension(tmp_path, name) for name in ("active_ext", "disabled", "failed")]
    records[1].status = "disabled"
    records[2].status = "failed"
    extra = records[0].root_path / "docs" / "advanced  topic.md"
    extra.write_text("Advanced sentinel.", encoding="utf-8")
    records[0].manifest = ExtensionManifest(
        documentation=(
            ExtensionDocumentation("docs/usage.md", "Usage", "Use the CLI."),
            ExtensionDocumentation("docs/advanced  topic.md", "Advanced", "For advanced tasks."),
        )
    )
    overridden = _extension(tmp_path, "hidden")
    overridden.status = "overridden"
    package = compose_vbot_docs(source, tmp_path / "cache", [*records, overridden])
    assert package is not None
    assert SkillRegistry.load(package).get("vbot-docs").path == package / "SKILL.md"
    assert (package / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    assert (package / "references" / "core.md").read_text() == "Core guide."
    index_path = package / "references" / "extension-index.md"
    index = index_path.read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)]+)\)", index)
    assert len(links) == 4
    labels = re.findall(r"\[(`+)(.*?)\1\]\(([^)]+)\)", index)
    assert len(labels) == 4
    for _, label, href in labels:
        assert label == "references/" + unquote(href)
    assert {(index_path.parent / unquote(link)).read_text() for link in links} == {
        "Usage sentinel.",
        "Advanced sentinel.",
    }
    assert "disabled" in index and "failed" in index
    resources = scan_skill_resources(package)
    assert len([name for name in resources if name.startswith("references/extensions/")]) == 4
    assert not any("private.txt" in name or "hidden" in name for name in resources)
    assert not (source / "references" / "extension-index.md").exists()


@pytest.mark.parametrize("problem", ["missing", "encoding", "directory", "link"])
def test_unusable_guide_is_reported_without_copying_or_hiding_healthy_guides(
    tmp_path: Path, problem: str
) -> None:
    source = _source(tmp_path)
    record = _extension(tmp_path)
    path = record.root_path / "docs" / "usage.md"
    path.unlink()
    if problem == "encoding":
        path.write_bytes(b"\xff\xfe")
    elif problem == "directory":
        path.mkdir()
    elif problem == "link":
        outside = tmp_path / "outside.md"
        outside.write_text("Outside sentinel.", encoding="utf-8")
        try:
            path.symlink_to(outside)
        except OSError:
            pytest.skip("Symbolic links are unavailable")
    healthy = _extension(tmp_path, "healthy")
    package = compose_vbot_docs(source, tmp_path / "cache", [record, healthy])
    assert package is not None
    assert not (package / "references/extensions/demo/docs/usage.md").exists()
    assert (package / "references/extensions/healthy/docs/usage.md").is_file()
    index = (package / "references/extension-index.md").read_text()
    assert "demo" in index and "Unavailable" in index
    assert "Outside sentinel" not in index


def test_reload_publishes_immutable_packages_and_startup_discards_only_cache(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    record = _extension(tmp_path)
    cache = tmp_path / "cache"
    first = compose_vbot_docs(source, cache, [record])
    assert first is not None
    assert compose_vbot_docs(source, cache, [record]) == first
    record.status = "disabled"
    (record.root_path / "docs/usage.md").write_text("Changed sentinel.", encoding="utf-8")
    second = compose_vbot_docs(source, cache, [record])
    assert second is not None and second != first
    assert (first / "references/extensions/demo/docs/usage.md").read_text() == "Usage sentinel."
    assert (second / "references/extensions/demo/docs/usage.md").read_text() == "Changed sentinel."
    removed = compose_vbot_docs(source, cache, [])
    assert removed is not None and removed != second
    assert not (removed / "references/extensions/demo").exists()
    unrelated = cache / "keep.txt"
    unrelated.write_text("Keep sentinel.", encoding="utf-8")
    document = removed / "SKILL.md"
    original = document.read_bytes()
    original_time = document.stat().st_mtime_ns
    assert compose_vbot_docs(source, cache, [], bootstrap=True) == removed
    assert document.stat().st_mtime_ns == original_time
    assert not first.exists() and not second.exists()
    document.write_text("stale", encoding="utf-8")
    (removed / "extra.md").write_text("unexpected", encoding="utf-8")
    assert compose_vbot_docs(source, cache, [], bootstrap=True) == removed
    assert document.read_bytes() == original
    assert not (removed / "extra.md").exists()
    clear_documentation_cache(cache)
    assert not removed.exists()
    assert unrelated.read_text() == "Keep sentinel."
    assert (source / "SKILL.md").is_file()


def test_case_conflicting_extension_paths_cannot_overwrite_a_guide(tmp_path: Path) -> None:
    source = _source(tmp_path)
    first = _extension(tmp_path, "demo")
    second = _extension(tmp_path / "other", "DEMO")
    (second.root_path / "docs/usage.md").write_text("Other sentinel.", encoding="utf-8")
    package = compose_vbot_docs(source, tmp_path / "cache", [first, second])
    assert package is not None
    paths = list((package / "references/extensions").rglob("usage.md"))
    assert len(paths) == 1
    assert "Unavailable" in (package / "references/extension-index.md").read_text()
