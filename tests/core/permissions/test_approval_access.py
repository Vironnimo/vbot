"""Permission approvals cannot bypass hard rules or escape the prepared access."""

from pathlib import Path

import pytest

from core.permissions import (
    ApprovalRequirement,
    BoundaryContext,
    PermissionBoundary,
    PermissionGrant,
    normalize_permission_rules,
)


def test_exact_grant_keeps_hard_refusals_and_subtree_denies(tmp_path: Path) -> None:
    work = tmp_path / "work"
    outside = tmp_path / "outside"
    context = BoundaryContext(workdir=work, home=tmp_path, data_dir=tmp_path / "data")
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"read": "work_area", "deny": [f"Read({outside}/private)"]}),
        context,
    )
    denied = boundary.check_path(outside, "read", subtree=True)
    assert denied is not None and denied.reason == "deny_rule"
    grant = PermissionGrant(boundary)
    requirement = ApprovalRequirement.path(boundary, outside / "public", "read")
    grant.approve([requirement])
    assert grant.check_path(outside / "public", "read") is None
    other = grant.check_path(outside / "other", "read")
    denied = grant.check_path(outside / "private", "read")
    assert other is not None and other.reason == "not_permitted"
    assert denied is not None and denied.reason == "deny_rule"
    assert PermissionGrant(boundary).check_path(outside / "public", "read") is not None


@pytest.mark.asyncio
async def test_rejected_canonical_command_is_not_asked_again(tmp_path: Path) -> None:
    import asyncio

    from core.permissions import PermissionApprovalRejected
    from core.runs import ChatRunManager, Run
    from core.tools._permission_preflight import authorize_command
    from tests.core.permissions.command_check_support import FakeAnalyzer, fake_checker, plan
    from tests.core.tools.tools_test_support import make_context

    work = tmp_path / "work"
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"commands": "listed", "allow": ["Shell(echo *)"]}),
        BoundaryContext(workdir=work, home=tmp_path, data_dir=tmp_path / "data"),
    )
    checker = fake_checker(FakeAnalyzer(plan("git status")))
    manager = ChatRunManager()
    run = Run(run_id="commands", agent_id="a", session_id="s")

    async def approval(context, reason, requirements, arguments, cwd):
        return await manager.request_approval(
            run,
            {},
            [key for item in requirements for key in item.suppression_keys()],
            [item.suppression_key for item in requirements],
        )

    context = make_context("bash", permissions=boundary, cwd=work, approval_hook=approval)

    async def invoke(command):
        await authorize_command(
            context,
            checker,
            arguments={"command": command},
            cwd=work,
            env={},
            command=command,
            shell="bash",
        )

    pending = asyncio.create_task(invoke("git status"))
    await asyncio.sleep(0)
    # The analyzer runs asynchronously; wait until the observable request exists.
    while not manager.approvals_snapshot()["requests"]:
        await asyncio.sleep(0)
    request = manager.approvals_snapshot()["requests"][0]
    assert manager.resolve_approval(request["id"], "reject")
    with pytest.raises(PermissionApprovalRejected, match="user declined"):
        await pending
    with pytest.raises(PermissionApprovalRejected, match="user declined"):
        await invoke("  git 'status'  ")
    assert manager.approvals_snapshot()["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["path", "command"])
async def test_approved_target_cannot_retarget_to_an_already_allowed_link(
    tmp_path: Path, mode: str
) -> None:
    import os

    from core.tools._permission_preflight import authorize_command, authorize_paths
    from core.tools.contracts import ToolContractError
    from tests.core.permissions.command_check_support import FakeAnalyzer, fake_checker, plan
    from tests.core.tools.tools_test_support import make_context
    from tests.directory_links import link_directory

    work, outside = tmp_path / "work", tmp_path / "outside"
    allowed = work / "allowed"
    outside.mkdir()
    allowed.mkdir(parents=True)
    link = work / "link"
    link_directory(link, outside)
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"read": "work_area"}),
        BoundaryContext(workdir=work, home=tmp_path, data_dir=tmp_path / "data"),
    )
    execution = {"computer_id": "server", "allowed_computer_ids": ["server"]}

    async def approval(context, reason, requirements, arguments, cwd):
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()
        link_directory(link, allowed)
        return True

    async def refresh():
        return boundary, execution

    context = make_context(
        "read",
        cwd=work,
        permissions=boundary,
        approval_hook=approval,
        approval_refresh_hook=refresh,
    )
    with pytest.raises(ToolContractError, match="changed"):
        if mode == "path":
            await authorize_paths(context, [(link / "file", "read", {})], arguments={})
        else:
            checker = fake_checker(FakeAnalyzer(plan("cat file", reads=[link / "file"])))
            await authorize_command(
                context, checker, arguments={}, cwd=work, env={}, command="cat file", shell="bash"
            )


