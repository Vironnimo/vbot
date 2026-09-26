from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from cli.application.install import install_payload
from cli.application.integration import CheckoutTransition
from cli.application.state import ApplicationError
from cli.install_state import build_install_state, write_install_state


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

    assert (root / "vBot.GUI.exe").read_bytes() == b"gui-bootstrap"
    saved = json.loads((root / "application.json").read_text(encoding="utf-8"))
    assert saved["release_public_key"] == public_key
    assert install.version().name == "v1_test"


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
    monkeypatch.setattr(
        "cli.application.integration.prepare_checkout_transition", unexpected_mutation
    )

    with pytest.raises(ApplicationError):
        install_payload(
            root,
            payload,
            shape="server",
            data_dir=tmp_path / "data",
            from_checkout=tmp_path / "checkout",
        )

    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == original_files
    assert not (root / "application.json").exists()


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


def _checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    remote = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    (checkout / "pyproject.toml").write_text('[project]\nname="vbot"\nversion="1.0"\n')
    (checkout / ".gitignore").write_text(".vbot-install.json\n.venv/\n")
    git("add", ".")
    git("commit", "-m", "fixture")
    git("remote", "add", "origin", str(remote))
    git("push", "-u", "origin", "main")
    state = build_install_state(
        checkout,
        install_shape="server",
        dependency_groups=("server", "cli"),
        python_executable=str(checkout / ".venv/Scripts/python.exe"),
        server_host="127.0.0.1",
        server_port=9320,
        server_data_directory=str(tmp_path / "source-data"),
    )
    write_install_state(checkout, state)
    monkeypatch.setattr(
        "cli.application.integration.prepare_checkout_transition",
        lambda source, shape: CheckoutTransition(state),
    )
    monkeypatch.setattr(
        "cli.application.integration.finish_checkout_transition", lambda *args: None
    )
    return checkout


def test_transition_to_separate_native_root_preserves_main_tracking(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path, monkeypatch)
    source = (checkout / "pyproject.toml").read_bytes()
    state = (checkout / ".vbot-install.json").read_bytes()
    install = install_payload(
        tmp_path / "application", _payload(tmp_path), shape="server", from_checkout=checkout
    )
    assert install.server_port == 9320
    assert (checkout / "pyproject.toml").read_bytes() == source
    assert (checkout / ".vbot-install.json").read_bytes() == state
    binding = json.loads((install.root / "source-update.json").read_text())
    assert binding["branch"] == "main"
    assert binding["remote"] == "origin"
    assert Path(binding["checkout"]) == checkout.resolve()
    assert not (checkout / "application.json").exists()


def test_nonempty_checkout_remains_protected(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path, monkeypatch)
    with pytest.raises(ApplicationError):
        install_payload(checkout, _payload(tmp_path), shape="server", from_checkout=checkout)
    assert not (checkout / "application.json").exists()
