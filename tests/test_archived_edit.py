"""The retired edit Tool's sources stay recoverable in an archive outside the build."""

import hashlib
import json
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_edit_archive_preserves_sources_outside_the_build():
    with zipfile.ZipFile(ROOT / "archive/edit.zip") as archive:
        manifest = json.loads(archive.read("MANIFEST.json"))
        assert {
            "core/tools/edit.py",
            "tests/core/tools/edit_helpers.py",
            "tests/core/tools/test_edit.py",
            "tests/core/tools/test_edit_batch.py",
            "tests/core/tools/test_edit_concurrency.py",
            "tests/core/tools/test_edit_file_integrity.py",
            ".vorch/domain-maps/tools/edit.md",
            "scripts/provider_probe/scenario_files.py",
            "scripts/provider_probe/workflow_tolerance.py",
        } <= manifest["sha256"].keys()
        for path, digest in manifest["sha256"].items():
            assert hashlib.sha256(archive.read(path)).hexdigest() == digest, path
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "/archive" in project["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