@pytest.mark.asyncio
async def test_mixed_path_and_url_checks_share_one_exact_request(tmp_path: Path) -> None:
    from core.tools._permission_preflight import authorize_accesses
    from tests.core.tools.tools_test_support import make_context

    work, outside = tmp_path / "work", tmp_path / "outside"
    boundary = PermissionBoundary.build(
        normalize_permission_rules(
            {"read": "work_area", "web": "listed", "deny": [f"Read({outside}/private)"]}
        ),
        BoundaryContext(
            workdir=work, home=tmp_path, data_dir=tmp_path / "data", loopback_port=1234
        ),
    )
    execution = {"computer_id": "server", "allowed_computer_ids": ["server"]}
    requests = []

    async def approval(context, reason, requirements, arguments, cwd):
        requests.append(requirements)
        return True

    async def refresh():
        return boundary, execution

    context = make_context(
        "image",
        cwd=work,
        permissions=boundary,
        approval_hook=approval,
        approval_refresh_hook=refresh,
    )
    await authorize_accesses(
        context, [(outside, "read", {"filtered": True})], ["https://example.com/a"], arguments={}
    )
    assert len(requests) == 1 and len(requests[0]) == 2
    assert context.permission_access.search_read_allowed(outside / "file", outside)
    assert not context.permission_access.search_read_allowed(outside / "private", outside)
    from core.permissions import PermissionGrant

    assert not PermissionGrant(boundary).search_read_allowed(outside / "file", outside)
    assert context.permission_access.check_url("https://EXAMPLE.COM./a") is None
    assert context.permission_access.check_url("https://example.com/b") is not None
    hard = context.permission_access.check_url("http://localhost:1234/")
    assert hard is not None and hard.reason == "protected_location"


def test_generated_output_grant_is_limited_to_one_folder_and_keeps_hard_denies(
    tmp_path: Path,
) -> None:
    work, outside = tmp_path / "work", tmp_path / "outside"
    boundary = PermissionBoundary.build(
        normalize_permission_rules(
            {"read": "work_area", "write": "work_area", "deny": [f"Edit({outside}/secret.png)"]}
        ),
        BoundaryContext(workdir=work, home=tmp_path, data_dir=tmp_path / "data"),
    )
    grant = PermissionGrant(boundary)
    grant.approve([ApprovalRequirement.path(boundary, outside, "write")])
    assert grant.output_write_allowed(outside / "image.png", outside)
    assert not grant.output_write_allowed(outside / "secret.png", outside)
    assert not grant.output_write_allowed(outside / "nested" / "image.png", outside)
    assert not PermissionGrant(boundary).output_write_allowed(outside / "image.png", outside)


@pytest.mark.asyncio
async def test_path_approval_preparation_and_rechecks_run_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from core.tools._permission_preflight import authorize_paths
    from tests.core.tools.tools_test_support import make_context

    loop_thread = threading.get_ident()
    checks: list[int] = []
    original = PermissionBoundary.check_path

    def checked(boundary, *args, **kwargs):
        checks.append(threading.get_ident())
        return original(boundary, *args, **kwargs)

    monkeypatch.setattr(PermissionBoundary, "check_path", checked)
    context = make_context("read", cwd=tmp_path)
    await authorize_paths(context, [(tmp_path / "file", "read", {})], arguments={})
    await context.revalidate_permissions()
    assert len(checks) >= 2 and all(thread != loop_thread for thread in checks)
