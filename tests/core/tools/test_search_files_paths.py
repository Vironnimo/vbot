"""Explicit search targets, approvals and unambiguous selection repairs."""

from __future__ import annotations

import os

import pytest

from core.permissions import (
    BoundaryContext,
    PermissionBoundary,
    PermissionDenied,
    normalize_permission_rules,
)
from tests.core.tools.search_files_test_support import dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["[id]", "a, b", "{a,b}"])
@pytest.mark.parametrize("file_root", [False, True])
async def test_approval_keeps_literal_special_names(tmp_path, name, file_root):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    target = outside / name
    target.mkdir(parents=True)
    (target / "f.txt").write_text("intended token")
    decoy = outside / "d"
    decoy.mkdir()
    (decoy / "f.txt").write_text("wrong token")
    if file_root:
        target = target / "f.txt"
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"read": "work_area"}),
        BoundaryContext(workdir=work, home=tmp_path / "home", data_dir=tmp_path / "data"),
    )
    approved = []

    async def approve(_context, _reason, requirements, _arguments, _cwd):
        approved.extend(requirements)
        return True

    async def refresh(_force):
        return boundary, {"computer_id": "server", "allowed_computer_ids": ["server"]}

    result = await dispatch(
        work,
        {"pattern": "token", "path": str(target)},
        permissions=boundary,
        approval_hook=approve,
        approval_refresh_hook=refresh,
    )
    assert result["ok"], result
    assert "intended token" in result["data"]["content"]
    assert "wrong token" not in result["data"]["content"]
    assert len(approved) == 1
    assert os.path.normcase(approved[0].target) == os.path.normcase(target.as_posix())


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["url", "quoted_url", "extended", "extended_slashes", "case"])
async def test_path_representations_search_the_named_file(tmp_path, spelling):
    if spelling != "url" and os.name != "nt":
        pytest.skip("Windows path grammar")
    folder = tmp_path / "src"
    folder.mkdir()
    target = folder / "a.py"
    target.write_text("token")
    path = {
        "url": target.as_uri(),
        "quoted_url": f'"{target.as_uri()}"',
        "extended": "\\\\?\\" + str(target),
        "extended_slashes": "//?/" + target.as_posix(),
        "case": "SRC/A.PY",
    }[spelling]
    result = await dispatch(tmp_path, {"pattern": "token", "path": path})
    assert result["ok"], result
    assert result["data"]["content"] == "src/a.py\n1:token"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows device namespace")
