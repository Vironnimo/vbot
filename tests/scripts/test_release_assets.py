from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cli.application.packages import MAX_IDENTITY_BYTES
from scripts import release_assets


def test_a_release_is_complete_only_with_every_package_and_its_identity(tmp_path: Path) -> None:
    names = release_assets.expected_assets("1.2.3")
    for name in names:
        (tmp_path / name).write_text("x", encoding="utf-8")
    identity = {"schema_version": 1, "version": "1.2.3", "revision": "abc", "channel": "main"}
    (tmp_path / release_assets.IDENTITY).write_text(json.dumps(identity), encoding="utf-8")
    arguments = ["--dir", str(tmp_path), "--version", "1.2.3", "--revision", "abc"]

    assert "vbot-linux-aarch64-server.zip.sig" in names
    assert "vBot-1.2.3-windows-x86_64-desktop-client.exe" in names
    assert release_assets.main([*arguments, "--channel", "main"]) == 0
    # A main build never passes as a release, and every package counts.
    assert release_assets.main([*arguments, "--channel", "release"]) == 1
    (tmp_path / "vbot-linux-x86_64-server.zip").unlink()
    assert release_assets.problems((path.name for path in tmp_path.iterdir()), "1.2.3") == [
        "missing asset vbot-linux-x86_64-server.zip"
    ]


def test_recorded_digests_cover_every_other_asset_within_the_updater_limit(
    tmp_path: Path,
) -> None:
    # The longest realistic identity: SemVer with two-digit parts, full revision.
    version, revision = "10.20.30", "f" * 40
    identity = {
        "schema_version": 1,
        "version_id": f"v10_20_30_{revision[:12]}",
        "version": version,
        "revision": revision,
        "channel": "release",
    }
    for index, name in enumerate(sorted(release_assets.expected_assets(version))):
        (tmp_path / name).write_bytes(bytes([index]) * (index + 1))
    path = tmp_path / release_assets.IDENTITY
    path.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    arguments = ["--dir", str(tmp_path), "--version", version, "--revision", revision]

    # Nothing is recorded for an incomplete or mismatching asset set.
    assert release_assets.main([*arguments, "--channel", "main", "--record-digests"]) == 1
    assert "assets" not in json.loads(path.read_text(encoding="utf-8"))
    assert release_assets.main([*arguments, "--channel", "release", "--record-digests"]) == 0

    recorded = json.loads(path.read_bytes())
    assert recorded == {
        **identity,
        "assets": {
            item.name: hashlib.sha256(item.read_bytes()).hexdigest()
            for item in tmp_path.iterdir()
            if item.name != release_assets.IDENTITY
        },
    }
    assert len(recorded["assets"]) == len(release_assets.expected_assets(version)) - 1
    # Installed updaters refuse a larger identity.
    assert release_assets.IDENTITY_LIMIT == MAX_IDENTITY_BYTES
    assert len(path.read_bytes()) <= MAX_IDENTITY_BYTES

    for index in range(40):
        (tmp_path / f"vbot-unexpected-{index:02}-{'x' * 40}.json").write_bytes(b"x")
    before = path.read_bytes()

    assert release_assets.main([*arguments, "--record-digests"]) == 1
    assert path.read_bytes() == before
