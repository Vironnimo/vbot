"""Release archives must be authenticated and exact before immutable staging."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cli.application.packages import stage_package, validate_release
from cli.application.state import ApplicationError, Installation

_VERSION = "rel_example"


def _install(root: Path, *, shape: str = "server", public_key: str = "") -> Installation:
    return Installation(
        root,
        shape,
        None if shape == "desktop-client" else "127.0.0.1",
        None if shape == "desktop-client" else 8420,
        None if shape == "desktop-client" else str((root / "data").resolve()),
        release_public_key=public_key,
    )


def _payload(*, shape: str = "server", marker: bytes = b"runtime") -> dict[str, bytes]:
    payload = {
        "app/cli/main.py": b"cli entry\n",
        "app/pyproject.toml": b"[project]\nname = 'vbot'\n",
        "runtime/vBot.Python.exe": marker,
    }
    if shape != "desktop-client":
        payload.update(
            {
                "app/server/main.py": b"server entry\n",
                "app/webui/dist/index.html": b"<!doctype html>\n",
            }
        )
    return payload


def _archive(
    path: Path,
    *,
    shape: str = "server",
    version: str = _VERSION,
    marker: bytes = b"runtime",
    additions: dict[str, bytes] | None = None,
    symlink: str | None = None,
    hash_override: dict[str, str] | None = None,
) -> Path:
    payload = _payload(shape=shape, marker=marker)
    files = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    manifest: dict[str, object] = {
        "schema_version": 1,
        "bootstrap_protocol": 1,
        "version_id": version,
        "install_shape": shape,
        "platform": "windows-x86_64",
        "files": files,
    }
    if hash_override:
        files.update(hash_override)
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in payload.items():
            bundle.writestr(name, data)
        if additions:
            for name, data in additions.items():
                bundle.writestr(name, data)
        if symlink:
            entry = zipfile.ZipInfo(symlink)
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(entry, b"target")
        bundle.writestr("release.json", json.dumps(manifest))
    return path


def _sign(archive: Path, private_key: Ed25519PrivateKey) -> str:
    with archive.open("rb") as source:
        archive_digest = hashlib.file_digest(source, "sha256").hexdigest()
    signature = private_key.sign(bytes.fromhex(archive_digest))
    archive.with_suffix(archive.suffix + ".sig").write_bytes(base64.b64encode(signature))
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(public).decode("ascii")


def test_signed_complete_release_stages_for_the_matching_installation(tmp_path: Path):
    archive = _archive(tmp_path / "release.zip")
    key = Ed25519PrivateKey.generate()
    install = _install(tmp_path / "install", public_key=_sign(archive, key))

    assert stage_package(install, archive) == _VERSION
    assert (
        install.root / "versions" / _VERSION / "app" / "webui" / "dist" / "index.html"
    ).is_file()


@pytest.mark.parametrize("change", ["tamper", "extra", "missing", "link"])
def test_revalidation_checks_payload_bytes_and_rejects_links(tmp_path, change):
    install = _install(tmp_path / "install")
    stage_package(install, _archive(tmp_path / "release.zip"), local=True)
    root = install.version(_VERSION)
    target = root / "app" / "cli" / "main.py"
    if change == "tamper":
        target.write_bytes(b"changed")
    elif change == "extra":
        (root / "app" / "extra.py").write_bytes(b"extra")
    elif change == "missing":
        target.unlink()
    else:
        outside = tmp_path / "outside.py"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("symlink privilege unavailable")
    with pytest.raises(ApplicationError):
        validate_release(root, shape="server")


def test_remote_archives_require_a_valid_signature(tmp_path: Path):
    archive = _archive(tmp_path / "release.zip")
    install = _install(tmp_path / "install", public_key=base64.b64encode(b"x" * 32).decode())

    with pytest.raises(ApplicationError, match="signature"):
        stage_package(install, archive)
    archive.with_suffix(".zip.sig").write_bytes(b"not-a-signature")
    with pytest.raises(ApplicationError, match="signature"):
        stage_package(install, archive)


@pytest.mark.parametrize(
    ("additions", "symlink", "message"),
    [
        ({"../escape": b"x"}, None, "Unsafe"),
        ({"app/cli/main.py": b"duplicate"}, None, "Duplicate"),
        ({"app/CLI/main.py": b"case"}, None, "Duplicate"),
        ({"unexpected.txt": b"x"}, None, "inventory"),
        (None, "app/link", "special"),
    ],
)
def test_local_archive_rejects_unsafe_or_non_exact_payloads(
    tmp_path: Path, additions: dict[str, bytes] | None, symlink: str | None, message: str
):
    archive = _archive(tmp_path / "release.zip", additions=additions, symlink=symlink)

    with pytest.raises(ApplicationError, match=message):
        stage_package(_install(tmp_path / "install"), archive, local=True)


def test_manifest_hash_and_shape_are_checked_for_local_archives(tmp_path: Path):
    archive = _archive(
        tmp_path / "release.zip",
        marker=b"first",
        hash_override={"runtime/vBot.Python.exe": "0" * 64},
    )
    with pytest.raises(ApplicationError, match="verification"):
        stage_package(_install(tmp_path / "install"), archive, local=True)

    mismatch = _archive(tmp_path / "client.zip", shape="desktop-client")
    with pytest.raises(ApplicationError, match="shape"):
        stage_package(_install(tmp_path / "other"), mismatch, local=True)


@pytest.mark.parametrize("protocol", [None, True, 2])
def test_release_rejects_missing_mistyped_or_incompatible_bootstrap_protocol(
    tmp_path: Path, protocol: object
) -> None:
    root = tmp_path / "release"
    payload = _payload()
    for name, content in payload.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "version_id": _VERSION,
        "install_shape": "server",
        "platform": "windows-x86_64",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
    }
    if protocol is not None:
        manifest["bootstrap_protocol"] = protocol
    (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ApplicationError, match="bootstrap|manifest"):
        validate_release(root)


def test_same_identity_is_idempotent_only_for_the_exact_same_payload(tmp_path: Path):
    install = _install(tmp_path / "install")
    first = _archive(tmp_path / "first.zip", marker=b"first")
    same = _archive(tmp_path / "same.zip", marker=b"first")
    changed = _archive(tmp_path / "changed.zip", marker=b"changed")

    assert stage_package(install, first, local=True) == _VERSION
    assert stage_package(install, same, local=True) == _VERSION
    with pytest.raises(ApplicationError, match="different payload"):
        stage_package(install, changed, local=True)


def test_only_explicit_local_mode_allows_an_unsigned_archive(tmp_path: Path):
    archive = _archive(tmp_path / "release.zip")
    install = _install(tmp_path / "install")

    with pytest.raises(ApplicationError, match="signature"):
        stage_package(install, archive)
    assert stage_package(install, archive, local=True) == _VERSION
