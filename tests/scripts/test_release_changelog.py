"""The authored Markdown, published GitHub body and bounded asset stay in agreement."""

import json

import pytest

from core.utils import changelog
from scripts import release_changelog


@pytest.mark.parametrize(
    ("heading", "body", "valid"),
    [
        ("# v1.2.3 - 2024-02-29", "## Fixes\n- Restores access.", True),
        ("# v1.2.4 - 2024-02-29", "## Fixes\n- Fixed.", False),
        ("# v1.2.3 - 2025-02-29", "## Fixes\n- Fixed.", False),
        ("# v1.2.3 - 2024-02-29", "## Highlights\n\n## Fixes\n- Fixed.", False),
        ("# v1.2.3 - 2024-02-29", "## Fixes\n- Fixed.\n## Highlights\n- New.", False),
        ("# v1.2.3 - 2024-02-29", "## Other\n- Changed.", False),
    ],
)
def test_check_and_render_release(tmp_path, monkeypatch, capsys, heading, body, valid):
    monkeypatch.setattr(changelog, "DIRECTORY", tmp_path)
    source = f"{heading}\n\n{body}\n"
    (tmp_path / "1.2.3.md").write_text(source, encoding="utf-8")
    assert (release_changelog.main(["check", "1.2.3"]) == 0) is valid
    if valid:
        assert release_changelog.main(["notes", "1.2.3", "--previous", "v1.2.2"]) == 0
        assert (
            capsys.readouterr().out
            == source
            + "\n**Full Changelog**: https://github.com/Vironnimo/vbot/compare/v1.2.2...v1.2.3\n"
        )


def test_asset_contains_the_latest_fifty_releases_and_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(changelog, "DIRECTORY", tmp_path)
    for patch in range(52):
        version = f"1.2.{patch}"
        (tmp_path / f"{version}.md").write_text(
            f"# v{version} - 2026-11-20\n## Before you update\n- Take a snapshot.\n"
            "## Fixes\n- Restores access.\n",
            encoding="utf-8",
        )
    out = tmp_path / changelog.ASSET
    args = ["asset", "--out", str(out)]
    assert release_changelog.main(args) == 0
    original = out.read_bytes()
    asset = json.loads(original)
    assert asset["schema_version"] == 1
    assert [r["version"] for r in asset["releases"]] == [f"1.2.{n}" for n in range(51, 1, -1)]
    newest = asset["releases"][0]
    assert newest["before_update"] == "- Take a snapshot."
    assert newest["markdown"].startswith("## Before you update")
    with (tmp_path / "1.2.51.md").open("a", encoding="utf-8") as f:
        f.write("x" * changelog.MAX_BYTES)
    assert release_changelog.main(args) == 1
    assert out.read_bytes() == original
