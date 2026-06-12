"""Direct tests for the per-event hook dispatcher on ``ExtensionRegistry``.

These pin the composition semantics each event relies on (observer, accumulator,
context pipeline, tool_call decision pipeline, tool_result replace pipeline) so
the chat call sites can delegate without behavior drift. Handlers are seeded into
the dispatch table through ``install_handler`` (the loader's apply-phase seam),
mixing sync and async callables and exercising per-handler exception isolation
and load-order preservation.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.extensions import (
    Deny,
    ExtensionRegistry,
    HookContext,
    Modify,
    Replace,
)


def _ctx(add_note: Any = None) -> HookContext:
    if add_note is None:
        return HookContext(session_id="s1", agent_id="a1", run_id="r1")
    return HookContext(session_id="s1", agent_id="a1", run_id="r1", add_note=add_note)


def _register(registry: ExtensionRegistry, extension_name: str, event: str, handler: Any) -> None:
    """Seed a handler into the dispatch table (the loader's apply-phase seam)."""
    registry.install_handler(extension_name, event, handler)


def _make_validator() -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    """Validator stub: rejects candidates carrying an ``_invalid`` marker.

    Returns the callable plus the list of ``(extension_name, candidate)`` pairs
    it was called with, so tests can assert which dicts reached validation.
    """
    seen: list[tuple[str, dict[str, Any]]] = []

    def validate(extension_name: str, candidate: dict[str, Any]) -> dict[str, Any] | None:
        seen.append((extension_name, dict(candidate)))
        if candidate.get("_invalid"):
            return None
        return dict(candidate)

    return validate, seen


class TestRunStartRunEnd:
    """Observer events: every handler runs, return values are ignored."""

    @pytest.mark.asyncio
    async def test_run_start_runs_all_handlers_in_order_sync_and_async(self) -> None:
        registry = ExtensionRegistry()
        calls: list[str] = []

        def sync_handler(ctx: HookContext, *, session_id: str, agent_id: str) -> str:
            calls.append(f"sync:{session_id}:{agent_id}")
            return "ignored"

        async def async_handler(ctx: HookContext, *, session_id: str, agent_id: str) -> None:
            calls.append("async")

        _register(registry, "ext-a", "run_start", sync_handler)
        _register(registry, "ext-b", "run_start", async_handler)

        result = await registry.dispatch_run_start(_ctx(), session_id="s1", agent_id="a1")

        assert result is None
        assert calls == ["sync:s1:a1", "async"]

    @pytest.mark.asyncio
    async def test_run_start_handler_exception_is_isolated(self) -> None:
        registry = ExtensionRegistry()
        calls: list[str] = []

        def boom(ctx: HookContext, **payload: Any) -> None:
            raise RuntimeError("boom")

        def ok(ctx: HookContext, **payload: Any) -> None:
            calls.append("ok")

        _register(registry, "ext-a", "run_start", boom)
        _register(registry, "ext-b", "run_start", ok)

        await registry.dispatch_run_start(_ctx(), session_id="s", agent_id="a")

        assert calls == ["ok"]

    @pytest.mark.asyncio
    async def test_run_end_passes_outcome_to_every_handler(self) -> None:
        registry = ExtensionRegistry()
        seen: list[str] = []

        async def handler(
            ctx: HookContext, *, session_id: str, agent_id: str, outcome: str
        ) -> None:
            seen.append(outcome)

        _register(registry, "ext-a", "run_end", handler)
        _register(registry, "ext-b", "run_end", handler)

        await registry.dispatch_run_end(
            _ctx(), session_id="s", agent_id="a", outcome="cancelled"
        )

        assert seen == ["cancelled", "cancelled"]

    @pytest.mark.asyncio
    async def test_no_handlers_is_a_noop(self) -> None:
        registry = ExtensionRegistry()
        assert await registry.dispatch_run_start(_ctx(), session_id="s", agent_id="a") is None
        await registry.dispatch_run_end(_ctx(), session_id="s", agent_id="a", outcome="success")

    @pytest.mark.asyncio
    async def test_load_order_preserved_across_extensions(self) -> None:
        registry = ExtensionRegistry()
        order: list[str] = []

        def make(name: str) -> Any:
            def handler(ctx: HookContext, **payload: Any) -> None:
                order.append(name)

            return handler

        for name in ["ext-1", "ext-2", "ext-3"]:
            _register(registry, name, "run_start", make(name))

        await registry.dispatch_run_start(_ctx(), session_id="s", agent_id="a")

        assert order == ["ext-1", "ext-2", "ext-3"]


class TestBeforeAgentStart:
    """Accumulator event: collect every handler's ``system_prompt_append``."""

    @pytest.mark.asyncio
    async def test_accumulates_appends_in_load_order(self) -> None:
        registry = ExtensionRegistry()

        def a(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"system_prompt_append": "A"}

        async def b(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"system_prompt_append": "B"}

        def returns_none(ctx: HookContext, **payload: Any) -> None:
            return None

        _register(registry, "ext-a", "before_agent_start", a)
        _register(registry, "ext-b", "before_agent_start", b)
        _register(registry, "ext-c", "before_agent_start", returns_none)

        appends = await registry.dispatch_before_agent_start(
            _ctx(), agent=object(), session=object(), messages=[], run=object()
        )

        assert appends == ["A", "B"]

    @pytest.mark.asyncio
    async def test_ignores_non_string_append_and_missing_key(self) -> None:
        registry = ExtensionRegistry()

        def int_append(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"system_prompt_append": 123}

        def other_key(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"something_else": "x"}

        def good(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"system_prompt_append": "ok"}

        _register(registry, "ext-a", "before_agent_start", int_append)
        _register(registry, "ext-b", "before_agent_start", other_key)
        _register(registry, "ext-c", "before_agent_start", good)

        appends = await registry.dispatch_before_agent_start(
            _ctx(), agent=None, session=None, messages=[], run=None
        )

        assert appends == ["ok"]

    @pytest.mark.asyncio
    async def test_exception_skips_only_failing_handler(self) -> None:
        registry = ExtensionRegistry()

        def boom(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            raise ValueError("x")

        def good(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"system_prompt_append": "kept"}

        _register(registry, "ext-a", "before_agent_start", boom)
        _register(registry, "ext-b", "before_agent_start", good)

        appends = await registry.dispatch_before_agent_start(
            _ctx(), agent=None, session=None, messages=[], run=None
        )

        assert appends == ["kept"]


class TestContext:
    """Pipeline event: each handler may replace the running message list in turn."""

    @pytest.mark.asyncio
    async def test_handlers_chain_each_sees_previous_output(self) -> None:
        registry = ExtensionRegistry()
        seen: list[list] = []

        def first(ctx: HookContext, *, messages: list) -> list:
            seen.append(messages)
            return [{"role": "user", "content": "first"}]

        async def second(ctx: HookContext, *, messages: list) -> list:
            seen.append(messages)
            return [*messages, {"role": "user", "content": "second"}]

        _register(registry, "ext-a", "context", first)
        _register(registry, "ext-b", "context", second)

        result = await registry.dispatch_context(_ctx(), messages=[{"role": "user"}])

        # second handler saw first handler's replacement, not the original input
        assert seen == [[{"role": "user"}], [{"role": "user", "content": "first"}]]
        assert result == [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]

    @pytest.mark.asyncio
    async def test_non_list_returns_leave_running_list_unchanged(self) -> None:
        registry = ExtensionRegistry()
        original = [{"role": "user"}]

        def returns_none(ctx: HookContext, *, messages: list) -> None:
            return None

        def returns_dict(ctx: HookContext, *, messages: list) -> dict[str, Any]:
            return {"not": "a list"}

        _register(registry, "ext-a", "context", returns_none)
        _register(registry, "ext-b", "context", returns_dict)

        result = await registry.dispatch_context(_ctx(), messages=original)

        # no handler replaced, so the original list is returned unchanged
        assert result is original

    @pytest.mark.asyncio
    async def test_exception_skipped_pipeline_continues(self) -> None:
        registry = ExtensionRegistry()

        def boom(ctx: HookContext, *, messages: list) -> list:
            raise RuntimeError()

        def returns_list(ctx: HookContext, *, messages: list) -> list:
            return [{"role": "system"}]

        _register(registry, "ext-a", "context", boom)
        _register(registry, "ext-b", "context", returns_list)

        assert await registry.dispatch_context(_ctx(), messages=[]) == [{"role": "system"}]


class TestToolCall:
    """Decision pipeline: handlers modify the input, deny, or replace the result."""

    @pytest.mark.asyncio
    async def test_none_proceeds_with_unmodified_input(self) -> None:
        registry = ExtensionRegistry()

        def observe(ctx: HookContext, **payload: Any) -> None:
            return None

        _register(registry, "ext-a", "tool_call", observe)
        validator, _seen = _make_validator()

        decision = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={"x": 1}, validator=validator
        )

        assert decision.effective_input == {"x": 1}
        assert decision.deny_reason is None
        assert decision.replacement is None

    @pytest.mark.asyncio
    async def test_modify_rewrites_input_and_pipeline_continues(self) -> None:
        registry = ExtensionRegistry()
        seen_inputs: list[dict[str, Any]] = []

        def rewrite(ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict) -> Modify:
            seen_inputs.append(dict(input))
            return Modify({"cmd": "ls -la"})

        def observe(ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict) -> None:
            seen_inputs.append(dict(input))
            return None

        _register(registry, "ext-a", "tool_call", rewrite)
        _register(registry, "ext-b", "tool_call", observe)
        validator, _seen = _make_validator()

        decision = await registry.dispatch_tool_call(
            _ctx(), tool_name="bash", tool_call_id="c1", input={"cmd": "ls"}, validator=validator
        )

        # second handler saw the modified input; the decision carries it as effective
        assert seen_inputs == [{"cmd": "ls"}, {"cmd": "ls -la"}]
        assert decision.effective_input == {"cmd": "ls -la"}
        assert decision.deny_reason is None
        assert decision.replacement is None

    @pytest.mark.asyncio
    async def test_deny_short_circuits_with_reason_and_extension(self) -> None:
        registry = ExtensionRegistry()
        calls: list[str] = []

        def deny(ctx: HookContext, **payload: Any) -> Deny:
            calls.append("a")
            return Deny("blocked by policy")

        def should_not_run(ctx: HookContext, **payload: Any) -> None:
            calls.append("b")
            return None

        _register(registry, "ext-a", "tool_call", deny)
        _register(registry, "ext-b", "tool_call", should_not_run)
        validator, _seen = _make_validator()

        decision = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={}, validator=validator
        )

        assert decision.deny_reason == "blocked by policy"
        assert decision.deny_extension == "ext-a"
        assert decision.replacement is None
        assert calls == ["a"]

    @pytest.mark.asyncio
    async def test_replace_validated_short_circuits(self) -> None:
        registry = ExtensionRegistry()
        calls: list[str] = []

        def replace(ctx: HookContext, **payload: Any) -> Replace:
            calls.append("a")
            return Replace({"from": "replacement"})

        def should_not_run(ctx: HookContext, **payload: Any) -> None:
            calls.append("b")
            return None

        _register(registry, "ext-a", "tool_call", replace)
        _register(registry, "ext-b", "tool_call", should_not_run)
        validator, seen = _make_validator()

        decision = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={}, validator=validator
        )

        assert decision.replacement == {"from": "replacement"}
        assert decision.deny_reason is None
        assert calls == ["a"]
        assert [name for name, _ in seen] == ["ext-a"]

    @pytest.mark.asyncio
    async def test_invalid_replace_treated_as_continue(self) -> None:
        registry = ExtensionRegistry()

        def invalid_replace(ctx: HookContext, **payload: Any) -> Replace:
            return Replace({"_invalid": True})

        def then_replace(ctx: HookContext, **payload: Any) -> Replace:
            return Replace({"from": "valid"})

        _register(registry, "ext-a", "tool_call", invalid_replace)
        _register(registry, "ext-b", "tool_call", then_replace)
        validator, _seen = _make_validator()

        decision = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={}, validator=validator
        )

        # the rejected Replace did not short-circuit; the next valid one did
        assert decision.replacement == {"from": "valid"}

    @pytest.mark.asyncio
    async def test_plain_dict_return_is_ignored(self) -> None:
        registry = ExtensionRegistry()

        def legacy_dict(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"ok": True, "error": None, "data": {}, "artifacts": []}

        _register(registry, "ext-a", "tool_call", legacy_dict)
        validator, seen = _make_validator()

        decision = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={"x": 1}, validator=validator
        )

        # the old plain-dict short-circuit contract is gone: proceed unchanged
        assert decision.effective_input == {"x": 1}
        assert decision.deny_reason is None
        assert decision.replacement is None
        assert seen == []

    @pytest.mark.asyncio
    async def test_modify_then_deny_carries_modified_input(self) -> None:
        registry = ExtensionRegistry()

        def rewrite(ctx: HookContext, **payload: Any) -> Modify:
            return Modify({"cmd": "rewritten"})

        def deny(ctx: HookContext, **payload: Any) -> Deny:
            return Deny("nope")

        _register(registry, "ext-a", "tool_call", rewrite)
        _register(registry, "ext-b", "tool_call", deny)
        validator, _seen = _make_validator()

        decision = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={"cmd": "orig"}, validator=validator
        )

        assert decision.deny_reason == "nope"
        assert decision.deny_extension == "ext-b"
        assert decision.effective_input == {"cmd": "rewritten"}

    @pytest.mark.asyncio
    async def test_exception_skipped_then_deny_wins(self) -> None:
        registry = ExtensionRegistry()

        def boom(ctx: HookContext, **payload: Any) -> Deny:
            raise RuntimeError()

        def deny(ctx: HookContext, **payload: Any) -> Deny:
            return Deny("stop")

        _register(registry, "ext-a", "tool_call", boom)
        _register(registry, "ext-b", "tool_call", deny)
        validator, _seen = _make_validator()

        decision = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={}, validator=validator
        )

        assert decision.deny_reason == "stop"
        assert decision.deny_extension == "ext-b"

    @pytest.mark.asyncio
    async def test_handler_receives_event_payload(self) -> None:
        registry = ExtensionRegistry()
        captured: dict[str, Any] = {}

        def capture(
            ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict[str, Any]
        ) -> None:
            captured["ctx"] = ctx
            captured["tool_name"] = tool_name
            captured["tool_call_id"] = tool_call_id
            captured["input"] = input
            return None

        _register(registry, "ext-a", "tool_call", capture)
        validator, _seen = _make_validator()

        await registry.dispatch_tool_call(
            _ctx(), tool_name="bash", tool_call_id="tc-9", input={"cmd": "ls"}, validator=validator
        )

        assert captured["tool_name"] == "bash"
        assert captured["tool_call_id"] == "tc-9"
        assert captured["input"] == {"cmd": "ls"}
        assert isinstance(captured["ctx"], HookContext)


