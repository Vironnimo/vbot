"""Effective Tool dispatch with Extension hooks and exactly-once lifecycle emission."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast, override

from core.chat._tool_context import (
    ToolDispatchContext,
)
from core.chat._tool_result_projection import (
    _empty_tool_display_payload,
    _tool_display_payload,
    _validated_extension_tool_hook_result,
    _validated_tool_result,
)
from core.chat.events import _timing_payload
from core.chat.messages import JsonObject, ToolCallRejection
from core.extensions import ExtensionRegistry, HookContext
from core.performance import measure, session_track
from core.runs import TOOL_CALL_RESULT_EVENT, TOOL_CALL_STARTED_EVENT, Run
from core.tools import (
    ToolContext,
    ToolContract,
    ToolNotFoundError,
    ToolRegistry,
    model_tool_name,
    tool_failure,
    tool_failure_for_exception,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("chat")

_RESULT_HOOK_CANCEL_GRACE_SECONDS = 5.0

TOOL_REMOVED_ERROR_CODE = "tool_removed"

_TOOL_REMOVED_MESSAGE = (
    "Nothing was run: the Tool {name} was removed from your Tools in this Session. "
    "Use your other Tools, or tell the user if the task needs {name}."
)


@dataclass
class _CallEvents:
    """The lifecycle events one dispatch has emitted, and the arguments they show."""

    arguments: JsonObject
    started: bool = False
    finished: bool = False


class _EmittingToolRegistry(ToolRegistry):
    """Adapter that emits public lifecycle events around registry dispatch.

    Events share the Tool Call's argument object and the display snapshot with
    the canonical Assistant and Tool messages instead of copying them per call.
    All of them are read-only: transports project events into new containers,
    and the Tool registry repairs arguments only on its own deep copies.
    """

    def __init__(
        self,
        registry: Any,
        run: Run,
        extension_registry: ExtensionRegistry | None = None,
        note_hook: Callable[[str], None] | None = None,
        denial_resolver: Callable[[str], str | None] | None = None,
        rejections: Mapping[int, ToolCallRejection] | None = None,
        tool_restriction: Sequence[str] | None = None,
        assistant_message_id: str | None = None,
        result_payloads: ToolDispatchContext | None = None,
        removed_tool_names: Collection[str] = frozenset(),
    ) -> None:
        self._registry = registry
        self._removed_tool_names = frozenset(removed_tool_names)
        self._result_payloads = result_payloads
        self._assistant_message_id = assistant_message_id
        self._run = run
        self._extension_registry = extension_registry
        self._note_hook = note_hook
        self._denial_resolver = denial_resolver
        self._tool_restriction = tool_restriction
        # Read at dispatch time: a Tool round adds rejections as calls arrive.
        self._rejections: Mapping[int, ToolCallRejection] = (
            rejections if rejections is not None else {}
        )
        self._tool_timings: dict[str, JsonObject] = {}
        self._tool_displays: dict[str, JsonObject] = {}
        self._tool_media: dict[str, list[JsonObject]] = {}
        self._extension_hook_lock = asyncio.Lock()

    def _hook_context(self) -> HookContext:
        return HookContext(
            session_id=self._run.session_id,
            agent_id=self._run.agent_id,
            run_id=self._run.id,
            add_note=self._note_hook or (lambda _text: None),
        )

    @override
    def is_parallel_safe(self, name: str) -> bool:
        """Delegate the wrapped Tool's execution policy."""
        resolver = getattr(self._registry, "is_parallel_safe", None)
        return bool(callable(resolver) and resolver(name))

    @override
    def requires_execution_slot(self, name: str) -> bool:
        """Delegate the wrapped Tool's global execution-slot policy."""
        resolver = getattr(self._registry, "requires_execution_slot", None)
        return not callable(resolver) or bool(resolver(name))

    @override
    def schema_fingerprint(self, name: str) -> str:
        """Return the wrapped registry's Tool schema fingerprint."""
        resolver = getattr(self._registry, "schema_fingerprint", None)
        if not callable(resolver):
            return ""
        return str(resolver(name))

    @override
    def validate_result(
        self, name: str, result: Any, *, contract: ToolContract | None = None
    ) -> JsonObject:
        """Validate through the wrapped canonical registry."""
        if contract is not None:
            return super().validate_result(name, result, contract=contract)
        validator = getattr(self._registry, "validate_result", None)
        if not callable(validator):
            return _validated_tool_result(name, result)
        return cast(JsonObject, validator(name, result))

    def _result_contract(self, name: str) -> ToolContract | None:
        resolver = getattr(self._registry, "get", None)
        if not callable(resolver):
            return None
        try:
            contract = getattr(resolver(name), "contract", None)
        except ToolNotFoundError:
            return None
        return contract if isinstance(contract, ToolContract) else None

    @override
    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        self._run.raise_if_cancelled()
        context = replace(
            context,
            tool_restriction=self._tool_restriction,
            tool_denial_resolver=self._denial_resolver,
            background_registration_hook=lambda callback: self._run.register_tool_background(
                context.tool_call_id, callback
            ),
        )
        # Publish cancellation readiness before the started event reaches an
        # accessor. Tool-owned cleanup can register a moment later; Run retains
        # an early user request and fires that callback as soon as it arrives.
        self._run.begin_tool_call(context.tool_call_id)
        self._run.tool_call_count += 1
        self._run.tool_call_names.add(context.tool_name)
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()
        events = _CallEvents(arguments=arguments)
        # Payloads persist only with a result this dispatch returns.
        returned = False
        try:
            rejection = self._rejections.get(context.tool_call_index)
            if rejection is not None:
                return self._answer_without_running(
                    context,
                    events,
                    tool_failure(rejection.code, rejection.message, retryable=False),
                    error_code=rejection.code,
                    display=_empty_tool_display_payload(),
                    started_at=started_at,
                    started_perf=started_perf,
                )
            if context.tool_name in self._removed_tool_names:
                name = model_tool_name(context.tool_name)
                removed_result = tool_failure(
                    TOOL_REMOVED_ERROR_CODE,
                    _TOOL_REMOVED_MESSAGE.format(name=name),
                    retryable=False,
                )
                return self._answer_without_running(
                    context,
                    events,
                    removed_result,
                    error_code=TOOL_REMOVED_ERROR_CODE,
                    display=_tool_display_payload(
                        self._registry,
                        context.tool_name,
                        arguments,
                        context=context,
                        result=removed_result,
                    ),
                    started_at=started_at,
                    started_perf=started_perf,
                )
            denial_message = (
                self._denial_resolver(context.tool_name)
                if self._denial_resolver is not None
                else None
            )
            if denial_message is not None:
                denied_result = tool_failure("tool_not_allowed", denial_message)
                return self._answer_without_running(
                    context,
                    events,
                    denied_result,
                    error_code="tool_not_allowed",
                    display=_tool_display_payload(
                        self._registry,
                        context.tool_name,
                        arguments,
                        context=context,
                        result=denied_result,
                    ),
                    started_at=started_at,
                    started_perf=started_perf,
                )
            # A replacement skips canonical dispatch entirely. Check the same
            # authority first so neither hook can act on a forbidden call or
            # replace its authorization failure with a successful result.
            if self._extension_registry is not None:
                self._registry.authorize(context, arguments, allowed_tools)
            # Decision pipeline runs before the started event so the timeline
            # shows the effective (possibly modified) arguments. A deny or
            # replace short-circuits execution; a modify rewrites the input the
            # tool runs with and that tool_result hooks observe.
            effective_arguments = arguments
            result: JsonObject | None = None
            result_contract: ToolContract | None = None

            def validate_replacement(extension_name: str, candidate: Any) -> JsonObject | None:
                nonlocal result_contract
                # A hook replacement has no executing Tool. Retain the contract
                # that validated the replacement only when it is accepted.
                contract = self._result_contract(context.tool_name)
                validated = _validated_extension_tool_hook_result(
                    registry=self,
                    tool_name=context.tool_name,
                    extension_name=extension_name,
                    hook_name="tool_call",
                    result=candidate,
                    contract=contract,
                )
                if validated is not None:
                    result_contract = contract
                return validated

            if self._extension_registry is not None:
                async with self._extension_hook_lock:
                    decision = await self._extension_registry.dispatch_tool_call(
                        self._hook_context(),
                        tool_name=context.tool_name,
                        tool_call_id=context.tool_call_id,
                        input=arguments,
                        validator=validate_replacement,
                    )

            if self._extension_registry is not None:
                effective_arguments = decision.effective_input
                if decision.deny_reason is not None:
                    _LOGGER.warning(
                        "Extension %r denied %s tool call: %s",
                        decision.deny_extension,
                        context.tool_name,
                        decision.deny_reason,
                    )
                    result = tool_failure(
                        "tool_call_denied",
                        f"Tool call denied by extension '{decision.deny_extension}': "
                        f"{decision.deny_reason}",
                    )
                elif decision.replacement is not None:
                    result = decision.replacement

            events.arguments = effective_arguments
            fingerprint = _tool_context_schema_fingerprint(self, context)
            started_display = _tool_display_payload(
                self._registry,
                context.tool_name,
                effective_arguments,
                context=context,
            )
            self._emit_started(context, events, display=started_display, fingerprint=fingerprint)

            if result is None:
                result = await self._dispatch_with_failure_envelope(
                    context, effective_arguments, allowed_tools
                )
                result_contract = context.result_contract

            if self._extension_registry is not None:
                result = await self._finish_result_hooks(
                    context, effective_arguments, result, result_contract
                )

            if result.get("ok") is True:
                context._commit_owned_effects()
            context._commit_worked_paths()

            # Media goes with failures too: a Tool attaches it for the Model to see,
            # such as a screenshot of what a failed input step left on screen.
            if context.result_media:
                self._tool_media[context.tool_call_id] = context.result_media
            timing = _timing_payload(started_at, started_perf)
            self._tool_timings[context.tool_call_id] = timing
            completed_display = _tool_display_payload(
                self._registry,
                context.tool_name,
                effective_arguments,
                context=context,
                result=result,
            )
            self._tool_displays[context.tool_call_id] = completed_display
            error = result.get("error")
            error_code = error.get("code") if isinstance(error, dict) else None
            _LOGGER.debug(
                "Tool %s completed (run=%s call=%s schema=%s duration_ms=%s ok=%s error=%s)",
                context.tool_name,
                self._run.id,
                context.tool_call_id,
                fingerprint[:12],
                timing["duration_ms"],
                result.get("ok"),
                error_code,
            )
            # Return a completed result even when cancellation was requested so
            # a cooperatively terminating batch can still persist it. A forceful
            # Run cancellation persists only the siblings that finished; the
            # next Provider request repairs the missing Results from durable
            # Session evidence instead.
            self._emit_result(
                context,
                events,
                result,
                display=completed_display,
                timing=timing,
                fingerprint=fingerprint,
                error_code=error_code,
            )
            returned = True
            return result
        except Exception as error:
            # A hook or the bookkeeping around the handler raised. The call
            # still gets exactly one result, in its events and for the Model.
            if events.finished:
                raise
            return self._answer_failed_dispatch(context, events, error, started_at, started_perf)
        finally:
            # Per-call cancel registry entries are scoped to a single dispatch.
            # Clearing on every exit path keeps the registry bounded and lets a
            # later call that re-uses the same id start from a clean slate.
            self._run.clear_tool_cancel(context.tool_call_id)
            if self._result_payloads is not None:
                self._result_payloads.close_result_payloads(context.tool_call_id, keep=returned)

    async def _finish_result_hooks(
        self,
        context: ToolContext,
        arguments: JsonObject,
        result: JsonObject,
        result_contract: ToolContract | None,
    ) -> JsonObject:
        """Run the Extensions' tool_result hooks on a result the call produced.

        The call's work is done, so a Run cancel lets the hooks finish within a
        grace period and the result survives. A hook that outlasts it is
        cancelled and the call ends without a result.
        """
        registry = self._extension_registry
        assert registry is not None

        async def run_hooks() -> JsonObject:
            async with self._extension_hook_lock:
                return await registry.dispatch_tool_result(
                    self._hook_context(),
                    tool_name=context.tool_name,
                    tool_call_id=context.tool_call_id,
                    input=arguments,
                    result=result,
                    validator=lambda extension_name, candidate: (
                        _validated_extension_tool_hook_result(
                            registry=self,
                            tool_name=context.tool_name,
                            extension_name=extension_name,
                            hook_name="tool_result",
                            result=candidate,
                            contract=result_contract,
                        )
                    ),
                )

        hooks = asyncio.ensure_future(run_hooks())
        try:
            return await asyncio.shield(hooks)
        except asyncio.CancelledError:
            if not self._run.cancel_requested:
                hooks.cancel()
                raise
        try:
            async with asyncio.timeout(_RESULT_HOOK_CANCEL_GRACE_SECONDS):
                return await asyncio.shield(hooks)
        except TimeoutError:
            hooks.cancel()
            _LOGGER.warning(
                "Extension tool_result hooks outlasted the cancel of Run %s; "
                "Tool call %s ends without a result",
                self._run.id,
                context.tool_call_id,
            )
            raise asyncio.CancelledError from None

    def _answer_without_running(
        self,
        context: ToolContext,
        events: _CallEvents,
        result: JsonObject,
        *,
        error_code: str,
        display: JsonObject,
        started_at: datetime,
        started_perf: float,
    ) -> JsonObject:
        """Emit the lifecycle of a call answered before any hook or handler ran."""
        timing = _timing_payload(started_at, started_perf)
        self._tool_timings[context.tool_call_id] = timing
        self._tool_displays[context.tool_call_id] = display
        fingerprint = _tool_context_schema_fingerprint(self, context)
        self._emit_started(context, events, display=display, fingerprint=fingerprint)
        self._emit_result(
            context,
            events,
            result,
            display=display,
            timing=timing,
            fingerprint=fingerprint,
            error_code=error_code,
        )
        return result

    def _answer_failed_dispatch(
        self,
        context: ToolContext,
        events: _CallEvents,
        error: Exception,
        started_at: datetime,
        started_perf: float,
    ) -> JsonObject:
        """Finish the lifecycle of a call whose hooks or bookkeeping raised."""
        result = tool_failure_for_exception(context.tool_name, error)
        try:
            display = _tool_display_payload(
                self._registry, context.tool_name, events.arguments, context=context, result=result
            )
        except Exception:
            display = _empty_tool_display_payload()
        fingerprint = _tool_context_schema_fingerprint(self, context)
        if not events.started:
            self._emit_started(context, events, display=display, fingerprint=fingerprint)
        timing = _timing_payload(started_at, started_perf)
        self._tool_timings[context.tool_call_id] = timing
        self._tool_displays[context.tool_call_id] = display
        self._emit_result(
            context,
            events,
            result,
            display=display,
            timing=timing,
            fingerprint=fingerprint,
            error_code=result["error"]["code"],
        )
        return result

    def _emit_started(
        self,
        context: ToolContext,
        events: _CallEvents,
        *,
        display: JsonObject,
        fingerprint: str,
    ) -> None:
        self._run.emit(
            TOOL_CALL_STARTED_EVENT,
            {
                "assistant_message_id": self._assistant_message_id,
                "tool_call": {
                    "id": context.tool_call_id,
                    "index": context.tool_call_index,
                    "name": context.tool_name,
                    "arguments": events.arguments,
                },
                "display": display,
                "schema_fingerprint": fingerprint,
            },
        )
        events.started = True

    def _emit_result(
        self,
        context: ToolContext,
        events: _CallEvents,
        result: JsonObject,
        *,
        display: JsonObject,
        timing: JsonObject,
        fingerprint: str,
        error_code: Any,
    ) -> None:
        self._run.emit(
            TOOL_CALL_RESULT_EVENT,
            {
                "assistant_message_id": self._assistant_message_id,
                "tool_call": {
                    "id": context.tool_call_id,
                    "index": context.tool_call_index,
                    "name": context.tool_name,
                },
                "result": result,
                "display": display,
                "timing": timing,
                "schema_fingerprint": fingerprint,
                "error_code": error_code,
            },
        )
        events.finished = True

    def take_media_for_call(self, tool_call_id: str, *, tool_message_id: str) -> list[JsonObject]:
        """Transfer in-memory media to the correlated request without retaining a cache."""
        return [
            {**media, "tool_call_id": tool_call_id, "tool_message_id": tool_message_id}
            for media in self._tool_media.pop(tool_call_id, [])
        ]

    def timing_for_call(self, tool_call_id: str) -> JsonObject | None:
        """Return measured timing for a completed tool call."""
        timing = self._tool_timings.get(tool_call_id)
        return dict(timing) if timing is not None else None

    def display_for_completed_call(self, tool_call_id: str) -> JsonObject | None:
        """Return the final presentation snapshot for a completed Tool call."""
        return self._tool_displays.get(tool_call_id)

    async def _dispatch_with_failure_envelope(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        try:
            return await self._dispatch_with_current_registry_signature(
                context,
                arguments,
                allowed_tools,
            )
        except Exception as error:
            # The failure envelope replaces whatever the handler staged.
            if self._result_payloads is not None:
                self._result_payloads.discard_result_payloads(context.tool_call_id)
            return tool_failure_for_exception(context.tool_name, error)

    async def _dispatch_with_current_registry_signature(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        run = self._run
        with measure(
            f"tool.{context.tool_name}",
            track=session_track(run.agent_id, run.session_id, run.project_id),
            name=context.tool_name,
            args={"run_id": run.id},
        ) as timer:
            try:
                result = await self._registry.dispatch(context, arguments, allowed_tools)
            except ToolNotFoundError:
                # A Model-invented Tool name must not create a metric of its own.
                timer.discard()
                raise
            return self.validate_result(context.tool_name, result, contract=context.result_contract)


def _safe_schema_fingerprint(registry: Any, tool_name: str) -> str:
    resolver = getattr(registry, "schema_fingerprint", None)
    if not callable(resolver):
        return ""
    try:
        return str(resolver(tool_name))
    except KeyError, ToolNotFoundError, ValueError:
        return ""


def _tool_context_schema_fingerprint(registry: Any, context: ToolContext) -> str:
    contract = context.input_contract
    if contract is not None:
        return contract.schema_fingerprint
    return _safe_schema_fingerprint(registry, context.tool_name)
