"""Computer use: the Provider probe matrix and the bundled Skill match the Tool."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.skills import SkillRegistry
from resources.extensions.computer_use import extension as computer_use
from scripts.provider_probe.computer_cases import COMPUTER_CASE_ARGUMENTS
from tests.resources.extensions.computer_use.computer_use_test_support import dispatch


def test_complete_provider_matrix_runs_through_real_handler(computer, monkeypatch):
    service, run_context, client, _ = computer
    # Waits and held keys in the matrix advance a controlled clock instead of real time.
    clock = [0.0]
    monkeypatch.setattr(computer_use.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        service._wake, "wait", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    for case, arguments in COMPUTER_CASE_ARGUMENTS.items():
        # Each case runs in its own Run, so earlier captures never make a target ambiguous.
        context = replace(run_context, run_id=f"run-{case}")
        args = dict(arguments)
        if case.startswith("invalid_"):
            before = len(client.calls)
            result = dispatch(computer, args, context)
            assert (result["error"] or {}).get("code") in {
                "invalid_arguments",
                "capture_required",
            }, case
            assert len(client.calls) == before, case
            continue
        target = {key: args[key] for key in ("pid", "window_id", "monitor") if key in args}
        if case in {
            "capture_view_query",
            "verify_view",
            "element_target",
            "sequence_element_target",
            "capture_reset_background",
        }:
            target = {"pid": 1, "window_id": 2}
        observed = service.handle(
            context,
            {
                "action": "capture",
                **target,
                "mode": "som" if "pid" in target else "vision",
                "foreground": args.get("foreground", "pid" not in target),
            },
        )
        assert observed["ok"], case
        if "view_id" in args:
            args["view_id"] = observed["data"]["view_id"]
        if ":" in args.get("element", ""):
            args["element"] = observed["data"]["elements"][0]["element"]
        if case == "sequence_element_target":
            args["steps"] = [
                {"action": "click", "element": observed["data"]["elements"][0]["element"]}
            ]
        result = dispatch(computer, args, context)
        assert result["ok"], (case, result)


def test_computer_skill_is_discoverable_from_the_loaded_extension():
    root = Path(computer_use.__file__).parent / "skills"
    registry = SkillRegistry.load(root)
    skill = registry.get("computer-use")
    assert skill is not None and skill.name == "computer-use"
