"""Capture driver: render every Provider, Connection, Model, effort and mode offline.

Record kinds (one JSONL file per Provider, see ``records.py``):

* ``catalog`` - per Provider Model: the catalog facts (task types, task options,
  modalities, voices, Connections, pricing, reasoning, windows). The only
  record for Models that cannot chat.
* ``connection`` - per Connection: the Adapter class and any setup traffic
  (Copilot's token exchange).
* ``model`` - per Connection and chat Model: the Adapter's declarations
  (reasoning replay policy and fidelity, wire media, size limits, the reasoning
  render description per effort, the request input estimate, the effective
  context window, the output limit on the wire and the detected wire protocol).
* ``render`` - per effort and mode (plus one image attachment variant): every
  request the Adapter put on the wire up to and including the chat request.
* ``response`` - per mode: canned protocol responses run through
  ``send`` + ``normalize_response`` and through ``stream``.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import core.providers.adapter_types  # noqa: F401  (load every Adapter before patching)
from core.chat.model_resolution import resolve_request_temperature, resolve_request_top_p
from core.models.models import Model
from core.providers.accounts import ConnectionRef
from core.providers.adapter import (
    ProviderAdapter,
    estimate_wire_request_input_tokens,
    request_input_budget,
)
from core.providers.providers import ConnectionConfig, resolve_effective_context_window
from core.providers.reasoning import ReasoningReplayPolicy
from core.utils.retry import caller_owns_retries
from scripts._wire_snapshot.environment import SnapshotEnvironment, build_environment
from scripts._wire_snapshot.fixtures import (
    AGENT_ID,
    PROMPT_CACHE_AFFINITY_ID,
    SESSION_ID,
    image_media_type,
    request_messages,
)
from scripts._wire_snapshot.records import Masker, to_jsonable, write_snapshot
from scripts._wire_snapshot.transport import CaptureSentinel, WireRecorder, offline_wire

EFFORTS: tuple[str | None, ...] = (None, "none", "minimal", "low", "medium", "high", "xhigh", "max")
MODES = ("send", "stream")
UNKNOWN_MODEL_ID = "snapshot-unknown-model"
_IMAGE_VARIANT_EFFORT: str | None = None
_IMAGE_VARIANT_MODE = "send"
_OUTPUT_LIMIT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


@dataclass
class CaptureResult:
    """What one capture wrote, for the console report."""

    records_by_provider: dict[str, list[dict[str, Any]]]
    summary: dict[str, Any]
    seconds: float = 0.0
    swallowed: list[str] = field(default_factory=list)


def capture_snapshot(out_dir: Path, providers: Iterable[str] | None = None) -> CaptureResult:
    """Capture the snapshot of *providers* (all when ``None``) into *out_dir*.

    Raises ``ValueError`` for a Provider id the Provider registry does not know.
    """

    started = time.perf_counter()
    result = asyncio.run(_capture(sorted(set(providers)) if providers else None))
    write_snapshot(out_dir, result.records_by_provider, result.summary)
    result.seconds = time.perf_counter() - started
    return result


async def _capture(selected: list[str] | None) -> CaptureResult:
    vbot_logger = logging.getLogger("vbot")
    previous_level = vbot_logger.level
    # Adapters log expected fallbacks (e.g. the refused Codex WebSocket); the
    # records carry everything that matters.
    vbot_logger.setLevel(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="vbot-wire-snapshot-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            environment = await build_environment(data_dir)
            provider_ids = environment.providers.list_ids()
            if selected is not None:
                unknown = sorted(set(selected) - set(provider_ids))
                if unknown:
                    raise ValueError(f"unknown Provider(s): {', '.join(unknown)}")
                provider_ids = selected
            recorder = WireRecorder(Masker(environment.secrets, data_dir))
            records_by_provider: dict[str, list[dict[str, Any]]] = {}
            with offline_wire(recorder):
                for provider_id in provider_ids:
                    capture = _ProviderCapture(environment, recorder, provider_id)
                    records_by_provider[provider_id] = await capture.run()
    finally:
        vbot_logger.setLevel(previous_level)
    return _result(records_by_provider)


def _result(records_by_provider: dict[str, list[dict[str, Any]]]) -> CaptureResult:
    summary: dict[str, Any] = {"providers": {}}
    totals: Counter[str] = Counter()
    swallowed: list[str] = []
    for provider_id, records in sorted(records_by_provider.items()):
        kinds = Counter(str(record["kind"]) for record in records)
        outcomes = Counter(
            str(record["outcome"]) for record in records if record["kind"] == "render"
        )
        swallowed.extend(
            str(record["key"])
            for record in records
            if record["kind"] == "render" and record["outcome"] == "sentinel_swallowed"
        )
        summary["providers"][provider_id] = {
            "records": dict(sorted(kinds.items())),
            "render_outcomes": dict(sorted(outcomes.items())),
        }
        totals.update(kinds)
    summary["totals"] = dict(sorted(totals.items()))
    return CaptureResult(records_by_provider, summary, swallowed=sorted(swallowed))


@dataclass(frozen=True)
class _Target:
    model_id: str
    model: Model | None


class _ProviderCapture:
    """Capture every record of one Provider."""

    def __init__(
        self, environment: SnapshotEnvironment, recorder: WireRecorder, provider_id: str
    ) -> None:
        self._environment = environment
        self._recorder = recorder
        self._masker = recorder.masker
        self._provider_id = provider_id
        self._config = environment.providers.get(provider_id)
        self._records: list[dict[str, Any]] = []

    async def run(self) -> list[dict[str, Any]]:
        catalog = self._environment.models.list_for_provider(self._provider_id)
        for model in catalog:
            self._records.append(self._catalog_record(model))
        for connection in self._config.connections:
            await self._capture_connection(connection, catalog)
        return self._records

    def _catalog_record(self, model: Model) -> dict[str, Any]:
        capabilities = model.capabilities
        return {
            "key": f"catalog|{self._provider_id}|{model.model_id}",
            "kind": "catalog",
            "provider": self._provider_id,
            "model": model.model_id,
            "chat": _is_chat_model(model),
            "task_types": capabilities.task_types,
            "task_options": capabilities.task_options,
            "input_modalities": capabilities.input_modalities,
            "output_modalities": capabilities.output_modalities,
            "voices": capabilities.supported_voices,
            "supported_parameters": capabilities.supported_parameters,
            "tools": capabilities.tools,
            "vision": capabilities.vision,
            "json_mode": capabilities.json_mode,
            "reasoning": capabilities.reasoning,
            "unlisted_tool_calls": capabilities.unlisted_tool_calls,
            "connections": model.connections,
            "context_window": model.context_window,
            "connection_context_windows": model.connection_context_windows,
            "max_output_tokens": model.max_output_tokens,
            "recommended_temperature": model.recommended_temperature,
            "recommended_top_p": model.recommended_top_p,
            "reasoning_replay": model.reasoning_replay,
            "pricing": model.pricing,
            "family": model.family,
        }

    async def _capture_connection(self, connection: ConnectionConfig, catalog: list[Model]) -> None:
        scope = f"{self._provider_id}:{connection.id}"
        ref = ConnectionRef(self._provider_id, scope)
        record: dict[str, Any] = {
            "key": f"connection|{scope}",
            "kind": "connection",
            "connection_type": connection.type,
            "connection_mode": connection.mode,
        }
        self._recorder.begin("setup")
        try:
            await self._environment.runtime.get_connection_token_getter(ref)()
        except Exception as error:
            record["token_error"] = self._masker.error(error)
        record["setup_requests"] = self._recorder.take()
        try:
            render_adapter = self._environment.runtime.get_adapter(ref)
            respond_adapter = self._environment.runtime.get_adapter(ref)
        except Exception as error:
            record["adapter_error"] = self._masker.error(error)
            self._records.append(record)
            return
        record["adapter"] = f"{type(render_adapter).__module__}.{type(render_adapter).__qualname__}"
        targets = [
            _Target(model.model_id, model)
            for model in catalog
            if _is_chat_model(model) and model.allows_connection(connection.id)
        ]
        targets.append(_Target(UNKNOWN_MODEL_ID, None))
        record["chat_models"] = len(targets) - 1
        self._records.append(record)
        try:
            for target in targets:
                capture = _ModelCapture(self, scope, connection.id, target)
                await capture.run(render_adapter, respond_adapter)
        finally:
            await render_adapter.aclose()
            await respond_adapter.aclose()


class _ModelCapture:
    """Capture the model, render and response records of one Connection Model."""

    def __init__(
        self, provider: _ProviderCapture, scope: str, connection_id: str, target: _Target
    ) -> None:
        self._provider = provider
        self._recorder = provider._recorder
        self._masker = provider._masker
        self._environment = provider._environment
        self._provider_id = provider._provider_id
        self._scope = scope
        self._connection_id = connection_id
        self._model_id = target.model_id
        self._model = target.model
        self._agent_model = f"{self._provider_id}/{self._model_id}::{connection_id}"
        self._key = f"{scope}|{self._model_id}"
        models = self._environment.models
        # Chat passes the Agent's temperature (unset here) through the same resolution.
        self._temperature = resolve_request_temperature(
            None, models, self._provider_id, self._model_id
        )
        self._top_p = resolve_request_top_p(models, self._provider_id, self._model_id)

    async def run(self, render_adapter: ProviderAdapter, respond_adapter: ProviderAdapter) -> None:
        record: dict[str, Any] = {"key": f"model|{self._key}", "kind": "model"}
        declarations = _declarations(render_adapter, self._model_id, self._masker)
        record.update(declarations)
        replay_policy = declarations.get("reasoning_replay_policy")
        policy = cast(
            ReasoningReplayPolicy,
            replay_policy if isinstance(replay_policy, str) else "current_run",
        )
        context = self._request_context(render_adapter, record)

        probe_messages, tools = request_messages(
            agent_model=self._agent_model, protocol=None, replay_policy=policy
        )
        probe = await self._render(
            render_adapter, probe_messages, tools, context, effort=None, mode="send"
        )
        protocol = self._recorder_protocol(probe)
        record["protocol"] = protocol
        record["probe_outcome"] = probe["outcome"]
        record["probe_websocket_attempts"] = [
            request for request in probe["requests"] if request["method"] == "WEBSOCKET"
        ]
        record["output_limit"] = _output_limit(probe["requests"])

        messages, tools = request_messages(
            agent_model=self._agent_model, protocol=protocol, replay_policy=policy
        )
        budget = self._estimate(render_adapter, messages, tools, record)
        record["describe_reasoning_render"] = {
            _effort_label(effort): self._describe(effort) for effort in EFFORTS
        }
        record["effective_context_window"] = self._effective_context_window()
        record["unlisted_tool_calls"] = (
            self._model.capabilities.unlisted_tool_calls if self._model else None
        )

        for effort in EFFORTS:
            for mode in MODES:
                render = await self._render(
                    render_adapter,
                    messages,
                    tools,
                    context,
                    effort=effort,
                    mode=mode,
                    budget=budget,
                )
                self._add_render("text", effort, mode, render)

        wire_media = declarations.get("wire_media_support")
        input_modalities = self._model.capabilities.input_modalities if self._model else ()
        image_media = image_media_type(
            frozenset(wire_media if isinstance(wire_media, list) else ())
        )
        if "image" not in input_modalities:
            record["image_variant"] = "skipped: the Model takes no image input"
        elif image_media is None:
            record["image_variant"] = "skipped: the wire carries no image media"
        else:
            record["image_variant"] = image_media
            image_messages, tools = request_messages(
                agent_model=self._agent_model,
                protocol=protocol,
                replay_policy=policy,
                image_media=image_media,
            )
            render = await self._render(
                render_adapter,
                image_messages,
                tools,
                context,
                effort=_IMAGE_VARIANT_EFFORT,
                mode=_IMAGE_VARIANT_MODE,
                budget=budget,
            )
            self._add_render("image", _IMAGE_VARIANT_EFFORT, _IMAGE_VARIANT_MODE, render)

        self._provider._records.append(self._masker.json_value(to_jsonable(record)))
        for mode in MODES:
            await self._respond(respond_adapter, messages, tools, context, mode, budget)

    # ------------------------------------------------------------------
    # Declarations
    # ------------------------------------------------------------------

    def _request_context(self, adapter: ProviderAdapter, record: dict[str, Any]) -> dict[str, Any]:
        try:
            context = dict(
                adapter.request_context_kwargs(
                    agent_id=AGENT_ID,
                    session_id=SESSION_ID,
                    project_id=None,
                    prompt_cache_affinity_id=PROMPT_CACHE_AFFINITY_ID,
                )
            )
        except Exception as error:
            record["request_context_error"] = self._masker.error(error)
            return {}
        record["request_context_kwargs"] = self._masker.json_value(to_jsonable(context))
        record["sampling"] = {"temperature": self._temperature, "top_p": self._top_p}
        return context

    def _estimate(
        self,
        adapter: ProviderAdapter,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        record: dict[str, Any],
    ) -> int:
        try:
            estimate = estimate_wire_request_input_tokens(
                adapter, messages, model_id=self._model_id, tools=tools
            )
        except Exception as error:
            record["estimate_request_input_tokens"] = self._masker.error(error)
            return 0
        record["estimate_request_input_tokens"] = estimate
        return estimate

    def _describe(self, effort: str | None) -> Any:
        try:
            intent = self._environment.runtime.describe_reasoning_render(
                self._provider_id, self._model_id, effort
            )
        except Exception as error:
            return {"error": self._masker.error(error)}
        return to_jsonable(intent)

    def _effective_context_window(self) -> int | None:
        if self._model is None:
            return None
        return resolve_effective_context_window(
            self._model.context_window_for(self._connection_id),
            self._provider._config,
            model_metadata=self._model.metadata,
            model_key=f"{self._provider_id}/{self._model_id}",
            local_context_windows=self._environment.runtime.local_context_windows(),
        )

    @staticmethod
    def _recorder_protocol(render: dict[str, Any]) -> str | None:
        protocol = render.get("protocol")
        return protocol if isinstance(protocol, str) else None

    # ------------------------------------------------------------------
    # Renders and responses
    # ------------------------------------------------------------------

    async def _render(
        self,
        adapter: ProviderAdapter,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        context: dict[str, Any],
        *,
        effort: str | None,
        mode: str,
        budget: int | None = None,
    ) -> dict[str, Any]:
        """Send one request in capture mode; the sentinel ends it after the chat request."""

        self._recorder.begin("capture", self._model_id)
        render: dict[str, Any] = {}
        try:
            await self._call(adapter, messages, tools, context, effort, mode, budget)
            render["outcome"] = "returned_without_chat_request"
            if self._recorder.chat_requests:
                render["outcome"] = "sentinel_swallowed"
        except CaptureSentinel:
            render["outcome"] = "captured"
        except Exception as error:
            render["outcome"] = "sentinel_swallowed" if self._recorder.chat_requests else "error"
            render["error"] = self._masker.error(error)
        render["protocol"] = self._recorder.chat_protocol
        render["requests"] = self._recorder.take()
        return render

    async def _call(
        self,
        adapter: ProviderAdapter,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        context: dict[str, Any],
        effort: str | None,
        mode: str,
        budget: int | None,
    ) -> Any:
        """Call the Adapter the way Chat does; return the response or the stream deltas."""

        kwargs = {
            "model_id": self._model_id,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "thinking_effort": effort,
            "tools": tools,
            **context,
        }
        if budget is None:
            budget = estimate_wire_request_input_tokens(
                adapter, messages, model_id=self._model_id, tools=tools
            )
        with caller_owns_retries(), request_input_budget(self._model_id, budget):
            if mode == "send":
                return await adapter.send(messages, **kwargs)
            return [delta async for delta in adapter.stream(messages, **kwargs)]

    def _add_render(
        self, variant: str, effort: str | None, mode: str, render: dict[str, Any]
    ) -> None:
        self._provider._records.append(
            {
                "key": f"render|{self._key}|{variant}|effort={_effort_label(effort)}|mode={mode}",
                "kind": "render",
                **render,
            }
        )

    async def _respond(
        self,
        adapter: ProviderAdapter,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        context: dict[str, Any],
        mode: str,
        budget: int,
    ) -> None:
        """Run a canned protocol response through ``send`` + normalization or ``stream``."""

        self._recorder.begin("respond", self._model_id)
        record: dict[str, Any] = {
            "key": f"response|{self._key}|mode={mode}",
            "kind": "response",
        }
        try:
            result = await self._call(adapter, messages, tools, context, None, mode, budget)
            if mode == "send":
                record["raw_keys"] = sorted(result) if isinstance(result, dict) else None
                record["normalized"] = adapter.normalize_response(result, model_id=self._model_id)
            else:
                record["deltas"] = result
        except Exception as error:
            record["error"] = self._masker.error(error)
        record["endpoints"] = [
            f"{request['method']} {request['url']}" for request in self._recorder.take()
        ]
        self._provider._records.append(self._masker.json_value(to_jsonable(record)))


def _declarations(adapter: ProviderAdapter, model_id: str, masker: Masker) -> dict[str, Any]:
    declarations: dict[str, Any] = {}
    probes = {
        "reasoning_replay_policy": adapter.reasoning_replay_policy,
        "reasoning_replay_fidelity": adapter.reasoning_replay_fidelity,
        "wire_media_support": adapter.wire_media_support,
        "image_size_limit": adapter.image_size_limit,
        "request_body_limit": adapter.request_body_limit,
    }
    for name, probe in probes.items():
        try:
            declarations[name] = to_jsonable(probe(model_id))
        except Exception as error:
            declarations[name] = {"error": masker.error(error)}
    return declarations


def _is_chat_model(model: Model) -> bool:
    return "chat" in model.capabilities.task_types


def _effort_label(effort: str | None) -> str:
    return "unset" if effort is None else effort


def _output_limit(requests: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the output-token limit field the chat request carried, if any."""

    for request in reversed(requests):
        body = request.get("body")
        if request["method"] != "POST" or not isinstance(body, dict):
            continue
        for field_name in _OUTPUT_LIMIT_FIELDS:
            if field_name in body:
                return {"field": field_name, "value": body[field_name]}
        options = body.get("options")
        if isinstance(options, dict) and "num_predict" in options:
            return {"field": "options.num_predict", "value": options["num_predict"]}
        generation = body.get("generationConfig")
        if isinstance(generation, dict) and "maxOutputTokens" in generation:
            return {
                "field": "generationConfig.maxOutputTokens",
                "value": generation["maxOutputTokens"],
            }
        return None
    return None
