"""Integration review boundaries, including code that has already landed."""

from __future__ import annotations

import pytest

from .test_factory_execution import (
    _NEW,
    Git,
    reviewed,
    started,
    submitted,
)
from .test_factory_execution import (
    service as service,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "moved",
        "moved_merged",
        "merge_files",
        "merge_contents",
        "merge_files_needs_changes",
        "merge_contents_needs_changes",
        "missing",
        "reference",
        "reference_only_without_target",
        "needs_changes",
        "landed_needs_changes",
        "failed",
    ],
)
async def test_integration_checks_and_changed_heads_return_to_review(service, monkeypatch, case):
    await started(service)
    card = await reviewed(service)
    if case == "reference_only_without_target":
        service.store.cards[card["id"]]["runtime"]["settings"]["integration_check"] = (
            "reference_only"
        )
        card = await service.card()

        async def unavailable_target(*_args):
            raise AssertionError("Reference-only integration does not require a local target")

        monkeypatch.setattr(Git, "base", unavailable_target)
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    if case == "reference_only_without_target":
        assert "integration_base_sha" not in card["runtime"]
    else:
        assert card["runtime"]["integration_base_sha"] == Git.target_value
    if case in {"moved", "moved_merged", "landed_needs_changes"}:
        monkeypatch.setattr(Git, "head_value", _NEW)
        monkeypatch.setattr(Git, "merged", case != "moved")
    if case.startswith("merge_files"):
        monkeypatch.setattr(Git, "extra_paths", ("unreviewed.txt",))
    if case.startswith("merge_contents"):
        monkeypatch.setattr(Git, "merge_paths", ("feature.txt",))
    if case == "missing":
        monkeypatch.setattr(Git, "ancestor", False)
    arguments = {
        "outcome": (
            "needs_changes"
            if case.endswith("_needs_changes")
            else case
            if case in {"needs_changes", "failed"}
            else "integrated"
        ),
        "note": "Integration conflict",
    }
    if case == "reference":
        monkeypatch.setattr(Git, "ancestor", False)
        arguments["reference"] = "https://example.test/pull/1"
    elif case == "reference_only_without_target":
        arguments["reference"] = "Project integration record"
    async with service.lock:
        await service.execution.report_integration(service.tool_context(card), arguments)
    await service.finish()
    card = await service.card()
    if case in {"moved", "moved_merged", "landed_needs_changes"} or case.startswith("merge_"):
        assert card["state"] == "working"
        assert card["head_sha"] == (Git.merge_value if case.startswith("merge_") else _NEW)
        assert card["base_sha"] == Git.target_value
        assert card["runtime"]["integration_review"] is True
        assert card["runtime"]["current_participant"] == "review-r2"
        assert ("integration_landing" in card["runtime"]) is (case != "moved")
        if case != "moved":
            landing = card["runtime"]["integration_landing"]
            assert landing["head_sha"] == card["head_sha"]
            assert landing["status"] == "pending"
            assert landing["extra_paths"] == (
                ["unreviewed.txt"] if case.startswith("merge_files") else []
            )
    elif case in {"failed", "missing"}:
        assert (card["state"], card["waiting_reason"]) == ("waiting", "integration_failed")
    elif case == "needs_changes":
        assert card["state"] == "working"
        # Answering a question during the rework keeps it headed for integration review.
        async with service.lock:
            await service.execution.report_blocked(
                service.tool_context(card), {"question": "Keep the old API?"}
            )
        await service.finish()
        async with service.lock:
            await service.execution.send_back(await service.card(), "Yes")
        await service.flush()
        card = await submitted(service)
        assert card["runtime"]["integration_review"] is True
        assert card["runtime"]["current_participant"] == "review-r2"
    else:
        assert card["state"] == "done"
    await service.execution.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["rebase", "merge"])
