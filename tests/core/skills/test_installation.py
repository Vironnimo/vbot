"""Whole-package import contracts, with independently authored source fixtures."""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from core.skills import SkillAuthoringError, SkillAuthoringService, SkillRegistry, _packages

DOCUMENT = b"---\nname: research\ndescription: Research a topic.\n---\n\nRead templates/guide.md.\n"
FILES = {
    "SKILL.md": DOCUMENT,
    "LICENSE.txt": b"License text\n",
    "templates/guide.md": b"A supporting guide.\n",
    "scripts/run.sh": b"#!/bin/sh\nexit 0\n",
    "assets/template.bin": bytes(range(256)),
}


def archive(
    tmp_path: Path, files: dict[str, bytes], *, prefix: str = "research/", tar: bool = False
) -> Path:
    path = tmp_path / ("bundle.tar.gz" if tar else "bundle.skill")
    if tar:
        with tarfile.open(path, "w:gz") as output:
            for name, content in files.items():
                info = tarfile.TarInfo(prefix + name)
                info.size = len(content)
                info.mode = 0o755 if name.endswith(".sh") else 0o644
                output.addfile(info, io.BytesIO(content))
    else:
        with zipfile.ZipFile(path, "w") as output:
            for name, content in files.items():
                zip_info = zipfile.ZipInfo(prefix + name)
                zip_info.external_attr = (
                    stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)
                ) << 16
                output.writestr(zip_info, content)
    return path


@pytest.mark.parametrize(
    "tar,prefix", [(False, "research/"), (False, ""), (True, "research/"), (True, "./")]
)
def test_installs_complete_package_without_rewriting_it(tmp_path, tar, prefix):
    source = archive(tmp_path, FILES, tar=tar, prefix=prefix)
    target = tmp_path / "skills"
    result = SkillAuthoringService().install(target, str(source))

    assert (result.name, result.operation, result.files) == ("research", "installed", len(FILES))
    assert result.warnings == []
    for name, content in FILES.items():
        assert (target / "research" / name).read_bytes() == content
    if os.name != "nt":
        assert (target / "research/scripts/run.sh").stat().st_mode & 0o111
    receipt = json.loads((target / "research/.vbot-install.json").read_bytes())
    assert receipt["source"] == source.as_posix()
    assert receipt["sha256"] == result.sha256
    assert receipt["installed_at"].endswith("+00:00")
    assert SkillRegistry.load(target).get("research").description == "Research a topic."