class TestToolResult:
    """Replace pipeline: each handler may swap in a full validated envelope."""

    @pytest.mark.asyncio
    async def test_each_handler_replaces_running_envelope(self) -> None:
        registry = ExtensionRegistry()
        seen_results: list[dict[str, Any]] = []

        def first(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"status": "first"}

        async def second(
            ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict, result: dict
        ) -> dict[str, Any]:
            seen_results.append(dict(result))
            return {"status": "second"}

        _register(registry, "ext-a", "tool_result", first)
        _register(registry, "ext-b", "tool_result", second)
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_result(
            _ctx(),
            tool_name="t",
            tool_call_id="c1",
            input={},
            result={"status": "original", "value": 0},
            validator=validator,
        )

        # full replace (no merge): the prior keys are gone, not merged
        assert result == {"status": "second"}
        # the second handler observed the first handler's full replacement
        assert seen_results == [{"status": "first"}]

    @pytest.mark.asyncio
    async def test_invalid_replacement_dropped_keeps_prior(self) -> None:
        registry = ExtensionRegistry()
        seen_results: list[dict[str, Any]] = []

        def bad(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"_invalid": True}

        def good(
            ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict, result: dict
        ) -> dict[str, Any]:
            seen_results.append(dict(result))
            return {"status": "good"}

        _register(registry, "ext-a", "tool_result", bad)
        _register(registry, "ext-b", "tool_result", good)
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_result(
            _ctx(),
            tool_name="t",
            tool_call_id="c1",
            input={},
            result={"status": "original"},
            validator=validator,
        )

        assert result == {"status": "good"}
        # the dropped replacement never reached the next handler
        assert seen_results == [{"status": "original"}]

    @pytest.mark.asyncio
    async def test_none_non_dict_and_exceptions_ignored(self) -> None:
        registry = ExtensionRegistry()

        def returns_none(ctx: HookContext, **payload: Any) -> None:
            return None

        def non_dict(ctx: HookContext, **payload: Any) -> str:
            return "x"

        def boom(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            raise RuntimeError()

        def replace(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"status": "replaced"}

        _register(registry, "ext-a", "tool_result", returns_none)
        _register(registry, "ext-b", "tool_result", non_dict)
        _register(registry, "ext-c", "tool_result", boom)
        _register(registry, "ext-d", "tool_result", replace)
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_result(
            _ctx(),
            tool_name="t",
            tool_call_id="c",
            input={},
            result={"status": "original"},
            validator=validator,
        )

        assert result == {"status": "replaced"}

    @pytest.mark.asyncio
    async def test_returns_original_when_no_handlers(self) -> None:
        registry = ExtensionRegistry()
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_result(
            _ctx(),
            tool_name="t",
            tool_call_id="c",
            input={},
            result={"status": "ok"},
            validator=validator,
        )

        assert result == {"status": "ok"}


class TestHookContext:
    """``HookContext`` carries identity plus an ``add_note`` capability."""

    @pytest.mark.asyncio
    async def test_add_note_delegates_to_wired_callback(self) -> None:
        registry = ExtensionRegistry()
        notes: list[str] = []

        def handler(ctx: HookContext, **payload: Any) -> None:
            ctx.add_note("from extension")
            return None

        _register(registry, "ext-a", "run_start", handler)

        await registry.dispatch_run_start(
            _ctx(add_note=notes.append), session_id="s", agent_id="a"
        )

        assert notes == ["from extension"]

    def test_default_add_note_is_a_noop(self) -> None:
        ctx = HookContext(session_id="s", agent_id="a", run_id="r")
        # no session wired: calling add_note must not raise
        ctx.add_note("dropped")

    def test_run_id_is_exposed(self) -> None:
        ctx = HookContext(session_id="s", agent_id="a", run_id="run-42")
        assert ctx.run_id == "run-42"
