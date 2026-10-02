"""Release archives must be authenticated and exact before immutable staging."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cli.application import packages
from cli.application.packages import (
    RELEASE_IDENTITY_ASSET,
    digest_files,
    download_release,
    stage_package,
    validate_release,
)
from cli.application.state import (
    CHANNEL_URLS,
    ApplicationError,
    Installation,
    current_platform,
    package_name,
)
from tests.directory_links import cloud_placeholder_status, link_directory

_VERSION = "rel_example"
_CACHE = "app/cli/__pycache__/main.cpython-313.pyc"


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
    platform: str | None = None,
) -> Path:
    payload = _payload(shape=shape, marker=marker)
    files = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    manifest: dict[str, object] = {
        "schema_version": 1,
        "bootstrap_protocol": 1,
        "version_id": version,
        "install_shape": shape,
        "platform": platform or current_platform(),
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


@pytest.mark.parametrize("change", ["tamper", "extra", "missing", "link", "directory-link"])
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
    elif change == "directory-link":
        # A junction on Windows, which needs no privilege; it adds no payload file.
        (tmp_path / "outside").mkdir()
        link_directory(root / "app" / "linked", tmp_path / "outside")
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


def test_revalidation_treats_a_cloud_file_placeholder_as_an_ordinary_file(tmp_path, monkeypatch):
    install = _install(tmp_path / "install")
    stage_package(install, _archive(tmp_path / "release.zip"), local=True)
    root = install.version(_VERSION)
    placeholder = str(root / "app" / "cli" / "main.py")
    scandir = os.scandir

    class Entry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry

        def __getattr__(self, name: str) -> Any:
            return getattr(self._entry, name)

        def stat(self, *, follow_symlinks: bool = True) -> Any:
            status = self._entry.stat(follow_symlinks=follow_symlinks)
            if self._entry.path == placeholder and not follow_symlinks:
                return cloud_placeholder_status(status)
            return status

    @contextlib.contextmanager
    def placeholder_scandir(path: Any) -> Any:
        with scandir(path) as entries:
            yield [Entry(entry) for entry in entries]

    monkeypatch.setattr(packages.os, "scandir", placeholder_scandir)

    validate_release(root, shape="server")


def _staged(
    tmp_path: Path,
    *,
    additions: dict[str, bytes] | None = None,
    hash_override: dict[str, str] | None = None,
) -> Path:
    install = _install(tmp_path / "install")
    archive = _archive(tmp_path / "release.zip", additions=additions, hash_override=hash_override)
    stage_package(install, archive, local=True)
    return install.version(_VERSION)


def _write(root: Path, name: str, data: bytes = b"bytecode") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_concurrent_verification_names_the_first_changed_file_in_inventory_order(
    tmp_path: Path,
):
    root = _staged(tmp_path)
    (root / "app" / "webui" / "dist" / "index.html").write_bytes(b"changed")
    (root / "app" / "cli" / "main.py").write_bytes(b"changed")

    with pytest.raises(ApplicationError) as failure:
        validate_release(root, shape="server")

    assert str(failure.value).endswith(": app/cli/main.py")


def test_file_digests_keep_input_order_and_fail_on_an_unreadable_file(tmp_path: Path):
    paths = [_write(tmp_path, f"file{index}", bytes([index]) * index) for index in range(40)]

    assert digest_files(paths) == [
        hashlib.sha256(bytes([index]) * index).hexdigest() for index in range(40)
    ]
    with pytest.raises(FileNotFoundError):
        digest_files([*paths, tmp_path / "missing"])


def test_inventory_mismatch_names_a_bounded_sample_of_differing_paths(tmp_path: Path):
    root = _staged(tmp_path)
    for index in range(5):
        _write(root, f"app/extra{index}.py")
    (root / "app" / "cli" / "main.py").unlink()

    with pytest.raises(ApplicationError) as failure:
        validate_release(root, shape="server")

    assert str(failure.value) == (
        "Release file inventory does not match its payload (unexpected: app/extra0.py, "
        "app/extra1.py, app/extra2.py and 2 more; missing: app/cli/main.py)"
    )


def test_installed_version_removes_stray_bytecode_caches_before_verifying(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    root = _staged(tmp_path)
    caches = [
        _write(root, "runtime/Lib/encodings/__pycache__/aliases.cpython-313.pyc"),
        _write(root, "app/cli/__pycache__/main.cpython-313.opt-1.pyc"),
        # Temporary file of an interrupted atomic cache write.
        _write(root, _CACHE + ".2238104012345"),
    ]

    with pytest.raises(ApplicationError, match="unexpected: app/cli/__pycache__/"):
        validate_release(root, shape="server")
    assert all(cache.is_file() for cache in caches)

    with caplog.at_level(logging.WARNING, logger="vbot.application.packages"):
        manifest = validate_release(root, shape="server", remove_bytecode_caches=True)

    assert manifest["version_id"] == _VERSION
    assert not any(cache.exists() or cache.parent.exists() for cache in caches)
    assert "Removed 3 unverified bytecode cache files" in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        # Sourceless bytecode outside a cache directory is importable.
        "app/cli/main.pyc",
        "app/cli/__pycache__/main.py",
        _CACHE + ".tmp",
        "app/__pycache__.pyc",
    ],
)
def test_cache_removal_still_rejects_every_other_unverified_file(tmp_path: Path, name: str):
    root = _staged(tmp_path)
    cache = _write(root, _CACHE)
    unverified = _write(root, name, b"unverified")

    with pytest.raises(ApplicationError, match=rf"payload \(unexpected: {re.escape(name)}\)$"):
        validate_release(root, shape="server", remove_bytecode_caches=True)
    assert unverified.is_file()
    assert not cache.exists()


def test_inventoried_bytecode_is_verified_instead_of_removed(tmp_path: Path):
    root = _staged(
        tmp_path,
        additions={_CACHE: b"bytecode"},
        hash_override={_CACHE: hashlib.sha256(b"bytecode").hexdigest()},
    )
    _write(root, _CACHE, b"tampered")

    with pytest.raises(ApplicationError, match=f"verification failed: {re.escape(_CACHE)}"):
        validate_release(root, shape="server", remove_bytecode_caches=True)
    assert (root / _CACHE).read_bytes() == b"tampered"


def test_undeletable_bytecode_cache_fails_with_an_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _staged(tmp_path)
    cache = _write(root, _CACHE)
    unlink = Path.unlink

    def locked(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".pyc":
            raise PermissionError("in use")
        unlink(path, missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)

    with pytest.raises(ApplicationError, match="close programs that use this vBot version"):
        validate_release(root, shape="server", remove_bytecode_caches=True)
    assert cache.is_file()


def test_an_unsigned_or_forged_archive_stages_only_in_explicit_local_mode(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "release.zip")
    install = _install(tmp_path / "install", public_key=base64.b64encode(b"x" * 32).decode())

    with pytest.raises(ApplicationError, match="signature"):
        stage_package(install, archive)
    archive.with_suffix(".zip.sig").write_bytes(b"not-a-signature")
    with pytest.raises(ApplicationError, match="signature"):
        stage_package(install, archive)
    assert stage_package(install, archive, local=True) == _VERSION


@pytest.mark.parametrize(
    ("archive_options", "message"),
    [
        pytest.param({"additions": {"../escape": b"x"}}, "Unsafe", id="path-escape"),
        pytest.param({"additions": {"app/c:x.py": b"x"}}, "Unsafe", id="drive-colon"),
        *(
            pytest.param({"additions": {f"app/{name}": b"x"}}, "Reserved", id=f"reserved-{index}")
            for index, name in enumerate(["aux.py", "CONIN$", "lpt¹.log", "nul .txt", "a|b"])
        ),
        pytest.param({"additions": {"app/cli/main.py": b"duplicate"}}, "Duplicate", id="duplicate"),
        pytest.param({"additions": {"app/CLI/main.py": b"case"}}, "Duplicate", id="case-collision"),
        pytest.param({"additions": {"unexpected.txt": b"x"}}, "inventory", id="not-inventoried"),
        pytest.param({"symlink": "app/link"}, "special", id="link"),
        pytest.param(
            {"hash_override": {"runtime/vBot.Python.exe": "0" * 64}},
            "verification",
            id="digest-mismatch",
        ),
        pytest.param({"shape": "desktop-client"}, "shape", id="other-shape"),
        pytest.param({"platform": "linux-riscv64"}, "platform", id="other-platform"),
    ],
)
def test_local_archive_rejects_unsafe_or_non_exact_payloads(
    tmp_path: Path, archive_options: dict[str, Any], message: str
) -> None:
    archive = _archive(tmp_path / "release.zip", **archive_options)

    with pytest.raises(ApplicationError, match=message):
        stage_package(_install(tmp_path / "install"), archive, local=True)


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
        "platform": current_platform(),
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
    cache = _write(install.version(_VERSION), _CACHE)
    assert stage_package(install, same, local=True) == _VERSION
    assert not cache.exists()
    with pytest.raises(ApplicationError, match="different payload"):
        stage_package(install, changed, local=True)


_DOWNLOADS = "https://github.com/Vironnimo/vbot/releases"


@pytest.mark.parametrize(
    ("channel", "published", "version", "package_published"),
    [
        pytest.param("release", "rel_active", "1.0.0", True, id="already-active"),
        pytest.param("release", "rel_newer", "1.0.0", True, id="newer-release"),
        pytest.param("main", "rel_newer", "1.1.0.dev3", True, id="newer-main-build"),
        pytest.param("release", "rel_newer", "1.0.0", False, id="no-package"),
        # The version becomes the tag of the release download URL.
        pytest.param("release", "rel_newer", "1.0.0/../../x", True, id="unsafe-version"),
        # A release without the identity names no version and is never downloaded.
        pytest.param("release", None, "1.0.0", True, id="no-identity"),
    ],
)
def test_download_fetches_the_platform_package_only_for_a_version_not_yet_active(
    tmp_path: Path, channel: str, published: str | None, version: str, package_published: bool
) -> None:
    respx = pytest.importorskip("respx")
    install = replace(
        _install(tmp_path / "install", public_key="key"), release_url=CHANNEL_URLS[channel]
    )
    (install.root / "versions" / "rel_active").mkdir(parents=True)
    (install.root / "active-version").write_text("rel_active\n", encoding="ascii")
    archive = package_name("server")
    # Only these release downloads are mocked, so a GitHub API request fails. The
    # release channel downloads the packages from the tag the latest identity names,
    # so a release published meanwhile cannot mix in.
    identity_base, package_base = {
        "release": (f"{_DOWNLOADS}/latest/download", f"{_DOWNLOADS}/download/v{version}"),
        "main": (f"{_DOWNLOADS}/download/main-build", f"{_DOWNLOADS}/download/main-build"),
    }[channel]
    storage = "https://release-assets.example"
    identity = {"schema_version": 1, "version_id": published, "version": version}
    with respx.mock(assert_all_called=False) as router:
        identity_route = router.get(f"{identity_base}/{RELEASE_IDENTITY_ASSET}")
        if published:
            identity_route.respond(json=identity)
        else:
            identity_route.respond(404)
        # GitHub redirects every release download to its asset storage.
        for name in (archive, archive + ".sig"):
            route = router.get(f"{package_base}/{name}")
            if package_published:
                route.respond(302, headers={"Location": f"{storage}/{name}"})
            else:
                route.respond(404)
        package = router.get(f"{storage}/{archive}").respond(content=b"zip")
        router.get(f"{storage}/{archive}.sig").respond(content=b"sig")
        labels: list[str | None] = []

        if published is None or not package_published or "/" in version:
            with pytest.raises(
                ApplicationError, match="publishes no|No matching signed|no valid release version"
            ):
                download_release(install, "upd_test")
            assert not package.called
            return
        result = download_release(
            install, "upd_test", progress=lambda _message, label: labels.append(label)
        )

    assert labels == [version]
    if published == "rel_active":
        assert result is None
        assert not package.called
    else:
        assert result is not None and result.name == archive
        assert result.read_bytes() == b"zip"
        assert result.with_suffix(".zip.sig").read_bytes() == b"sig"
