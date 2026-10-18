"""Immutable Model records, task derivation, validation and frozen fact projection."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from core.models.assembly import (
    ModelDataIssueReport,
    log_model_data_issue,
)
from core.models.pricing import TokenPricing

MODEL_TASK_ORDER = (
    "chat",
    "text_output",
    "image_input",
    "image_understanding",
    "file_input",
    "file_understanding",
    "audio_input",
    "speech_to_text",
    "video_input",
    "video_understanding",
    "image_generation",
    "audio_generation",
    "music_generation",
    "text_to_speech",
    "text_embedding",
    "decision",
    "video_generation",
    # Explicit-only: no modality combination derives a live voice Model.
    "live_voice",
)

# The tasks that name what a chat request may carry. They follow a Model's final
# input modalities; every other task names a Provider route (see
# ``_settle_task_types``).
INPUT_TASK_TYPES = frozenset(
    {
        "image_input",
        "image_understanding",
        "file_input",
        "file_understanding",
        "audio_input",
        "video_input",
        "video_understanding",
    }
)

# How the provider exposes the reasoning control on the wire. ``levels`` is an
# effort ladder (e.g. low/medium/high), ``on_off`` a binary thinking toggle,
# ``budget`` a token budget. Derived from models.dev ``reasoning_options`` at
# refresh; see ``stuff/HANDOFF-model-db.md`` → "Reasoning — Steuerung".
REASONING_CONTROL_LEVELS = "levels"
REASONING_CONTROL_ON_OFF = "on_off"
REASONING_CONTROL_BUDGET = "budget"
REASONING_CONTROLS = (
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    REASONING_CONTROL_BUDGET,
)


@dataclass(frozen=True)
class ReasoningCapabilities:
    """How a model exposes reasoning through a specific provider.

    ``supported`` is the only required field and stays the load-bearing flag
    that runtime/snapping read (``model_reasoning_supported``). ``None`` means
    unknown: the source said nothing, so a lower layer or the wire profile
    decides and requests render the selected effort. The typed
    control fields describe *how* the provider steers reasoning and are all
    optional:

    * ``control`` — the wire control kind (one of ``REASONING_CONTROLS``), or
      ``None`` when not yet known. It is absent when ``supported`` is ``False``
      and may also be absent when ``supported`` is ``True`` but no ladder data
      has been projected yet (effort ladders arrive from models.dev
      ``reasoning_options`` in a later refresh phase).
    * ``levels`` — the effort ladder for ``control == "levels"`` (a subset of
      ``THINKING_EFFORT_ORDER``), empty otherwise.
    * ``budget_max`` — the maximum thinking-token budget for
      ``control == "budget"``, ``None`` otherwise.
    * ``mandatory`` — the provider always reasons and rejects a request that
      turns reasoning off; only meaningful when ``supported`` is ``True``.

    The fields are ordered so existing ``ReasoningCapabilities(supported=...)``
    construction sites keep working unchanged.
    """

    supported: bool | None
    control: str | None = None
    levels: tuple[str, ...] = ()
    budget_max: int | None = None
    mandatory: bool = False


@dataclass(frozen=True)
class Capabilities:
    """Provider-specific capability flags for a model.

    ``task_options`` holds typed option schemas for specialized task execution,
    keyed by task type (e.g. ``image_generation``). Each entry carries the
    provider-published facts the Settings option builder renders generically:

    * ``parameters`` — mapping of wire parameter name to a typed spec:
      ``{"type": "enum", "values": [...]}``, ``{"type": "range", "min": n,
      "max": n}``, ``{"type": "boolean"}`` (the parameter is supported,
      value free-form), or ``{"type": "string"}``. A spec
      may carry an optional ``default`` value that the option builder uses
      when it is among the offered choices.
    * ``passthrough`` — mapping of upstream provider slug to the list of
      provider-specific option keys accepted via passthrough (OpenRouter
      ``provider.options``).

    Projected at refresh from provider task-capability feeds (e.g. the
    OpenRouter image API) or hand-authored in ``<provider>.overrides.json``
    for providers whose APIs publish nothing (OpenAI native). It is frozen on
    construction and merged wholesale as one ``capabilities`` sub-field at load.
    """

    vision: bool
    tools: bool
    json_mode: bool
    reasoning: ReasoningCapabilities
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    supported_parameters: tuple[str, ...] = ()
    supported_voices: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    task_options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        input_modalities = _normalize_string_tuple(self.input_modalities)
        if not input_modalities:
            input_modalities = ("text", "image") if self.vision else ("text",)

        output_modalities = _normalize_string_tuple(self.output_modalities)
        if not output_modalities:
            output_modalities = ("text",)

        supported_parameters = _normalize_string_tuple(self.supported_parameters, sort=True)
        supported_voices = _normalize_string_tuple(self.supported_voices, sort=True)
        task_types = _normalize_string_tuple(self.task_types)
        if not task_types:
            task_types = derive_model_task_types(input_modalities, output_modalities)

        object.__setattr__(self, "input_modalities", input_modalities)
        object.__setattr__(self, "output_modalities", output_modalities)
        object.__setattr__(self, "supported_parameters", supported_parameters)
        object.__setattr__(self, "supported_voices", supported_voices)
        object.__setattr__(self, "task_types", task_types)
        object.__setattr__(self, "task_options", _freeze_metadata_value(self.task_options))


def text_embedding_capabilities(supported_parameters: tuple[str, ...] = ()) -> Capabilities:
    """Return the capabilities of a text-in, vectors-out embedding Model.

    The ``embeddings`` output derives only the ``text_embedding`` task, so such
    a Model is never offered as a chat Model. Provider catalogs that identify
    embedding Models use this one shape.
    """

    return Capabilities(
        vision=False,
        tools=False,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=False),
        input_modalities=("text",),
        output_modalities=("embeddings",),
        supported_parameters=supported_parameters,
    )


def _settle_task_types(
    record: dict[str, Any],
    override_model: Mapping[str, Any] | None,
) -> None:
    """Align an assembled record's input tasks with its final input modalities.

    A generated catalog stores the tasks its Adapter derived from the
    modalities the endpoint reported, but enrichment and overrides may widen
    those modalities later. The input tasks (what a chat request may carry)
    follow the assembled modalities; every other task stays as the catalog
    lists it, because it names a Provider route such as transcription or image
    generation that widened modalities do not create. An override's
    ``task_types`` is a hand-curated complete list and stays as it is.
    """

    override_capabilities = (override_model or {}).get("capabilities")
    if (
        isinstance(override_capabilities, Mapping)
        and override_capabilities.get("task_types") is not None
    ):
        return
    capabilities = record.get("capabilities")
    if not isinstance(capabilities, dict):
        return
    stored = capabilities.get("task_types")
    final_modalities = _record_modalities(capabilities)
    if not isinstance(stored, list) or final_modalities is None:
        return
    tasks = ({task for task in stored if isinstance(task, str)} - INPUT_TASK_TYPES) | (
        set(derive_model_task_types(*final_modalities)) & INPUT_TASK_TYPES
    )
    capabilities["task_types"] = [task for task in MODEL_TASK_ORDER if task in tasks]


def _record_modalities(
    capabilities: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Return a record's input and output modalities with the Capabilities defaults."""

    inputs = capabilities.get("input_modalities")
    outputs = capabilities.get("output_modalities")
    if not _is_string_list(inputs, allow_none=True) or not _is_string_list(
        outputs, allow_none=True
    ):
        return None
    if not inputs:
        inputs = ["text", "image"] if capabilities.get("vision") is True else ["text"]
    return tuple(inputs), tuple(outputs or ["text"])