def test_directory_install_preserves_files_but_not_generated_state(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    for name, content in {
        **FILES,
        ".git/config": b"private",
        "node_modules/pkg/index.js": b"dependency",
    }.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    target = tmp_path / "skills"
    SkillAuthoringService().install(target, str(source))
    installed = target / "research"
    assert (installed / "templates/guide.md").is_file()
    assert not (installed / ".git").exists()
    assert not (installed / "node_modules").exists()
    (source / "templates/guide.md").write_text("local change")
    assert (installed / "templates/guide.md").read_bytes() == FILES["templates/guide.md"]


@pytest.mark.parametrize("compression", ["gz", "bz2", "xz"])
def test_compressed_tar_packages(tmp_path, compression):
    source = tmp_path / "package.tar"
    with tarfile.open(source, f"w:{compression}") as output:
        info = tarfile.TarInfo("SKILL.md")
        info.size = len(DOCUMENT)
        output.addfile(info, io.BytesIO(DOCUMENT))
    result = SkillAuthoringService().install(tmp_path / "skills", str(source))
    assert result.name == "research"


def test_tar_metadata_also_counts_toward_expanded_size_limit(tmp_path, monkeypatch):
    source = tmp_path / "package.tar.gz"
    with tarfile.open(source, "w:gz", pax_headers={"comment": "x" * 10000}) as output:
        info = tarfile.TarInfo("SKILL.md")
        info.size = len(DOCUMENT)
        output.addfile(info, io.BytesIO(DOCUMENT))
    monkeypatch.setattr(_packages, "MAX_PACKAGE_BYTES", 4096)
    with pytest.raises(SkillAuthoringError, match="uncompressed size limit"):
        SkillAuthoringService().install(tmp_path / "skills", str(source))
    assert not (tmp_path / "skills").exists()


@pytest.mark.skipif(os.name == "nt", reason="FIFO sources are POSIX-only")
def test_special_source_is_rejected_before_open(tmp_path, monkeypatch):
    source = tmp_path / "package.skill"
    os.mkfifo(source)

    def unexpected_open(*args, **kwargs):
        pytest.fail("A FIFO must never be opened as an archive")

    monkeypatch.setattr(Path, "open", unexpected_open)
    with pytest.raises(SkillAuthoringError, match="ordinary archive"):
        SkillAuthoringService().install(tmp_path / "skills", str(source))


def test_preview_and_ambiguous_source_never_write(tmp_path):
    source = archive(
        tmp_path,
        {
            "skills/a/SKILL.md": DOCUMENT,
            "skills/b/SKILL.md": DOCUMENT.replace(b"research", b"other"),
        },
        prefix="repo-main/",
    )
    authoring = SkillAuthoringService()
    target = tmp_path / "skills"
    preview = authoring.install(target, str(source), dry_run=True)
    assert preview.operation == "candidates"
    assert [(item["path"], item["name"]) for item in preview.candidates] == [
        ("skills/a", "research"),
        ("skills/b", "other"),
    ]
    with pytest.raises(SkillAuthoringError):
        authoring.install(target, str(source))
    assert not target.exists()
    result = authoring.install(target, str(source), path="skills/b")
    assert result.name == "other"
    assert not (target / "research").exists()


def test_nested_example_is_part_of_parent_skill(tmp_path):
    source = archive(
        tmp_path, {**FILES, "assets/example/SKILL.md": DOCUMENT.replace(b"research", b"example")}
    )
    result = SkillAuthoringService().install(tmp_path / "skills", str(source))
    assert result.name == "research"
    assert result.files == len(FILES) + 1


def test_repeat_is_unchanged_and_overwrite_requires_explicit_replace(tmp_path):
    source = archive(tmp_path, FILES)
    authoring = SkillAuthoringService()
    target = tmp_path / "skills"
    authoring.install(target, str(source))
    receipt = target / "research/.vbot-install.json"
    before = receipt.read_bytes()
    assert authoring.install(target, str(source)).operation == "unchanged"
    assert receipt.read_bytes() == before
    local = target / "research/templates/local.md"
    local.write_text("local learning")
    with pytest.raises(SkillAuthoringError):
        authoring.install(target, str(source))
    assert local.read_text() == "local learning"
    preview = authoring.install(target, str(source), dry_run=True)
    assert preview.operation == "preview"
    assert preview.candidates[0]["exists"] is True
    assert preview.candidates[0]["unchanged"] is False
    assert local.exists()
    assert authoring.install(target, str(source), replace=True).operation == "replaced"
    assert not local.exists()


@pytest.mark.parametrize(
    "bad",
    [
        "../escaped.txt",
        "/absolute.txt",
        "C:/escaped.txt",
        "folder\\escaped.txt",
        "assets/../escaped.txt",
        "assets/a:stream",
        "assets/NUL.txt",
        "assets/trailing.",
        "assets/trailing ",
        "assets//empty.txt",
    ],
)
def test_rejects_unsafe_archive_names_without_partial_writes(tmp_path, bad):
    source = archive(tmp_path, {"SKILL.md": DOCUMENT, bad: b"x"}, prefix="")
    if "\\" in bad:
        # ZipInfo normalizes separators when creating an archive on Windows.
        source.write_bytes(
            source.read_bytes().replace(bad.replace("\\", "/").encode(), bad.encode())
        )
    target = tmp_path / "skills"
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(target, str(source))
    assert not target.exists()
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize(
    "files",
    [
        {"SKILL.md": DOCUMENT, "assets/A.txt": b"1", "assets/a.txt": b"2"},
        {"SKILL.md": DOCUMENT, "assets/file": b"1", "assets/file/child": b"2"},
        {"SKILL.md": DOCUMENT, "assets/e\u0301.txt": b"1", "assets/\u00e9.txt": b"2"},
    ],
)
def test_rejects_cross_platform_path_collisions(tmp_path, files):
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(tmp_path / "skills", str(archive(tmp_path, files)))
    assert not (tmp_path / "skills").exists()


@pytest.mark.parametrize("tar", [False, True])
def test_rejects_archive_links(tmp_path, tar):
    source = archive(tmp_path, FILES, tar=tar)
    if tar:
        with tarfile.open(source, "w:gz") as output:
            info = tarfile.TarInfo("research/assets/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../outside"
            output.addfile(info)
    else:
        with zipfile.ZipFile(source, "a") as output:
            info = zipfile.ZipInfo("research/assets/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(info, "../../outside")
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(tmp_path / "skills", str(source))
    assert not (tmp_path / "skills").exists()


@pytest.mark.parametrize(
    "limit", ["MAX_PACKAGE_ENTRIES", "MAX_PACKAGE_BYTES", "MAX_FILE_BYTES", "MAX_DOWNLOAD_BYTES"]
)
def test_package_limits_are_enforced_before_publication(tmp_path, monkeypatch, limit):
    source = archive(tmp_path, FILES)
    monkeypatch.setattr(_packages, limit, 2)
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(tmp_path / "skills", str(source))
    assert not (tmp_path / "skills").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"\xff\xfe",
        b"---\nname: ../outside\ndescription: Bad\n---\nBody",
        b"---\nname: CON\ndescription: Bad\n---\nBody",
    ],
)
def test_invalid_document_leaves_existing_skill_untouched(tmp_path, content):
    target = tmp_path / "skills"
    service = SkillAuthoringService()
    service.install(target, str(archive(tmp_path, FILES)))
    source = archive(tmp_path, {"SKILL.md": content})
    with pytest.raises(SkillAuthoringError):
        service.install(target, str(source), replace=True)
    assert (target / "research/SKILL.md").read_bytes() == DOCUMENT


def test_failed_publication_restores_previous_package(tmp_path, monkeypatch):
    target = tmp_path / "skills"
    service = SkillAuthoringService()
    source = archive(tmp_path, FILES)
    service.install(target, str(source))
    original = (target / "research/.vbot-install.json").read_bytes()
    source = archive(tmp_path, {**FILES, "references/new.md": b"new"})
    rename = Path.rename

    def fail_publish(self, destination):
        if self.name == "package":
            raise OSError("publication failed")
        return rename(self, destination)

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(OSError):
        service.install(target, str(source), replace=True)
    assert (target / "research/.vbot-install.json").read_bytes() == original
    assert not (target / "research/references/new.md").exists()
    assert list(target.iterdir()) == [target / "research"]


def test_failed_restore_retains_old_package_for_recovery(tmp_path, monkeypatch):
    target = tmp_path / "skills"
    service = SkillAuthoringService()
    service.install(target, str(archive(tmp_path, FILES)))
    source = archive(tmp_path, {**FILES, "references/new.md": b"new"})
    rename = Path.rename

    def fail_publish_and_restore(self, destination):
        if self.name in {"package", "previous"}:
            raise OSError("file is locked")
        return rename(self, destination)

    monkeypatch.setattr(Path, "rename", fail_publish_and_restore)
    with pytest.raises(SkillAuthoringError):
        service.install(target, str(source), replace=True)
    backups = list(target.glob(".skill-install-*/previous/SKILL.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == DOCUMENT
    assert SkillRegistry.load(target).list_all() == []


def test_cleanup_failure_does_not_report_successful_publication_as_failed(tmp_path, monkeypatch):
    from core.skills import _installation

    source = archive(tmp_path, FILES)
    target = tmp_path / "skills"

    def fail_cleanup(*args, **kwargs):
        raise OSError("temporary file locked")

    monkeypatch.setattr(_installation.shutil, "rmtree", fail_cleanup)
    result = SkillAuthoringService().install(target, str(source))
    assert result.operation == "installed"
    assert result.warnings
    assert (target / "research/SKILL.md").read_bytes() == DOCUMENT


def test_protected_scope_and_non_package_collision_are_not_overwritten(tmp_path):
    source = archive(tmp_path, FILES)
    target = tmp_path / "skills"
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService([target]).install(target, str(source))
    collision = target / "research"
    collision.mkdir(parents=True)
    (collision / "user.txt").write_text("keep")
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(target, str(source), replace=True)
    assert (collision / "user.txt").read_text() == "keep"
