from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from cli.application.install import install_payload
from cli.application.integration import CheckoutTransition
from cli.application.state import ApplicationError
from cli.install_state import build_install_state, write_install_state
from tests.cli.application.git_repositories import SourceRepositories


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


@pytest.mark.parametrize("track", ["main", "release"])
def test_checkout_transition_keeps_the_recorded_target_and_binds_only_main(
    tmp_path: Path,
    source_repositories: SourceRepositories,
    monkeypatch: pytest.MonkeyPatch,
    track: str,
) -> None:
    # A release source is no Git branch checkout: it keeps its target but binds no updates.
    checkout = source_repositories.checkout if track == "main" else tmp_path / "release-source"
    checkout.mkdir(exist_ok=True)
    state = build_install_state(
        checkout,
        install_shape="server",
        dependency_groups=("server", "cli"),
        python_executable=str(checkout / ".venv/Scripts/python.exe"),
        server_host="127.0.0.7",
        server_port=9123,
        server_data_directory=str((tmp_path / "recorded-data").resolve()),
    )
    write_install_state(checkout, state)
    completed: list[CheckoutTransition] = []
    monkeypatch.setattr(
        "cli.application.integration.prepare_checkout_transition",
        lambda source, shape: CheckoutTransition(state),
    )
    monkeypatch.setattr(
        "cli.application.integration.finish_checkout_transition",
        lambda install, value: completed.append(value),
    )
    checkout_files = {path.name: path.read_bytes() for path in checkout.iterdir() if path.is_file()}

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
        str((tmp_path / "recorded-data").resolve()),
    )
    assert len(completed) == 1
    assert {
        path.name: path.read_bytes() for path in checkout.iterdir() if path.is_file()
    } == checkout_files
    binding_path = install.root / "source-update.json"
    if track == "release":
        assert not binding_path.exists()
        return
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    assert (binding["remote"], binding["branch"]) == ("origin", "main")
    assert Path(binding["checkout"]) == checkout.resolve()


def test_a_nonempty_destination_is_refused_before_any_change(tmp_path: Path) -> None:
    destination = tmp_path / "checkout"
    destination.mkdir()
    (destination / "pyproject.toml").write_text("[project]", encoding="utf-8")

    with pytest.raises(ApplicationError, match="destination is not empty"):
        install_payload(destination, _payload(tmp_path), shape="server", data_dir=tmp_path / "data")

    assert [path.name for path in destination.iterdir()] == ["pyproject.toml"]
