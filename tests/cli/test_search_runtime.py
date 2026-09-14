"""Provisioned search assets must be pinned, atomic, offline reusable and private."""

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest

from cli.search_runtime import provision_search_runtime
from core.tools._search_binary import binary_spec, require_binary


def asset(root: Path, executable: bytes = b"fixture") -> tuple[Path, bytes]:
    target = "x86_64-pc-windows-msvc"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ripgrep/rg.exe", executable)
        archive.writestr("../../outside.txt", "must never extract")
    data = buffer.getvalue()
    root.mkdir(parents=True, exist_ok=True)
    (root / "ripgrep.lock.json").write_text(
        json.dumps(
            {
                "version": "fixture",
                "artifacts": {
                    target: {
                        "url": "https://example.test/rg.zip",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "binary_sha256": hashlib.sha256(executable).hexdigest(),
                    }
                },
            }
        )
    )
    return binary_spec(root, target)[0], data


def test_download_is_verified_and_extracts_only_executable(tmp_path, respx_mock, monkeypatch):
    target, archive = asset(tmp_path / "resources")
    respx_mock.get("https://example.test/rg.zip").mock(
        return_value=httpx.Response(200, content=archive)
    )
    monkeypatch.setattr(
        "cli.search_runtime.subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    assert (
        provision_search_runtime(tmp_path / "resources", target="x86_64-pc-windows-msvc") == target
    )
    assert target.read_bytes() == b"fixture"
    assert not (tmp_path / "outside.txt").exists()
    assert not list(target.parent.glob(".ripgrep-*"))
    respx_mock.reset()
    assert (
        provision_search_runtime(tmp_path / "resources", target="x86_64-pc-windows-msvc") == target
    )
    assert not respx_mock.calls


def test_bad_archive_preserves_previous_binary(tmp_path, respx_mock):
    target, _ = asset(tmp_path / "resources")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    respx_mock.get("https://example.test/rg.zip").mock(
        return_value=httpx.Response(200, content=b"corrupt")
    )
    with pytest.raises(ValueError, match="integrity"):
        provision_search_runtime(tmp_path / "resources", target="x86_64-pc-windows-msvc")
    assert target.read_bytes() == b"old"


def test_tool_call_never_uses_path_fallback_or_download(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ValueError, match="Repair the vBot installation"):
        require_binary(tmp_path)


def test_shipped_lock_is_complete_and_private_engine_has_pcre2():
    engine = require_binary()
    from core.utils.processes import subprocess_creation_flags

    result = subprocess.run(
        [str(engine), "--pcre2-version"],
        capture_output=True,
        timeout=10,
        creationflags=subprocess_creation_flags(),
    )
    assert result.returncode == 0
    assert b"PCRE2" in result.stdout
