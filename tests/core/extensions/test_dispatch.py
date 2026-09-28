"""Hook dispatch on ``ExtensionRegistry``: composition semantics per event.

Pins what each event does with its handlers: observers (``run_start``,
``run_end``), the ``context`` message pipeline, the ``tool_call`` decision
pipeline (``Modify`` / ``Deny`` / validated ``Replace``) and the ``tool_result``
replace pipeline, plus prefix-routed channel interaction dispatch. Hook handlers
are seeded through ``install_handler`` (the apply phase's primitive), mixing sync
and async callables, raising handlers and load order.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from core.extensions import Deny, ExtensionRegistry, HookContext, Modify, Replace
from tests.core.extensions.extension_test_support import (
    RecordingResponder,
    tap,
    write_extension,
)


def _ctx(**kwargs: Any) -> HookContext:
    return HookContext(session_id="s1", agent_id="a1", run_id="r1", **kwargs)


def _registry(event: str, *handlers: Any) -> ExtensionRegistry:
    """A registry with *handlers* for *event* installed as ext-a, ext-b, ... in order."""
    registry = ExtensionRegistry()
    for index, handler in enumerate(handlers):
        registry.install_handler(f"ext-{chr(ord('a') + index)}", event, handler)
    return registry


def _make_validator() -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    """Validator stub rejecting candidates marked ``_invalid``; records what it saw."""
    seen: list[tuple[str, dict[str, Any]]] = []

    def validate(extension_name: str, candidate: dict[str, Any]) -> dict[str, Any] | None:
        seen.append((extension_name, dict(candidate)))
        if candidate.get("_invalid"):
            return None
        return dict(candidate)

    return validate, seen


def _boom(*_args: Any, **_payload: Any) -> Any:
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_observer_events_run_every_handler_in_load_order_and_isolate_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    threads: list[int] = []
    notes: list[str] = []

    def sync_start(ctx: HookContext, *, session_id: str, agent_id: str) -> str:
        calls.append(f"sync:{session_id}:{agent_id}")
        threads.append(threading.get_ident())
        ctx.add_note("from extension")
        return "ignored"

    async def async_start(ctx: HookContext, **payload: Any) -> None:
        calls.append(f"async:{ctx.run_id}")

    async def on_end(ctx: HookContext, *, session_id: str, agent_id: str, outcome: str) -> None:
        calls.append(f"end:{outcome}")

    registry = _registry("run_start", sync_start, _boom, async_start)
    registry.install_handler("ext-a", "run_end", on_end)
    registry.install_handler("ext-c", "run_end", on_end)
    # The retired system-prompt append event has no dispatcher: registering it is inert.
    registry.install_handler("ext-a", "before" + "_agent_start", sync_start)
    caplog.set_level(logging.WARNING, logger="vbot.extensions")

    await registry.dispatch_run_start(_ctx(add_note=notes.append), session_id="s1", agent_id="a1")
    await registry.dispatch_run_end(_ctx(), session_id="s1", agent_id="a1", outcome="cancelled")
    await registry.dispatch_context(_ctx(), messages=[])

    assert calls == ["sync:s1:a1", "async:r1", "end:cancelled", "end:cancelled"]
    # Sync handlers run off the event loop thread.
    assert threads and threading.get_ident() not in threads
    assert notes == ["from extension"]
    raised = [
        entry
        for entry in caplog.records
        if entry.name == "vbot.extensions" and "handler raised" in entry.getMessage()
    ]
    assert len(raised) == 1
    assert raised[0].exc_info is not None
    # Without handlers dispatch is a no-op, and an unwired add_note drops the note.
    empty = ExtensionRegistry()
    await empty.dispatch_run_start(_ctx(), session_id="s", agent_id="a")
    await empty.dispatch_run_end(_ctx(), session_id="s", agent_id="a", outcome="success")
    _ctx().add_note("dropped")


@pytest.mark.asyncio
async def test_context_handlers_chain_the_message_list_and_skip_failures() -> None:
    seen: list[list] = []

    def first(ctx: HookContext, *, messages: list) -> list:
        seen.append(messages)
        return [{"role": "user", "content": "first"}]

    async def second(ctx: HookContext, *, messages: list) -> list:
        seen.append(messages)
        return [*messages, {"role": "user", "content": "second"}]

    def returns_none(ctx: HookContext, *, messages: list) -> None:
        return None

    def returns_dict(ctx: HookContext, *, messages: list) -> dict[str, Any]:
        return {"not": "a list"}

    registry = _registry("context", first, _boom, returns_none, returns_dict, second)

    result = await registry.dispatch_context(_ctx(), messages=[{"role": "user"}])

    # Each handler sees the previous replacement; non-list returns and raises are skipped.
    assert seen == [[{"role": "user"}], [{"role": "user", "content": "first"}]]
    assert result == [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]

    def mutates(ctx: HookContext, *, messages: list) -> None:
        messages[0]["role"] = "assistant"
        messages.append({"role": "user", "content": "added"})

    original = [{"role": "user"}]
    isolated = _registry("context", mutates, returns_none, returns_dict)
    # Handlers edit shallow per-message copies: in-place changes stay request-local.
    assert await isolated.dispatch_context(_ctx(), messages=original) == [
        {"role": "assistant"},
        {"role": "user", "content": "added"},
    ]
    assert original == [{"role": "user"}]
    # Without a context handler the caller's list passes through uncopied.
    assert await ExtensionRegistry().dispatch_context(_ctx(), messages=original) is original


@pytest.mark.asyncio
async def test_tool_call_modify_rewrites_input_for_later_handlers_and_the_decision() -> None:
    captured: list[tuple[Any, ...]] = []

    def rewrite(ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict) -> Modify:
        captured.append((type(ctx), tool_name, tool_call_id, dict(input)))
        return Modify({"cmd": "ls -la"})

    def observe(ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict) -> None:
        captured.append((type(ctx), tool_name, tool_call_id, dict(input)))
        return None

    def legacy_dict(ctx: HookContext, **payload: Any) -> dict[str, Any]:
        return {"ok": True, "error": None, "data": {}, "artifacts": []}

    registry = _registry("tool_call", rewrite, observe, legacy_dict)
    validator, validated = _make_validator()

    decision = await registry.dispatch_tool_call(
        _ctx(), tool_name="bash", tool_call_id="tc-9", input={"cmd": "ls"}, validator=validator
    )

    assert captured == [
        (HookContext, "bash", "tc-9", {"cmd": "ls"}),
        (HookContext, "bash", "tc-9", {"cmd": "ls -la"}),
    ]
    assert decision.effective_input == {"cmd": "ls -la"}
    assert decision.deny_reason is None
    # A plain dict is not a short-circuit result: nothing reaches validation.
    assert decision.replacement is None
    assert validated == []


@pytest.mark.asyncio
async def test_tool_call_deny_short_circuits_with_the_modified_input() -> None:
    calls: list[str] = []

    def rewrite(ctx: HookContext, **payload: Any) -> Modify:
        return Modify({"cmd": "rewritten"})

    def deny(ctx: HookContext, **payload: Any) -> Deny:
        calls.append("deny")
        return Deny("blocked by policy")

    def should_not_run(ctx: HookContext, **payload: Any) -> None:
        calls.append("late")

    registry = _registry("tool_call", _boom, rewrite, deny, should_not_run)
    validator, _validated = _make_validator()

    decision = await registry.dispatch_tool_call(
        _ctx(), tool_name="t", tool_call_id="c1", input={"cmd": "orig"}, validator=validator
    )

    assert (decision.deny_reason, decision.deny_extension) == ("blocked by policy", "ext-c")
    assert decision.effective_input == {"cmd": "rewritten"}
    assert decision.replacement is None
    assert calls == ["deny"]


@pytest.mark.asyncio
async def test_tool_call_replace_short_circuits_only_once_validated() -> None:
    calls: list[str] = []

    def invalid_replace(ctx: HookContext, **payload: Any) -> Replace:
        return Replace({"_invalid": True})

    def replace(ctx: HookContext, **payload: Any) -> Replace:
        return Replace({"from": "replacement"})

    def should_not_run(ctx: HookContext, **payload: Any) -> None:
        calls.append("late")

    registry = _registry("tool_call", invalid_replace, replace, should_not_run)
    validator, validated = _make_validator()

    decision = await registry.dispatch_tool_call(
        _ctx(), tool_name="t", tool_call_id="c1", input={}, validator=validator
    )

    assert decision.replacement == {"from": "replacement"}
    assert decision.deny_reason is None
    assert [name for name, _candidate in validated] == ["ext-a", "ext-b"]
    assert calls == []


@pytest.mark.asyncio
async def test_tool_result_handlers_replace_the_whole_envelope_in_turn() -> None:
    seen: list[dict[str, Any]] = []

    def returns_none(ctx: HookContext, **payload: Any) -> None:
        return None

    def non_dict(ctx: HookContext, **payload: Any) -> str:
        return "x"

    def invalid(ctx: HookContext, **payload: Any) -> dict[str, Any]:
        return {"_invalid": True}

    def first(ctx: HookContext, *, result: dict, **payload: Any) -> dict[str, Any]:
        seen.append(dict(result))
        return {"status": "first"}

    async def second(
        ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict, result: dict
    ) -> dict[str, Any]:
        seen.append(dict(result))
        return {"status": "second"}

    registry = _registry("tool_result", returns_none, non_dict, _boom, invalid, first, second)
    validator, _validated = _make_validator()

    result = await registry.dispatch_tool_result(
        _ctx(),
        tool_name="t",
        tool_call_id="c1",
        input={},
        result={"status": "original", "value": 0},
        validator=validator,
    )

    # Dropped returns never reach later handlers; replacements are full, not merged.
    assert seen == [{"status": "original", "value": 0}, {"status": "first"}]
    assert result == {"status": "second"}
    unchanged = await ExtensionRegistry().dispatch_tool_result(
        _ctx(),
        tool_name="t",
        tool_call_id="c",
        input={},
        result={"status": "ok"},
        validator=validator,
    )
    assert unchanged == {"status": "ok"}


@pytest.mark.asyncio
async def test_channel_interactions_route_by_prefix_and_isolate_raising_handlers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_extension(
        tmp_path / "extensions",
        "taps",
        "seen = []\n"
        "async def _check(event, responder):\n"
        "    seen.append((event.data, responder))\n"
        "def _ping(event, responder):\n"
        "    seen.append((event.data, responder))\n"
        "async def _boom(event, responder):\n"
        "    raise RuntimeError('boom')\n"
        "def register(api):\n"
        "    api.register_interaction_handler('chk', _check)\n"
        "    api.register_interaction_handler('ping', _ping)\n"
        "    api.register_interaction_handler('boom', _boom)\n",
    )
    registry = ExtensionRegistry.load(tmp_path / "extensions")
    seen = sys.modules["vbot_ext.taps"].seen
    responder = RecordingResponder()
    caplog.set_level(logging.WARNING, logger="vbot.extensions")

    assert await registry.dispatch_channel_interaction(tap("chk:milk"), responder) is True
    assert await registry.dispatch_channel_interaction(tap("other:x"), responder) is False
    # Data without a colon is its own prefix; sync handlers are supported.
    assert await registry.dispatch_channel_interaction(tap("ping"), responder) is True
    # A matched handler that raises still counts as handled; the error is only logged.
    assert await registry.dispatch_channel_interaction(tap("boom:a"), responder) is True

    assert seen == [("chk:milk", responder), ("ping", responder)]
    assert any("interaction handler" in entry.getMessage() for entry in caplog.records)
