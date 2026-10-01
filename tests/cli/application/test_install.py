from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from cli.application.install import install_payload
from cli.application.state import CHANNEL_URLS, ApplicationError, current_platform


def _payload(root: Path, shape: str = "server") -> Path:
    payload = root / "payload"
    files = {
        "app/cli/main.py": b"cli",
        "app/pyproject.toml": b"project",
        "runtime/vBot.Python.exe": b"python",
        "runtime/vBot.exe": b"bootstrap",
        "runtime/vBot.GUI.exe": b"gui-bootstrap",
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
        "platform": current_platform(),
        "install_shape": shape,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    (payload / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    return payload


# A new data directory starts with the fresh-install Agent defaults; an existing one
# keeps its settings.
@pytest.mark.parametrize("existing_data", [False, True], ids=["new_data", "existing_data"])
@pytest.mark.parametrize("channel", ["release", "main"])
def test_install_accepts_inno_registration_files_and_persists_key_and_channel(
    tmp_path: Path, channel: str, existing_data: bool
) -> None:
    data = tmp_path / "data"
    if existing_data:
        data.mkdir()
        (data / "settings.json").write_text('{"format_version": 1}', encoding="utf-8")
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
        data_dir=data,
        public_key=public_key,
        channel=channel,
    )

    assert (root / "vBot.GUI.exe").read_bytes() == b"gui-bootstrap"
    saved = json.loads((root / "application.json").read_text(encoding="utf-8"))
    assert saved["release_public_key"] == public_key
    assert saved["release_url"] == CHANNEL_URLS[channel]
    assert install.channel == channel
    assert install.version().name == "v1_test"
    settings = json.loads((data / "settings.json").read_text(encoding="utf-8"))
    expected = {} if existing_data else {"agent": {"thinking_effort": "high"}}
    assert settings.get("defaults", {}) == expected
    assert settings["format_version"] == 1


@pytest.mark.parametrize("relationship", ["same", "parent", "child", "normalized_child"])
def test_install_rejects_overlapping_payload_before_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relationship: str
) -> None:
    payload = _payload(tmp_path)
    root = {
        "same": payload,
        "parent": tmp_path,
        "child": payload / "application",
        "normalized_child": payload / ".." / "payload" / "application",
    }[relationship]
    original_files = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def unexpected_mutation(*args: object, **kwargs: object) -> None:
        pytest.fail("Overlapping paths reached installation mutation")

    monkeypatch.setattr("cli.application.install.shutil.copytree", unexpected_mutation)

    with pytest.raises(ApplicationError):
        install_payload(
            root,
            payload,
            shape="server",
            data_dir=tmp_path / "data",
        )

    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == original_files
    assert not (root / "application.json").exists()


def test_a_nonempty_destination_is_refused_before_any_change(tmp_path: Path) -> None:
    destination = tmp_path / "checkout"
    destination.mkdir()
    (destination / "pyproject.toml").write_text("[project]", encoding="utf-8")

    with pytest.raises(ApplicationError, match="destination is not empty"):
        install_payload(destination, _payload(tmp_path), shape="server", data_dir=tmp_path / "data")

    assert [path.name for path in destination.iterdir()] == ["pyproject.toml"]
