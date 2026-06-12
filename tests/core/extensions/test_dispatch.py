"""Direct tests for the per-event hook dispatcher on ``ExtensionRegistry``.

These pin the composition semantics each event relies on (observer, accumulator,
first-wins pipeline, decision-style validator pipeline) so the chat call sites
can delegate without behavior drift. Handlers are registered through the public
``HooksAPI`` facade, mixing sync and async callables and exercising per-handler
exception isolation and load-order preservation.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.extensions import ExtensionRegistry, HookContext, HooksAPI


def _ctx() -> HookContext:
    return HookContext(session_id="s1", agent_id="a1")


def _register(registry: ExtensionRegistry, extension_name: str, event: str, handler: Any) -> None:
    HooksAPI(registry, extension_name).on(event, handler)


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
    """Pipeline event: first handler returning a list wins, the rest are skipped."""

    @pytest.mark.asyncio
    async def test_first_list_wins_and_short_circuits(self) -> None:
        registry = ExtensionRegistry()
        calls: list[str] = []

        def returns_none(ctx: HookContext, *, messages: list) -> None:
            calls.append("a")
            return None

        async def returns_list(ctx: HookContext, *, messages: list) -> list:
            calls.append("b")
            return [{"role": "user", "content": "replaced"}]

        def should_not_run(ctx: HookContext, *, messages: list) -> list:
            calls.append("c")
            return [{"role": "x"}]

        _register(registry, "ext-a", "context", returns_none)
        _register(registry, "ext-b", "context", returns_list)
        _register(registry, "ext-c", "context", should_not_run)

        result = await registry.dispatch_context(_ctx(), messages=[{"role": "user"}])

        assert result == [{"role": "user", "content": "replaced"}]
        assert calls == ["a", "b"]

    @pytest.mark.asyncio
    async def test_returns_none_when_no_handler_replaces(self) -> None:
        registry = ExtensionRegistry()

        def returns_none(ctx: HookContext, *, messages: list) -> None:
            return None

        def returns_dict(ctx: HookContext, *, messages: list) -> dict[str, Any]:
            return {"not": "a list"}

        _register(registry, "ext-a", "context", returns_none)
        _register(registry, "ext-b", "context", returns_dict)

        assert await registry.dispatch_context(_ctx(), messages=[]) is None

    @pytest.mark.asyncio
    async def test_exception_skipped_then_later_list_wins(self) -> None:
        registry = ExtensionRegistry()

        def boom(ctx: HookContext, *, messages: list) -> list:
            raise RuntimeError()

        def returns_list(ctx: HookContext, *, messages: list) -> list:
            return [{"role": "system"}]

        _register(registry, "ext-a", "context", boom)
        _register(registry, "ext-b", "context", returns_list)

        assert await registry.dispatch_context(_ctx(), messages=[]) == [{"role": "system"}]


class TestToolCall:
    """Pipeline event: first valid result envelope short-circuits the tool."""

    @pytest.mark.asyncio
    async def test_first_valid_envelope_short_circuits(self) -> None:
        registry = ExtensionRegistry()
        calls: list[str] = []

        def invalid(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            calls.append("a")
            return {"_invalid": True, "from": "a"}

        async def valid(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            calls.append("b")
            return {"from": "b"}

        def should_not_run(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            calls.append("c")
            return {"from": "c"}

        _register(registry, "ext-a", "tool_call", invalid)
        _register(registry, "ext-b", "tool_call", valid)
        _register(registry, "ext-c", "tool_call", should_not_run)
        validator, seen = _make_validator()

        result = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={"x": 1}, validator=validator
        )

        assert result == {"from": "b"}
        assert calls == ["a", "b"]
        # validator saw the rejected candidate then the accepted one; ext-c never validated
        assert [name for name, _ in seen] == ["ext-a", "ext-b"]

    @pytest.mark.asyncio
    async def test_returns_none_when_no_valid_envelope(self) -> None:
        registry = ExtensionRegistry()

        def invalid(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"_invalid": True}

        def non_dict(ctx: HookContext, **payload: Any) -> str:
            return "nope"

        _register(registry, "ext-a", "tool_call", invalid)
        _register(registry, "ext-b", "tool_call", non_dict)
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={}, validator=validator
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_exception_skipped_then_valid_wins(self) -> None:
        registry = ExtensionRegistry()

        def boom(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            raise RuntimeError()

        def valid(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"from": "valid"}

        _register(registry, "ext-a", "tool_call", boom)
        _register(registry, "ext-b", "tool_call", valid)
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_call(
            _ctx(), tool_name="t", tool_call_id="c1", input={}, validator=validator
        )

        assert result == {"from": "valid"}

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
    """Pipeline event: every handler shallow-merge-patches the envelope in turn."""

    @pytest.mark.asyncio
    async def test_each_handler_merges_onto_running_result(self) -> None:
        registry = ExtensionRegistry()
        seen_results: list[dict[str, Any]] = []

        def patch_value(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"value": 1}

        async def patch_extra(
            ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict, result: dict
        ) -> dict[str, Any]:
            seen_results.append(dict(result))
            return {"extra": "e"}

        _register(registry, "ext-a", "tool_result", patch_value)
        _register(registry, "ext-b", "tool_result", patch_extra)
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_result(
            _ctx(),
            tool_name="t",
            tool_call_id="c1",
            input={},
            result={"status": "ok", "value": 0},
            validator=validator,
        )

        assert result == {"status": "ok", "value": 1, "extra": "e"}
        # the second handler observed the result already patched by the first
        assert seen_results == [{"status": "ok", "value": 1}]

    @pytest.mark.asyncio
    async def test_invalid_patch_dropped_keeps_prior_result(self) -> None:
        registry = ExtensionRegistry()
        seen_results: list[dict[str, Any]] = []

        def bad_patch(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"_invalid": True}

        def good_patch(
            ctx: HookContext, *, tool_name: str, tool_call_id: str, input: dict, result: dict
        ) -> dict[str, Any]:
            seen_results.append(dict(result))
            return {"value": 9}

        _register(registry, "ext-a", "tool_result", bad_patch)
        _register(registry, "ext-b", "tool_result", good_patch)
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_result(
            _ctx(),
            tool_name="t",
            tool_call_id="c1",
            input={},
            result={"status": "ok"},
            validator=validator,
        )

        assert result == {"status": "ok", "value": 9}
        # the dropped patch never reached the next handler
        assert seen_results == [{"status": "ok"}]

    @pytest.mark.asyncio
    async def test_non_dict_and_exceptions_ignored(self) -> None:
        registry = ExtensionRegistry()

        def non_dict(ctx: HookContext, **payload: Any) -> str:
            return "x"

        def boom(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            raise RuntimeError()

        def patch(ctx: HookContext, **payload: Any) -> dict[str, Any]:
            return {"added": True}

        _register(registry, "ext-a", "tool_result", non_dict)
        _register(registry, "ext-b", "tool_result", boom)
        _register(registry, "ext-c", "tool_result", patch)
        validator, _seen = _make_validator()

        result = await registry.dispatch_tool_result(
            _ctx(),
            tool_name="t",
            tool_call_id="c",
            input={},
            result={"status": "ok"},
            validator=validator,
        )

        assert result == {"status": "ok", "added": True}

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
