from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from cli.application.install import install_payload
from cli.application.integration import CheckoutTransition
from cli.install_state import build_install_state


def _payload(root: Path, shape: str = "server") -> Path:
    payload = root / "payload"
    files = {
        "app/cli/main.py": b"cli",
        "app/pyproject.toml": b"project",
        "runtime/vBot.Python.exe": b"python",
        "runtime/vBot.exe": b"bootstrap",
    }
    if shape != "desktop-client":
        files["app/server/main.py"] = b"server"
        files["app/webui/dist/index.html"] = b"webui"
    for name, content in files.items():
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "schema_version": 1,
        "bootstrap_protocol": 1,
        "version_id": "v1_test",
        "version": "1.0",
        "revision": "abcdef",
        "platform": "windows-x86_64",
        "install_shape": shape,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    (payload / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    return payload


def test_install_accepts_inno_registration_files_and_persists_actual_public_key(
    tmp_path: Path,
) -> None:
    root = tmp_path / "application"
    root.mkdir()
    (root / "vBot.exe").write_bytes(b"bootstrap")
    (root / "unins000.exe").write_bytes(b"uninstaller")
    (root / "unins000.dat").write_bytes(b"registration")
    public_key = base64.b64encode(b"k" * 32).decode("ascii")

    install = install_payload(
        root,
        _payload(tmp_path),
        shape="server",
        data_dir=tmp_path / "data",
        public_key=public_key,
    )

    saved = json.loads((root / "application.json").read_text(encoding="utf-8"))
    assert saved["release_public_key"] == public_key
    assert install.version().name == "v1_test"


def test_checkout_transition_preserves_recorded_shape_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    data = (tmp_path / "recorded-data").resolve()
    state = build_install_state(
        checkout,
        install_shape="server",
        dependency_groups=("server", "cli"),
        python_executable=str(checkout / ".venv/Scripts/python.exe"),
        server_host="127.0.0.7",
        server_port=9123,
        server_data_directory=str(data),
    )
    transition = CheckoutTransition(state)
    completed: list[CheckoutTransition] = []
    monkeypatch.setattr(
        "cli.application.integration.prepare_checkout_transition",
        lambda source, shape: transition,
    )
    monkeypatch.setattr(
        "cli.application.integration.finish_checkout_transition",
        lambda install, value: completed.append(value),
    )

    install = install_payload(
        tmp_path / "application",
        _payload(tmp_path),
        shape="server",
        host="wrong",
        port=9999,
        data_dir=tmp_path / "wrong-data",
        from_checkout=checkout,
    )

    assert (install.server_host, install.server_port, install.server_data_directory) == (
        "127.0.0.7",
        9123,
        str(data),
    )
    assert completed == [transition]
