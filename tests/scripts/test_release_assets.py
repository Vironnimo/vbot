from __future__ import annotations

import json
from pathlib import Path

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
