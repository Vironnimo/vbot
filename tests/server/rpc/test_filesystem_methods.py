"""The ``filesystem.list`` RPC: server directory listings for path pickers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from server.rpc.methods import dispatch_rpc


async def _call(params: dict[str, Any]) -> dict[str, Any]:
    return await dispatch_rpc(object(), {"method": "filesystem.list", "params": params})


@pytest.mark.asyncio
async def test_lists_places_and_directories_in_the_public_shape(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")

    places = await _call({})
    folders = await _call({"path": tmp_path.as_posix()})
    files = await _call({"path": "", "root": str(tmp_path), "include_files": True})

    assert places["ok"] is True
    assert set(places["result"]) == {"path", "parent", "entries", "truncated", "separator", "home"}
    assert folders["result"] == {
        "path": tmp_path.as_posix(),
        "parent": tmp_path.parent.as_posix(),
        "entries": [{"name": "src", "kind": "directory", "link": False, "hidden": False}],
        "truncated": False,
        "separator": places["result"]["separator"],
    }
    assert (files["result"]["path"], files["result"]["parent"]) == ("", None)
    assert [entry["name"] for entry in files["result"]["entries"]] == ["src", "README.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "code", "data"),
    [
        pytest.param({"path": "missing"}, "invalid_request", None, id="relative-without-root"),
        pytest.param({"path": ".."}, "invalid_request", None, id="outside-the-root"),
        pytest.param({"path": 3}, "invalid_request", None, id="path-not-a-string"),
        pytest.param({"include_files": "yes"}, "invalid_request", None, id="flag-not-a-bool"),
        pytest.param({"depth": 1}, "invalid_request", None, id="unknown-field"),
        pytest.param(
            {"path": "missing", "root": "<root>"},
            "domain_error",
            {"reason": "not_found"},
            id="missing-directory",
        ),
    ],
)
async def test_refusals_carry_a_stable_code_and_reason(
    tmp_path: Path, params: dict[str, Any], code: str, data: dict[str, str] | None
) -> None:
    if params.get("path") == ".." or params.get("root") == "<root>":
        params = {**params, "root": str(tmp_path)}

    response = await _call(params)

    assert response["ok"] is False
    assert response["error"]["code"] == code
    assert response["error"].get("data") == data
