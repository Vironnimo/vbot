"""Selection preserves search scope while avoiding irrelevant directory trees."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.tools import _search_selection
from core.tools.search_files import register_search_files_tool
from core.tools.tools import ToolRegistry
from tests.core.tools.test_search_files import context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("globs", "expected", "must_visit"),
    [
        (["wanted/**"], ["wanted/.hidden.rs", "wanted/deep/a.rs"], {"wanted/deep"}),
        (["wanted/*.rs"], ["wanted/.hidden.rs"], set()),
        (["{wanted,other}/deep/*.rs"], ["other/deep/b.rs", "wanted/deep/a.rs"], {"other/deep"}),
        (["**/deep/*.rs"], ["other/deep/b.rs", "wanted/deep/a.rs"], {"other", "wanted/deep"}),
        (
            ["wanted/**", "!wanted/deep/**", "wanted/deep/a.rs"],
            ["wanted/.hidden.rs", "wanted/deep/a.rs"],
            {"wanted/deep"},
        ),
        (["WANTED/**"], ["wanted/.hidden.rs", "wanted/deep/a.rs"], {"wanted/deep"}),
        (["./*.rs"], ["top.rs"], set()),
    ],
)
async def test_rooted_filters_prune_only_impossible_subtrees(
    tmp_path, monkeypatch, globs, expected, must_visit
):
    files = {
        "wanted/.hidden.rs": "needle\n",
        "wanted/deep/a.rs": "needle\n",
        "wanted/ignored.rs": "needle\n",
        "wanted/.git/config": "needle\n",
        "wanted/.gitignore": "ignored.rs\n",
        "other/deep/b.rs": "needle\n",
        "unrelated/deep/c.txt": "needle\n",
        "top.rs": "needle\n",
    }
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    visited = set()
    scandir = _search_selection.os.scandir

    def observe(path):
        if isinstance(path, (str, Path)) and Path(path).is_relative_to(tmp_path):
            visited.add(Path(path).relative_to(tmp_path).as_posix())
        return scandir(path)

    monkeypatch.setattr(_search_selection.os, "scandir", observe)
    registry = ToolRegistry()
    register_search_files_tool(registry)
    result = await registry.dispatch(
        context(tmp_path), {"pattern": "needle", "glob": globs, "output": "files"}
    )

    assert result["ok"], result
    assert result["data"]["content"].splitlines() == expected
    assert result["data"]["complete"] is True
    assert must_visit <= visited
    assert "wanted/.git" not in visited
    if "**/deep/*.rs" not in globs:
        assert "unrelated" not in visited
    if globs == ["wanted/*.rs"]:
        assert "wanted/deep" not in visited


@pytest.mark.asyncio
async def test_basename_filters_still_find_deep_matches_and_explicit_ignored_roots(tmp_path):
    (tmp_path / ".gitignore").write_text("vendor/\n")
    target = tmp_path / "vendor/one/deep/file.rs"
    target.parent.mkdir(parents=True)
    target.write_text("needle\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)

    result = await registry.dispatch(
        context(tmp_path), {"pattern": "needle", "glob": "*.rs", "path": "vendor"}
    )

    assert result["data"]["content"] == "vendor/one/deep/file.rs:1:needle"
    assert result["data"]["complete"] is True


@pytest.mark.asyncio
async def test_pruning_preserves_selected_directories_and_overlapping_roots(tmp_path):
    (tmp_path / "wanted/empty").mkdir(parents=True)
    (tmp_path / "other/deep").mkdir(parents=True)
    registry = ToolRegistry()
    register_search_files_tool(registry)

    result = await registry.dispatch(
        context(tmp_path),
        {"args": ["--dirs", "--sort=path"], "glob": "wanted/", "path": [".", "."]},
    )

    assert result["data"]["content"] == "wanted/"
    assert result["data"]["complete"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DirEntry omits device identities")
@pytest.mark.asyncio
async def test_one_file_system_uses_real_device_identity_for_files_and_directories(
    tmp_path, monkeypatch
):
    for name in ["local.rs", "foreign.rs", "mount/nested.rs"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("needle\n")
    foreign_paths = {tmp_path / "foreign.rs", tmp_path / "mount"}
    original_stat = Path.stat

    def device_stat(path, **kwargs):
        info = original_stat(path, **kwargs)
        if path in foreign_paths:
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_dev"] += 1
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(Path, "stat", device_stat)
    registry = ToolRegistry()
    register_search_files_tool(registry)
    unrestricted = await registry.dispatch(
        context(tmp_path), {"glob": "*.rs", "args": ["--sort=path"]}
    )
    restricted = await registry.dispatch(
        context(tmp_path), {"glob": "*.rs", "args": ["--one-file-system"]}
    )

    assert unrestricted["data"]["content"] == "foreign.rs\nlocal.rs\nmount/nested.rs"
    assert restricted["data"]["content"] == "local.rs"
    assert restricted["data"]["complete"] is True


@pytest.mark.asyncio
async def test_timeout_offers_a_callable_directory_narrowing_step(tmp_path, monkeypatch):
    import core.tools.search as shared

    (tmp_path / "vendor/package/src").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    registry = ToolRegistry()
    register_search_files_tool(registry)
    with monkeypatch.context() as patch:
        patch.setattr(shared, "SEARCH_TIMEOUT_SECONDS", -1)
        timed_out = await registry.dispatch(
            context(tmp_path), {"pattern": "needle", "path": "vendor", "glob": "*.rs"}
        )

    assert timed_out["ok"]
    assert timed_out["data"]["complete"] is False
    assert timed_out["data"]["warnings"]
    recovery = await registry.dispatch(context(tmp_path), timed_out["data"]["narrow_call"])
    assert recovery["data"]["content"] == "vendor/package/"
    assert recovery["data"]["complete"] is True