def _is_string_list(value: Any, *, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def derive_model_task_types(
    input_modalities: Iterable[str],
    output_modalities: Iterable[str],
) -> tuple[str, ...]:
    """Derive coarse task filters from provider-reported model modalities.

    Modality conventions (provider-specific, normalized on ingestion):

    * ``"transcription"`` in output — dedicated speech-to-text models (e.g.
      ``openai/whisper-1`` via OpenRouter ``?output_modalities=transcription``).
    * ``"speech"`` in output — dedicated text-to-speech models (e.g.
      ``openai/gpt-4o-mini-tts`` via OpenRouter ``?output_modalities=speech``).
    * ``"audio"`` in output — generic audio generation (music, sound effects,
      or conversational audio).  Models with only ``"audio"`` are NOT tagged
      ``text_to_speech`` unless they also have ``"speech"`` in their output
      modalities.
    * ``"embeddings"`` in output — dedicated text-embedding models (e.g.
      ``openai/text-embedding-3-small`` via OpenRouter
      ``?output_modalities=embeddings``). The model produces vectors, not
      text, so it is NOT tagged ``chat`` or ``text_output``. Mirrors the
      ``speech`` → ``text_to_speech`` alias.
    """

    inputs = set(_normalize_string_tuple(tuple(input_modalities)))
    outputs = set(_normalize_string_tuple(tuple(output_modalities)))

    # "transcription" output means the model produces text from audio → STT
    has_transcription = "transcription" in outputs
    # "speech" output means the model produces speech audio → TTS
    has_speech = "speech" in outputs
    # "audio" output is generic audio generation (music, effects, conv audio)
    has_audio = "audio" in outputs

    has_text_output = "text" in outputs or has_transcription

    tasks: set[str] = set()

    if has_text_output:
        tasks.add("text_output")
    if "text" in inputs and has_text_output:
        tasks.add("chat")
    if "image" in inputs:
        tasks.add("image_input")
        if has_text_output:
            tasks.add("image_understanding")
    if "file" in inputs:
        tasks.add("file_input")
        if has_text_output:
            tasks.add("file_understanding")
    if "audio" in inputs:
        tasks.add("audio_input")
        if has_text_output:
            tasks.add("speech_to_text")
    if has_transcription:
        # Dedicated STT models that output transcription text
        tasks.add("speech_to_text")
    if "video" in inputs:
        tasks.add("video_input")
        if has_text_output:
            tasks.add("video_understanding")
    if "image" in outputs:
        tasks.add("image_generation")
    if has_audio:
        tasks.add("audio_generation")
    if has_speech:
        # Dedicated TTS models that output speech audio
        tasks.add("audio_generation")
        tasks.add("text_to_speech")
    if "embeddings" in outputs:
        # Dedicated embedding models: output is a vector, not text/chat.
        # Mirror of the "speech" → text_to_speech alias.
        tasks.add("text_embedding")
    if "decisions" in outputs:
        tasks.add("decision")
    if "video" in outputs:
        tasks.add("video_generation")

    return tuple(task for task in MODEL_TASK_ORDER if task in tasks)


@dataclass(frozen=True)
class Model:
    """A specific AI model at a specific provider.

    The ``model_id`` is the exact string sent in API requests — no remapping.
    For example, ``"anthropic/claude-sonnet-4"`` at OpenRouter is sent as-is.

    ``family`` is the model lineage as the provider/feed reports it (e.g.
    ``"gpt-5.2"``, ``"claude-sonnet-4"``). It is a first-class fact on the model
    — the handoff places it here ("eigenes Feld am Modell"), replacing per-adapter
    family-from-name guessing. Optional; defaults to ``""`` when unknown.

    ``connection_context_windows`` carries the exceptional case where two
    Connections expose different Context limits for the same Provider wire id.
    Callers that know the selected Connection use :meth:`context_window_for`;
    other readers retain the conservative Model-wide ``context_window``.

    ``metadata`` is the sanctioned home for provider-scoped per-model wire facts.
    Conventions (keep them tight — this is not a dumping ground):

    * **Provider-scoped:** keys are provider ids (e.g.
      ``metadata.github_copilot.supported_endpoints``,
      ``metadata.opencode_go.interleaved_field``), so one provider's wire
      quirk never pollutes the schema for every model.
    * **Small and immutable after load:** nested mappings/lists are frozen on
      construction (see ``__post_init__``); loaded ``Model`` instances never
      mutate.
    * **Provider-reported facts only — never hand-authored wire decisions, raw
      payloads, provider policy text, credentials, or secrets.** Wire decisions
      (protocol, carriers, replay scope, media, listed Tools) live in the
      Provider's wire profile file (``resources/wire/<provider>.json``).
    """

    model_id: str
    name: str
    capabilities: Capabilities
    context_window: int | None
    max_output_tokens: int | None
    family: str = ""
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    connections: tuple[str, ...] = ()
    connection_context_windows: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    recommended_temperature: float | None = None
    recommended_top_p: float | None = None
    pricing: TokenPricing | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_metadata_value(self.metadata))
        object.__setattr__(
            self,
            "connection_context_windows",
            _freeze_connection_context_windows(self.connection_context_windows),
        )

    def allows_connection(self, connection_id: str) -> bool:
        """Whether this model may run on ``connection_id`` of its provider.

        An empty ``connections`` allowlist permits every connection; a non-empty
        one restricts the model to the listed connection ids. This is the single
        source of the per-model connection rule — target expansion and the
        save-time guards read it so the catalog cannot offer a model on a
        connection it forbids.
        """
        return not self.connections or connection_id in self.connections

    def context_window_for(self, connection_id: str) -> int | None:
        """Return the selected Connection's window or the Model-wide fallback."""

        return self.connection_context_windows.get(connection_id, self.context_window)


