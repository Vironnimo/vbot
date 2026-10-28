from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from cli.application import install
from cli.application.install import install_payload
from cli.application.state import CHANNEL_URLS, ApplicationError, current_platform
from core.access.config import AccessConfig, access_config


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
        "server_access_protocol": 1,
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


@pytest.mark.parametrize("at_admission", [False, True])
@pytest.mark.parametrize("existing_install", [False, True])
def test_a_nonempty_destination_is_refused_before_any_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    at_admission: bool,
    existing_install: bool,
) -> None:
    destination = tmp_path / "checkout"
    payload = _payload(tmp_path, "desktop-client")
    previous = _payload(tmp_path / "previous", "desktop-client")
    manifest_path = previous / "release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version_id"] = "v0_existing"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original: dict[Path, bytes] = {}
    original_exclusive = install.exclusive

    def contents() -> dict[Path, bytes]:
        return {
            path.relative_to(destination): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file() and path.name != ".operation.lock"
        }

    def populate() -> None:
        if existing_install:
            with monkeypatch.context() as patch:
                patch.setattr(install, "exclusive", original_exclusive)
                install_payload(destination, previous, shape="desktop-client", channel="main")
        else:
            destination.mkdir(exist_ok=True)
            (destination / "pyproject.toml").write_text("[project]", encoding="utf-8")
        original.update(contents())

    @contextmanager
    def admission(root: Path):
        populate()
        with original_exclusive(root):
            yield

    if at_admission:
        monkeypatch.setattr(install, "exclusive", admission)
    else:
        populate()
    with pytest.raises(ApplicationError):
        install_payload(destination, payload, shape="desktop-client")

    assert contents() == original
    assert not (destination / "versions" / "v1_test").exists()


@pytest.mark.parametrize("relationship", ["same", "child", "parent", "normalized-child"])
def test_install_rejects_overlapping_data_before_mutations(
    tmp_path: Path, relationship: str
) -> None:
    root = tmp_path / "application"
    data = {
        "same": root,
        "child": root / "data",
        "parent": tmp_path,
        "normalized-child": tmp_path / "outside" / ".." / "application" / "data",
    }[relationship]
    payload = _payload(tmp_path)

    with pytest.raises(ApplicationError):
        install_payload(root, payload, shape="server", data_dir=data)

    assert not root.exists()
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    ("options", "host", "access"),
    [
        pytest.param([], "127.0.0.1", access_config("local"), id="default"),
        pytest.param(["--access", "lan"], "0.0.0.0", access_config("lan"), id="lan"),
        pytest.param(
            ["--access", "lan", "--host", "192.168.1.5", "--allow-network", "100.64.0.0/10"],
            "192.168.1.5",
            access_config("lan", allow_networks=["100.64.0.0/10"]),
            id="lan-with-vpn",
        ),
        # A host name alone already selects the default mode with that name accepted.
        pytest.param(
            ["--allow-host", "vbot.home.arpa"],
            "127.0.0.1",
            access_config("local", allow_hosts=["vbot.home.arpa"]),
            id="allowed-host-name",
        ),
        pytest.param(
            [
                "--access",
                "authenticated",
                "--public-url",
                "https://vbot.example.org",
                "--trusted-proxy",
                "127.0.0.1",
            ],
            "127.0.0.1",
            access_config(
                "authenticated",
                public_url="https://vbot.example.org",
                trusted_proxies=["127.0.0.1"],
            ),
            id="behind-proxy",
        ),
    ],
)
def test_installers_record_the_server_access_mode_and_its_bind_address(
    tmp_path: Path, options: list[str], host: str, access: AccessConfig
) -> None:
    root = tmp_path / "application"
    common = ["--root", str(root), "--payload", str(_payload(tmp_path)), "--shape", "server"]

    install.main([*common, "--data-dir", str(tmp_path / "data"), *options])

    saved = json.loads((root / "application.json").read_text(encoding="utf-8"))
    assert saved["server_host"] == host
    assert saved["server_access"] == access.to_record()


@pytest.mark.parametrize(
    ("shape", "options", "message"),
    [
        pytest.param(
            "server", ["--access", "local", "--host", "0.0.0.0"], "loopback", id="local-network"
        ),
        pytest.param("server", ["--access", "authenticated"], "--public-url", id="no-public-url"),
        pytest.param(
            "server",
            [
                "--access",
                "authenticated",
                "--public-url",
                "https://vbot.example.org",
                "--tls-cert",
                "missing-cert.pem",
                "--tls-key",
                "missing-key.pem",
            ],
            "does not exist",
            id="missing-certificate",
        ),
        pytest.param("desktop-client", ["--access", "lan"], "Desktop Client", id="client"),
    ],
)
def test_installers_refuse_an_access_mode_the_server_could_not_start_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    options: list[str],
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "application"
    common = ["--root", str(root), "--payload", str(_payload(tmp_path, shape)), "--shape", shape]

    with pytest.raises(ApplicationError, match=message):
        install.main([*common, "--data-dir", str(tmp_path / "data"), *options])

    assert not root.exists()


def test_authenticated_install_refuses_an_unmarked_payload_before_creating_files(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    manifest_path = payload / "release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["server_access_protocol"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    root, data = tmp_path / "installation", tmp_path / "data"
    with pytest.raises(ApplicationError):
        install_payload(
            root,
            payload,
            shape="server",
            data_dir=data,
            access=access_config(
                "authenticated", public_url="https://vbot.example", trusted_proxies=["127.0.0.1"]
            ),
        )
    assert not root.exists() and not data.exists()
