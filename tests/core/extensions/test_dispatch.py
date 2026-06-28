"""Direct tests for the per-event hook dispatcher on ``ExtensionRegistry``.

These pin the composition semantics each event relies on (observer, accumulator,
context pipeline, tool_call decision pipeline, tool_result replace pipeline) so
the chat call sites can delegate without behavior drift. Handlers are seeded into
the dispatch table through ``install_handler`` (the loader's apply-phase seam),
mixing sync and async callables and exercising per-handler exception isolation
and load-order preservation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from core.extensions import (
    Deny,
    ExtensionRegistry,
    HookContext,
    Modify,
    Replace,
)
from core.extensions.extensions import ExtensionAPI, ExtensionRecord


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

        # Sync handler return values are ignored; dispatch itself returns nothing.
        await registry.dispatch_run_start(_ctx(), session_id="s1", agent_id="a1")

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
    async def test_run_start_handler_exception_is_logged_with_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = ExtensionRegistry()

        def boom(ctx: HookContext, **payload: Any) -> None:
            raise RuntimeError("boom")

        _register(registry, "ext-a", "run_start", boom)

        caplog.set_level(logging.WARNING, logger="vbot.extensions")
        await registry.dispatch_run_start(_ctx(), session_id="s", agent_id="a")

        raised_records = [
            record
            for record in caplog.records
            if record.name == "vbot.extensions" and "handler raised" in record.getMessage()
        ]
        assert len(raised_records) == 1
        assert raised_records[0].exc_info is not None

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

        await registry.dispatch_run_end(_ctx(), session_id="s", agent_id="a", outcome="cancelled")

        assert seen == ["cancelled", "cancelled"]

    @pytest.mark.asyncio
    async def test_no_handlers_is_a_noop(self) -> None:
        registry = ExtensionRegistry()
        await registry.dispatch_run_start(_ctx(), session_id="s", agent_id="a")
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


# The retired event name, assembled at runtime so the literal never appears in
# source (the legacy tail-append event is gone entirely, D6).
_RETIRED_PROMPT_EVENT = "before" + "_agent_start"


class TestRetiredPromptAppendEventRemoved:
    """The legacy system-prompt tail-append event is gone entirely (D6).

    Its sole purpose was the append, replaced by declared prompt blocks. The
    dispatch surface is now exactly the five kept events, and an extension that
    still registers the retired name runs harmlessly into the void.
    """

    def test_dispatch_surface_is_the_five_kept_events(self) -> None:
        registry = ExtensionRegistry()
        dispatch_methods = {name for name in dir(registry) if name.startswith("dispatch_")}
        assert dispatch_methods == {
            "dispatch_run_start",
            "dispatch_run_end",
            "dispatch_context",
            "dispatch_tool_call",
            "dispatch_tool_result",
        }

    @pytest.mark.asyncio
    async def test_registering_the_retired_event_is_inert(self) -> None:
        # Registering the retired event name must not raise and must never fire —
        # the existing "unknown event is inert" behavior covers it, nothing
        # special-cases the name anymore.
        registry = ExtensionRegistry()
        calls: list[str] = []

        def handler(ctx: HookContext, **payload: Any) -> None:
            calls.append("ran")
            return None

        _register(registry, "ext-a", _RETIRED_PROMPT_EVENT, handler)
        # No dispatcher reads this event; the only generic dispatch (context) does
        # not touch it, so the handler never runs.
        await registry.dispatch_context(_ctx(), messages=[])
        assert calls == []


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


def _loaded_record(registry: ExtensionRegistry, name: str) -> ExtensionAPI:
    """Append a loaded record for *name* and return an API over its declarations.

    The apply-phase seam for prompt blocks (mirrors ``install_handler`` for hooks):
    tests register blocks through the returned ``ExtensionAPI`` without the full
    filesystem load path, then call the registry's ``prompt_block_declarations``.
    """
    record = ExtensionRecord(
        name=name,
        root_path=Path(name),
        entry_path=Path(name),
        status="loaded",
    )
    registry._records.append(record)
    return ExtensionAPI(name, record.declarations, config={}, logger=logging.getLogger(name))


class TestPromptBlockDeclarations:
    """``register_prompt_block`` collects declarations; the registry builds blocks."""

    def test_static_and_dynamic_blocks_become_definitions(self) -> None:
        registry = ExtensionRegistry()
        api = _loaded_record(registry, "greeter")
        api.register_prompt_block("static", default_text="Hello.")
        api.register_prompt_block("dynamic", render=lambda ctx: "Rendered.")

        definitions = registry.prompt_block_declarations()

        by_id = {definition.id: definition for definition in definitions}
        assert by_id["extension:static"].owner == "extension:greeter"
        assert by_id["extension:static"].default_text == "Hello."
        assert by_id["extension:static"].editable is True
        assert by_id["extension:dynamic"].owner == "extension:greeter"
        assert by_id["extension:dynamic"].render is not None
        assert by_id["extension:dynamic"].editable is False

    def test_requires_exactly_one_of_text_or_render(self) -> None:
        registry = ExtensionRegistry()
        api = _loaded_record(registry, "ext")

        with pytest.raises(ValueError, match="exactly one"):
            api.register_prompt_block("both", default_text="x", render=lambda ctx: "y")
        with pytest.raises(ValueError, match="exactly one"):
            api.register_prompt_block("neither")

    def test_slug_collision_is_first_wins_with_diagnostic(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = ExtensionRegistry()
        first = _loaded_record(registry, "ext-a")
        second = _loaded_record(registry, "ext-b")
        first.register_prompt_block("shared", default_text="A wins.")
        second.register_prompt_block("shared", default_text="B loses.")

        caplog.set_level(logging.WARNING, logger="vbot.extensions")
        definitions = registry.prompt_block_declarations()

        # Exactly one extension:shared block, owned by the first-loaded extension.
        shared = [d for d in definitions if d.id == "extension:shared"]
        assert len(shared) == 1
        assert shared[0].owner == "extension:ext-a"
        assert shared[0].default_text == "A wins."
        # Both sides diagnosed on their records.
        record_a = next(r for r in registry.records() if r.name == "ext-a")
        record_b = next(r for r in registry.records() if r.name == "ext-b")
        assert any("also declared" in message for message in record_a.capability_errors)
        assert any("skipped" in message for message in record_b.capability_errors)

    def test_disabled_and_failed_extensions_contribute_nothing(self) -> None:
        registry = ExtensionRegistry()
        loaded = _loaded_record(registry, "loaded-ext")
        loaded.register_prompt_block("block", default_text="Visible.")
        # A disabled and a failed record with declarations must be ignored.
        disabled = ExtensionRecord(
            name="disabled-ext",
            root_path=Path("disabled"),
            entry_path=Path("disabled"),
            status="disabled",
        )
        ExtensionAPI(
            "disabled-ext", disabled.declarations, config={}, logger=logging.getLogger("d")
        ).register_prompt_block("block", default_text="Hidden.")
        registry._records.append(disabled)

        definitions = registry.prompt_block_declarations()

        ids = {definition.id for definition in definitions}
        assert ids == {"extension:block"}
        assert all(d.owner == "extension:loaded-ext" for d in definitions)

    def test_loaded_extension_names_reflects_only_loaded(self) -> None:
        registry = ExtensionRegistry()
        _loaded_record(registry, "loaded-ext")
        disabled = ExtensionRecord(
            name="disabled-ext",
            root_path=Path("disabled"),
            entry_path=Path("disabled"),
            status="disabled",
        )
        registry._records.append(disabled)

        assert registry.loaded_extension_names() == {"loaded-ext"}


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

        await registry.dispatch_run_start(_ctx(add_note=notes.append), session_id="s", agent_id="a")

        assert notes == ["from extension"]

    def test_default_add_note_is_a_noop(self) -> None:
        ctx = HookContext(session_id="s", agent_id="a", run_id="r")
        # no session wired: calling add_note must not raise
        ctx.add_note("dropped")

    def test_run_id_is_exposed(self) -> None:
        ctx = HookContext(session_id="s", agent_id="a", run_id="run-42")
        assert ctx.run_id == "run-42"
