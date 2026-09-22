"""Portable source validation rejects aliases before preparing an installation."""

import io
import os
import tarfile
import zipfile

import pytest

from core.skills import SkillAuthoringError, SkillAuthoringService
from core.skills._packages import PackageError, package_path


@pytest.mark.parametrize("name", ["COM¹.txt", "LPT².log", "COM³", "CONIN$", "CONOUT$", "CON .txt"])
def test_package_paths_reject_all_windows_device_aliases(name):
    with pytest.raises(PackageError):
        package_path(f"assets/{name}")


def test_directory_hard_link_is_rejected_before_target_creation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: Fixture.\n---\nBody\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("External content")
    os.link(outside, source / "linked.txt")
    target = tmp_path / "installed"
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(target, str(source))
    assert not target.exists()
    assert outside.read_text() == "External content"


def test_archive_reserved_device_alias_fails_before_target_creation(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: demo\ndescription: Fixture.\n---\nBody\n")
        archive.writestr("assets/COM¹.txt", b"payload")
    target = tmp_path / "installed"
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(target, "demo.skill", archive=stream.getvalue())
    assert not target.exists()


def test_tar_non_utf8_filename_is_rejected_as_a_package_error(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in {
            "SKILL.md": b"---\nname: demo\ndescription: Fixture.\n---\nBody\n",
            "assets/invalid-\udcff.bin": b"payload",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    target = tmp_path / "installed"
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(target, "demo.tar", archive=stream.getvalue())
    assert not target.exists()