def _model_from_record(
    model_id: str,
    record: Mapping[str, Any],
    report: ModelDataIssueReport = log_model_data_issue,
) -> Model:
    """Construct a typed ``Model`` from an assembled effective-model record.

    The record is the field-level merge of the canonical, provider, and override
    layers (see :mod:`core.models.assembly`) with the internal ``canonical``
    pointer already stripped. It must carry the loader's required fields; a layer
    set that fails to supply one (e.g. a model missing ``name`` or
    ``capabilities``) surfaces as a ``KeyError`` naming the missing field, which
    is the correct "the data is incomplete" signal. ``report`` receives optional
    values that are ignored without dropping the Model.

    ``context_window`` and ``max_output_tokens`` are the deliberate exceptions:
    an absent value is the honest "this fact is unknown" signal, not a load
    error. It stays ``None`` in the data (the read-side default chain — model
    value → provider-config default → global floor — fills the gap at use time,
    see :func:`core.providers.providers.resolve_context_window`).
    """

    caps = _required_field(record, "capabilities")
    reasoning_data = _required_field(record, "capabilities", "reasoning")
    supported = _required_field(record, "capabilities", "reasoning", "supported")
    reasoning = ReasoningCapabilities(
        supported=supported,
        control=reasoning_data.get("control"),
        levels=tuple(reasoning_data.get("levels", ())),
        budget_max=reasoning_data.get("budget_max"),
        mandatory=_coerce_reasoning_mandatory(
            reasoning_data.get("mandatory"), supported=supported, report=report
        ),
    )
    capabilities = Capabilities(
        vision=_required_field(record, "capabilities", "vision"),
        tools=_required_field(record, "capabilities", "tools"),
        json_mode=_required_field(record, "capabilities", "json_mode"),
        reasoning=reasoning,
        input_modalities=tuple(caps.get("input_modalities", ())),
        output_modalities=tuple(caps.get("output_modalities", ())),
        supported_parameters=tuple(caps.get("supported_parameters", ())),
        supported_voices=tuple(caps.get("supported_voices", ())),
        task_types=tuple(caps.get("task_types", ())),
        task_options=caps.get("task_options", {}),
    )
    return Model(
        model_id=model_id,
        name=_required_field(record, "name"),
        capabilities=capabilities,
        context_window=record.get("context_window"),
        max_output_tokens=record.get("max_output_tokens"),
        family=record.get("family", ""),
        metadata=record.get("metadata", {}),
        connections=tuple(record.get("connections", ())),
        connection_context_windows=record.get("connection_context_windows", {}),
        recommended_temperature=_coerce_recommended_temperature(
            record.get("recommended_temperature"),
            report,
        ),
        recommended_top_p=_coerce_recommended_top_p(record.get("recommended_top_p"), report),
        pricing=TokenPricing.from_dict(record.get("pricing")),
    )