@pytest.mark.parametrize("restricted", [False, True])
async def test_extended_device_namespace_never_becomes_a_local_path(
    tmp_path, monkeypatch, restricted
):
    monkeypatch.chdir(tmp_path)
    decoy = tmp_path / "GLOBALROOT" / "Device" / "volume" / "f.txt"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("wrong token")
    boundary = PermissionBoundary.UNRESTRICTED
    if restricted:
        boundary = PermissionBoundary.build(
            normalize_permission_rules({"read": "work_area"}),
            BoundaryContext(workdir=tmp_path, home=tmp_path / "home", data_dir=tmp_path / "data"),
        )
    result = await dispatch(
        tmp_path,
        {"pattern": "token", "path": r"\\?\GLOBALROOT\Device\volume\f.txt"},
        permissions=boundary,
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert result["data"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "glob", ["*.py,*.md", "*.py, *.md", "*.py *.md", "*.{py,md}", ["*.py", "*.md"]]
)
@pytest.mark.parametrize("pattern", [None, "token"])
async def test_separate_glob_filters_select_their_union(tmp_path, glob, pattern):
    for name in ("a.py", "b.md", "c.txt"):
        (tmp_path / name).write_text("token")
    arguments = {"glob": glob, "args": ["--sort=path"]}
    if pattern:
        arguments["pattern"] = pattern
    result = await dispatch(tmp_path, arguments)
    assert result["ok"], result
    expected = "a.py\n1:token\n\nb.md\n1:token" if pattern else "a.py\nb.md"
    assert result["data"]["content"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("glob,name", [("*.py,*.md", "a.py,b.md"), ("*.py *.md", "a.py b.md")])
async def test_an_existing_glob_with_separators_keeps_its_literal_meaning(tmp_path, glob, name):
    for file in (name, "other.py", "other.md"):
        (tmp_path / file).write_text("token")
    result = await dispatch(tmp_path, {"glob": glob})
    assert result["ok"], result
    assert result["data"]["content"] == name


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["--files", "--dirs"])
async def test_empty_name_match_does_not_reinterpret_selected_globs(tmp_path, mode):
    for name in ("a.py,b.md", "other.py"):
        if mode == "--files":
            (tmp_path / name).write_text("token")
        else:
            (tmp_path / name).mkdir()
    result = await dispatch(
        tmp_path, {"glob": "*.py,*.md", "pattern": r"^other\.py$", "args": [mode]}
    )
    assert result["ok"], result
    assert "content" not in result["data"]


@pytest.mark.asyncio
async def test_glob_list_retry_preserves_directory_depth(tmp_path):
    (tmp_path / "one" / "deep.py").mkdir(parents=True)
    (tmp_path / "top.md").mkdir()
    result = await dispatch(tmp_path, {"glob": "*.py,*.md", "args": ["--dirs", "--max-depth=1"]})
    assert result["ok"], result
    assert result["data"]["content"] == "top.md/"


@pytest.mark.asyncio
async def test_absolute_globs_check_hard_refusals_before_requesting_approval(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    denied = tmp_path / "denied"
    denied.mkdir()
    boundary = PermissionBoundary.build(
        normalize_permission_rules(
            {"read": "work_area", "deny": [f"Read({denied.as_posix()}/**)"]}
        ),
        BoundaryContext(workdir=work, home=tmp_path / "home", data_dir=tmp_path / "data"),
    )

    async def approve(*_args):
        pytest.fail("hard-denied call requested approval")

    with pytest.raises(PermissionDenied) as refused:
        await dispatch(
            work,
            {
                "pattern": "token",
                "glob": [f"{outside.as_posix()}/*.txt", f"{denied.as_posix()}/*.txt"],
            },
            permissions=boundary,
            approval_hook=approve,
        )
    assert refused.value.refusal.reason == "deny_rule"


@pytest.mark.asyncio
@pytest.mark.parametrize("path_kind", ["relative", "absolute", "below"])
@pytest.mark.parametrize("restricted", [False, True])
async def test_wildcard_roots_keep_existing_directory_prefixes_literal(
    tmp_path, path_kind, restricted
):
    target = tmp_path / "[id]"
    target.mkdir()
    (target / "a.txt").write_text("intended token")
    decoy = tmp_path / "d"
    decoy.mkdir()
    (decoy / "a.txt").write_text("wrong token")
    work = tmp_path if path_kind == "below" else target
    path = {"relative": "*.txt", "absolute": f"{target.as_posix()}/*.txt", "below": "[id]/*.txt"}[
        path_kind
    ]
    boundary = PermissionBoundary.UNRESTRICTED
    if restricted:
        boundary = PermissionBoundary.build(
            normalize_permission_rules({"read": "work_area"}),
            BoundaryContext(workdir=work, home=tmp_path / "home", data_dir=tmp_path / "data"),
        )
    result = await dispatch(work, {"pattern": "token", "path": path}, permissions=boundary)
    assert result["ok"], result
    assert (
        result["data"]["content"]
        == ("[id]/" if path_kind == "below" else "") + "a.txt\n1:intended token"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("filter", [{"glob": "*.md"}, {"type": "md"}])
async def test_filters_on_explicit_files_report_their_lack_of_effect(tmp_path, filter):
    (tmp_path / "a.py").write_text("token")
    result = await dispatch(tmp_path, {"pattern": "token", "path": "a.py", **filter})
    assert result["ok"], result
    assert result["data"]["content"] == "a.py\n1:token"
    assert result["data"].get("note")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({"args": ["grep", "src"]}, "src/a.txt\n1:grep appears here"),
        ({"pattern": "unique token", "args": ["rg", "src"]}, "rg\n1:unique token"),
        ({"pattern": "unique token", "args": ["rg", "-n", "src"]}, "rg\n1:unique token"),
    ],
)
async def test_program_names_can_be_literal_patterns_or_roots(tmp_path, arguments, expected):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("grep appears here")
    (tmp_path / "rg").write_text("unique token")
    result = await dispatch(tmp_path, arguments)
    assert result["ok"], result
    assert result["data"]["content"] == expected
