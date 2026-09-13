"""Managed Extension dependencies activate only for their exact runtime."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.extensions import dependencies


def _runtime(version: Path, packages: list[dict[str, str]]) -> Path:
    runtime = version / "runtime"
    runtime.mkdir(parents=True)
    executable = runtime / "vBot.Server.exe"
    executable.write_text("", encoding="utf-8")
    (runtime / "vbot-runtime-inventory.json").write_text(
        json.dumps({"schema_version": 1, "packages": packages}), encoding="utf-8"
    )
    (version / "release.json").write_text(
        json.dumps(
            {
                "files": {
                    "runtime/python3.dll": "b" * 64,
                    "runtime/python314.dll": "a" * 64,
                }
            }
        ),
        encoding="utf-8",
    )
    return executable


def test_activate_adds_only_matching_site_without_processing_pth(
    tmp_path: Path, monkeypatch
) -> None:
    executable = _runtime(tmp_path / "versions" / "rel_a", [{"name": "Base.Pkg", "version": "1"}])
    runtime = dependencies.runtime_dependencies(executable.parent.parent)
    site = tmp_path / "data" / "extension-dependencies" / runtime.fingerprint / "deps_one" / "site"
    site.mkdir(parents=True)
    (site / "run_me.pth").write_text(
        "import builtins; builtins.extension_pth_ran = True", encoding="utf-8"
    )
    (site.parent.parent / "active.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "compatibility": runtime.fingerprint,
                "requirements": "example==1\n",
                "site_relative": "deps_one/site",
            }
        ),
        encoding="utf-8",
    )
    paths = list(sys.path)
    monkeypatch.setattr(dependencies.sys, "executable", str(executable))
    monkeypatch.setattr(dependencies.sys, "path", paths)

    dependencies.activate(tmp_path / "data")
    dependencies.activate(tmp_path / "data")

    assert paths.count(str(site)) == 1
    assert not hasattr(__import__("builtins"), "extension_pth_ran")


def test_activate_rejects_incompatible_or_linked_managed_records(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    executable = _runtime(tmp_path / "versions" / "rel_a", [{"name": "base", "version": "1"}])
    runtime = dependencies.runtime_dependencies(executable.parent.parent)
    base = tmp_path / "data" / "extension-dependencies" / runtime.fingerprint
    base.mkdir(parents=True)
    (base / "active.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "compatibility": "b" * 64,
                "requirements": "example==1\n",
                "site_relative": "deps_one/site",
            }
        ),
        encoding="utf-8",
    )
    paths = list(sys.path)
    monkeypatch.setattr(dependencies.sys, "executable", str(executable))
    monkeypatch.setattr(dependencies.sys, "path", paths)

    dependencies.activate(tmp_path / "data")

    assert not any("deps_one" in entry for entry in paths)
    assert "unavailable" in caplog.text


def test_source_checkout_mode_is_left_unchanged(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "python.exe"
    executable.write_text("", encoding="utf-8")
    paths = list(sys.path)
    monkeypatch.setattr(dependencies.sys, "executable", str(executable))
    monkeypatch.setattr(dependencies.sys, "path", paths)

    dependencies.activate(tmp_path / "data")

    assert paths == sys.path