def _required_field(record: Mapping[str, Any], *keys: str) -> Any:
    """Return the nested required value at ``keys``; a missing one raises ``KeyError(path)``."""

    value: Any = record
    for depth, key in enumerate(keys):
        if not isinstance(value, Mapping):
            raise ValueError(f"field '{'.'.join(keys[:depth])}' must be an object")
        if key not in value:
            raise KeyError(".".join(keys[: depth + 1]))
        value = value[key]
    return value


def _describe_model_data_error(exc: Exception) -> str:
    """Render a caught Model data error; a bare ``KeyError`` names its missing field."""

    if type(exc) is KeyError and exc.args:
        return f"missing required field '{exc.args[0]}'"
    return str(exc)


def _freeze_connection_context_windows(value: Any) -> Mapping[str, int]:
    """Validate and freeze optional per-Connection Context limits."""

    if not isinstance(value, Mapping):
        raise ValueError("connection_context_windows must be an object")
    windows: dict[str, int] = {}
    for connection_id, context_window in value.items():
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_context_windows keys must be non-empty strings")
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            raise ValueError("connection_context_windows values must be positive integers")
        windows[connection_id] = context_window
    return MappingProxyType(windows)


def _coerce_reasoning_mandatory(
    value: Any, *, supported: Any, report: ModelDataIssueReport
) -> bool:
    """Validate the optional ``capabilities.reasoning.mandatory`` fact.

    Absent means optional reasoning. A non-boolean value, or ``true`` on a Model
    whose reasoning is not supported, is reported and treated as ``False`` so
    one bad fact never hides the Model.
    """

    if value is None or value is False:
        return False
    if value is not True:
        report(f"capabilities.reasoning.mandatory is not a boolean ({value!r}); ignoring")
        return False
    if supported is not True:
        report("capabilities.reasoning.mandatory is true but reasoning is not supported; ignoring")
        return False
    return True


