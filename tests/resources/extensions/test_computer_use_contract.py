"""Computer use: contract behavior."""

from __future__ import annotations

from pathlib import Path

from resources.extensions.computer_use import extension as computer_use
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


def test_complete_provider_matrix_runs_through_real_handler(computer):
    from scripts.probe_provider_tool_call import COMPUTER_CASE_ARGUMENTS

    service, context, client, _ = computer
    for case, arguments in COMPUTER_CASE_ARGUMENTS.items():
        args = dict(arguments)
        if case.startswith("invalid_"):
            before = len(client.calls)
            result = service.handle(context, args)
            assert result["error"]["code"] == "invalid_arguments", case
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
        result = service.handle(context, args)
        assert result["ok"], (case, result)


def test_computer_skill_is_discoverable_from_the_loaded_extension():
    from core.skills import SkillRegistry

    root = Path(computer_use.__file__).parent / "skills"
    registry = SkillRegistry.load(root)
    skill = registry.get("computer-use")
    assert skill is not None and skill.name == "computer-use"