@pytest.mark.parametrize("verdict", ["submit", "changes_requested"])
async def test_already_landed_review_waits_for_user_instead_of_automatic_rework(
    service, monkeypatch, source, verdict
):
    await started(service)
    card = await reviewed(service)
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    if source == "rebase":
        monkeypatch.setattr(Git, "head_value", _NEW)
        monkeypatch.setattr(Git, "merged", True)
    else:
        monkeypatch.setattr(Git, "extra_paths", ("unreviewed.txt",))
    card = await service.card()
    async with service.lock:
        await service.execution.report_integration(
            service.tool_context(card), {"outcome": "integrated"}
        )
    await service.finish()
    card = await service.card()
    admissions = len(service.host.temporary_agents.admissions)
    assert card["runtime"]["current_participant"] == "review-r2"
    async with service.lock:
        if verdict == "submit":
            await service.execution.submit_work(
                service.tool_context(card), {"summary": "Post-integration judgment"}
            )
        else:
            await service.execution.request_changes(
                service.tool_context(card), {"findings": ["Integration finding"]}
            )
    await service.finish()
    card = await service.card()
    assert card["state"] == "waiting"
    assert card["waiting_reason"] == ("finished" if verdict == "submit" else "no_agreement")
    assert card["runtime"]["integration_landing"]["status"] == (
        "approved" if verdict == "submit" else "changes_requested"
    )
    assert len(service.host.temporary_agents.admissions) == admissions
    # Even an override of rejected landed work is a fresh, explicit user action.
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    async with service.lock:
        await service.execution.report_integration(
            service.tool_context(card), {"outcome": "integrated"}
        )
    await service.finish()
    assert (await service.card())["state"] == "done"
    assert len(service.host.temporary_agents.admissions) == admissions + 1


@pytest.mark.asyncio
async def test_unknown_landed_base_is_visible_and_can_be_supplied_without_losing_changed_head(
    service, monkeypatch
):
    await started(service)
    card = await reviewed(service)
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    monkeypatch.setattr(Git, "head_value", _NEW)
    monkeypatch.setattr(Git, "merged", True)
    monkeypatch.setattr(Git, "unknown_base", True)
    async with service.lock:
        result = await service.execution.report_integration(
            service.tool_context(card), {"outcome": "integrated"}
        )
    assert not result["ok"] and result["error"]["code"] == "integration_base_required"
    card = await service.card()
    assert card["state"] == "integrating"
    assert card["runtime"]["integration_landing"]["head_sha"] == _NEW
    assert "review_branch_head_sha" not in card["runtime"]
    async with service.lock:
        result = await service.execution.report_integration(
            service.tool_context(card), {"outcome": "integrated", "base_commit": Git.target_value}
        )
    assert result["ok"]
    await service.finish()
    card = await service.card()
    assert card["state"] == "working" and card["head_sha"] == _NEW
    assert card["runtime"]["current_participant"] == "review-r2"
    assert card["base_sha"] == Git.target_value
    await service.execution.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_historical_landing_does_not_turn_new_isolated_rework_into_post_landing_review(
    service, monkeypatch
):
    await started(service)
    card = await reviewed(service)
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    monkeypatch.setattr(Git, "head_value", _NEW)
    monkeypatch.setattr(Git, "merged", True)
    card = await service.card()
    async with service.lock:
        await service.execution.report_integration(
            service.tool_context(card), {"outcome": "integrated"}
        )
    await service.finish()
    card = await service.card()
    async with service.lock:
        await service.execution.request_changes(service.tool_context(card), {"findings": ["Fix"]})
    await service.finish()
    async with service.lock:
        await service.execution.send_back(await service.card(), "Repair in isolation")
    await service.flush()
    monkeypatch.setattr(Git, "head_value", "f" * 40)
    monkeypatch.setattr(Git, "merged", False)
    card = await submitted(service)
    assert "review_branch_head_sha" not in card["runtime"]
    assert card["runtime"]["integration_landing"]["head_sha"] != card["head_sha"]
    async with service.lock:
        await service.execution.request_changes(
            service.tool_context(card), {"findings": ["Another fix"]}
        )
    await service.finish()
    assert (await service.card())["state"] == "working"
    await service.execution.quiesce()
    await service.flush()