def _coerce_recommended_temperature(value: Any, report: ModelDataIssueReport) -> float | None:
    """Coerce a ``recommended_temperature`` record value into a valid float or None.

    Like ``context_window`` and ``max_output_tokens``, this is an optional
    model fact that stays ``None`` when absent. A non-numeric or out-of-range
    value is logged and treated as unknown rather than failing assembly, so one
    bad override never hides valid sibling models.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | float) and not isinstance(value, bool):
        temperature = float(value)
    else:
        report(f"recommended_temperature is not a number ({value!r}); ignoring")
        return None
    if not math.isfinite(temperature):
        report(f"recommended_temperature is not finite ({value!r}); ignoring")
        return None
    if temperature < 0.0 or temperature > 2.0:
        report(f"recommended_temperature {temperature:g} is outside [0.0, 2.0]; ignoring")
        return None
    return temperature


def _coerce_recommended_top_p(value: Any, report: ModelDataIssueReport) -> float | None:
    """Coerce a ``recommended_top_p`` record value into a valid float or None.

    Like ``recommended_temperature``, this is an optional model fact that stays
    ``None`` when absent. A non-numeric or out-of-range value is logged and
    treated as unknown rather than failing assembly, so one bad override never
    hides valid sibling models.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | float) and not isinstance(value, bool):
        top_p = float(value)
    else:
        report(f"recommended_top_p is not a number ({value!r}); ignoring")
        return None
    if not math.isfinite(top_p):
        report(f"recommended_top_p is not finite ({value!r}); ignoring")
        return None
    if top_p < 0.0 or top_p > 1.0:
        report(f"recommended_top_p {top_p:g} is outside [0.0, 1.0]; ignoring")
        return None
    return top_p


def _freeze_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_metadata_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_metadata_value(item) for item in value)
    return value


def _normalize_string_tuple(values: Iterable[str], *, sort: bool = False) -> tuple[str, ...]:
    normalized_items: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized_value = value.strip().lower()
        if not normalized_value or normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized_items.append(normalized_value)

    if sort:
        normalized_items.sort()
    return tuple(normalized_items)
