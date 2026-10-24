"""Release signing isolates the private key from package construction and execution."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import release_assets, sign_packages

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_signing_covers_archive_bytes_with_the_installers_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    installer = tmp_path / "install.sh"
    installer.write_text(f'RELEASE_PUBLIC_KEY="{public}"\n', encoding="utf-8")
    monkeypatch.setenv(
        sign_packages.KEY_ENVIRONMENT, base64.b64encode(key.private_bytes_raw()).decode("ascii")
    )
    archives = sign_packages.ARCHIVES[platform]
    assert archives <= release_assets.expected_assets("1.2.3")
    for name in archives:
        # The signer must never unpack or run a package, even to inspect it.
        (tmp_path / name).write_bytes(b"opaque archive bytes\x00" + name.encode())
    unchanged = tmp_path / "vbot-release.json"
    unchanged.write_bytes(b"identity is forwarded unchanged")

    sign_packages.sign_packages(
        tmp_path, platform, public_key=sign_packages.release_public_key(installer)
    )

    for name in archives:
        archive = tmp_path / name
        signature = base64.b64decode(
            archive.with_suffix(".zip.sig").read_bytes().strip(), validate=True
        )
        key.public_key().verify(signature, hashlib.sha256(archive.read_bytes()).digest())
    assert unchanged.read_bytes() == b"identity is forwarded unchanged"


@pytest.mark.parametrize(
    "problem", ["missing_key", "wrong_key", "missing_archive", "extra_archive"]
)
def test_signing_refuses_before_writing_for_a_wrong_identity_or_archive_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    monkeypatch.setenv(
        sign_packages.KEY_ENVIRONMENT, base64.b64encode(key.private_bytes_raw()).decode("ascii")
    )
    for name in sign_packages.ARCHIVES["linux"]:
        (tmp_path / name).write_bytes(b"archive")
    if problem == "missing_key":
        monkeypatch.delenv(sign_packages.KEY_ENVIRONMENT)
    elif problem == "wrong_key":
        public = base64.b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        ).decode()
    elif problem == "missing_archive":
        (tmp_path / "vbot-linux-aarch64-server.zip").unlink()
    else:
        (tmp_path / "unexpected.zip").write_bytes(b"archive")

    with pytest.raises(ValueError):
        sign_packages.sign_packages(tmp_path, "linux", public_key=public)
    assert list(tmp_path.glob("*.sig")) == []


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_release_key_is_available_only_on_a_fresh_signing_runner(platform: str) -> None:
    workflow = yaml.safe_load(
        (ROOT / f".github/workflows/{platform}-package.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    assert sign_packages.KEY_ENVIRONMENT not in json.dumps(jobs["package"])
    signer = jobs["sign"]
    assert signer["needs"] == "package"
    secret_steps = [
        step for step in signer["steps"] if sign_packages.KEY_ENVIRONMENT in step.get("env", {})
    ]
    assert len(secret_steps) == 1
    [step] = secret_steps
    assert step["run"] == f"python -I scripts/sign_packages.py --dir assets --platform {platform}"
    assert step["if"] == "inputs.release_mode"
    # The signing job checks out no runtime modules, build scripts or package locks.
    [checkout] = [step for step in signer["steps"] if "checkout@" in step.get("uses", "")]
    assert set(checkout["with"]["sparse-checkout"].splitlines()) == {
        "scripts/sign_packages.py",
        "scripts/install.sh",
    }
    assert len(base64.b64decode(sign_packages.release_public_key(), validate=True)) == 32
