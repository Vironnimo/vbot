"""Provisioned search assets must be pinned, atomic, offline reusable and private."""

import hashlib
import io
import json
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cli.search_runtime import provision_search_runtime
from core.utils.search_binary import binary_spec, require_binary


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


def test_download_is_verified_and_extracts_only_executable(tmp_path, monkeypatch):
    target, archive = asset(tmp_path / "resources")
    downloads = []

    def download(*args, **kwargs):
        downloads.append(args)
        return io.BytesIO(archive)

    monkeypatch.setattr("urllib.request.OpenerDirector.open", download)
    monkeypatch.setattr(
        "cli.search_runtime.subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    assert (
        provision_search_runtime(tmp_path / "resources", target="x86_64-pc-windows-msvc") == target
    )
    assert target.read_bytes() == b"fixture"
    assert not (tmp_path / "outside.txt").exists()
    assert not list(target.parent.glob(".ripgrep-*"))
    downloads.clear()
    assert (
        provision_search_runtime(tmp_path / "resources", target="x86_64-pc-windows-msvc") == target
    )
    assert not downloads


def test_bad_archive_preserves_previous_binary(tmp_path, monkeypatch):
    target, _ = asset(tmp_path / "resources")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", lambda *a, **k: io.BytesIO(b"corrupt")
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


def test_clean_builder_downloads_asset_without_site_packages(tmp_path):
    # Exercise the actual packaging entry point, including a missing asset and
    # an HTTP redirect, in the stdlib-only interpreter used by clean CI builds.
    executable = require_binary().read_bytes()
    root = tmp_path / "source"
    resources = root / "resources"
    target, archive = asset(resources, executable)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/redirect.zip":
                self.send_response(302)
                self.send_header("Location", "/rg.zip")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(archive)))
                self.end_headers()
                self.wfile.write(archive)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manifest_path = resources / "ripgrep.lock.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"]["x86_64-pc-windows-msvc"]["url"] = (
            f"http://127.0.0.1:{server.server_port}/redirect.zip"
        )
        manifest_path.write_text(json.dumps(manifest))
        from cli.application.payload import app_paths

        for relative in app_paths("server"):
            item = root / relative
            if item.exists():
                continue
            item.parent.mkdir(parents=True, exist_ok=True)
            if item.suffix:
                item.write_text("fixture")
            else:
                item.mkdir()
        destination = tmp_path / "app"
        probe = (
            "import sys; from pathlib import Path; "
            "from cli.application.payload import copy_application; "
            "copy_application(Path(sys.argv[1]), Path(sys.argv[2]), 'server')"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", probe, str(root), str(destination)],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        installed = destination / target.relative_to(root)
        assert installed.read_bytes() == executable
        assert requests == ["/redirect.zip", "/rg.zip"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
