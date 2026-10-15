"""Only the running server's own installation contributes application logs."""

from pathlib import Path
from typing import Literal

import pytest

from cli.application.state import Installation
from server import _log_sources


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source", "server", "other-data", "desktop-client"])
async def test_log_sources_follow_the_running_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["source", "server", "other-data", "desktop-client"],
) -> None:
    data, root = tmp_path / "data", tmp_path / "installation"
    (data / "logs").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "update.log").write_text("updated", encoding="utf-8")
    installation = (
        None
        if kind == "source"
        else Installation(
            root,
            "desktop-client" if kind == "desktop-client" else "server",
            "localhost",
            8420,
            str(tmp_path / "other-data" if kind == "other-data" else data),
        )
    )
    monkeypatch.setattr(_log_sources, "discover", lambda: installation)
    viewer = await _log_sources.build_log_viewer(data)
    try:
        assert (await viewer.list_files())["files"] == (
            ["application/update.log"] if kind == "server" else []
        )
    finally:
        await viewer.aclose()
