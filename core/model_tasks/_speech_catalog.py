"""Local speech target catalog and options; constructing it never loads ML packages."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any

from core.model_tasks._speech_context import LocalTranscriptionEngine, SpeechEngineDefinition
from core.model_tasks._speech_engines import (
    _NemotronEngine,
    _ParakeetEngine,
    _QwenEngine,
    _TtsEngine,
    _WaitingWorkers,
)
from core.model_tasks.constants import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH
from core.model_tasks.local_targets import LocalTaskTargetDescriptor
from core.model_tasks.model_files import PinnedModel
from core.model_tasks.options import (
    TaskModelOptionChoice,
    TaskModelOptionField,
)
from core.model_tasks.speech_models import SPEECH_MODELS
from core.model_tasks.speech_setup import LocalSpeechSetup

_LOAD_OPTIONS = ("model_path", "device", "dtype")

# Local speech option asking the Runtime to load the engine after startup and binding changes.
PRELOAD_OPTION = "preload"
# English names of the language codes the local engines accept.
_LANGUAGE_NAMES = {
    "ar": "Arabic",
    "bg": "Bulgarian",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "fa": "Persian",
    "fi": "Finnish",
    "fil": "Filipino",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mk": "Macedonian",
    "ms": "Malay",
    "nb": "Norwegian Bokmål",
    "nl": "Dutch",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sv": "Swedish",
    "sw": "Swahili",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "yue": "Cantonese",
    "zh": "Chinese",
}


def _preload_field(first_use: str) -> TaskModelOptionField:
    return TaskModelOptionField(
        PRELOAD_OPTION,
        "boolean",
        "Load at server start",
        default=False,
        description="Load the model in the background when the vBot server starts or this "
        f"binding changes, so the first {first_use} does not wait for loading. "
        "The model then stays in memory even while unused.",
    )


def _language_choice(value: str, region: str = "") -> TaskModelOptionChoice:
    """A language choice labelled with its English name; *region* tells variants apart."""
    name = _LANGUAGE_NAMES[value.split("-")[0]]
    return TaskModelOptionChoice(value, f"{name} ({region})" if region else name)


def _stt_language_field(choices: Sequence[TaskModelOptionChoice]) -> TaskModelOptionField:
    return TaskModelOptionField(
        "language",
        "select",
        "Language",
        default="",
        description="The language of your recordings. Automatic detects it in each recording.",
        options=(
            TaskModelOptionChoice("", "Automatic"),
            *sorted(choices, key=lambda choice: choice.label),
        ),
    )


def builtin_speech_engines() -> tuple[SpeechEngineDefinition, ...]:
    """Return fresh registrations; importing this module needs no ML packages."""
    common = (
        TaskModelOptionField(
            "device",
            "select",
            "Device",
            default="auto",
            required=True,
            options=tuple(
                TaskModelOptionChoice(value, label)
                for value, label in (
                    ("auto", "Automatic"),
                    ("cuda", "CUDA / ROCm GPU"),
                    ("cpu", "CPU"),
                    ("mps", "Apple GPU"),
                )
            ),
        ),
        TaskModelOptionField(
            "dtype",
            "select",
            "Precision",
            default="auto",
            required=True,
            options=tuple(
                TaskModelOptionChoice(value, label)
                for value, label in (
                    ("auto", "Automatic"),
                    ("float32", "Float32"),
                    ("float16", "Float16"),
                    ("bfloat16", "BFloat16"),
                )
            ),
        ),
        TaskModelOptionField(
            "model_path",
            "text",
            "Model directory",
            default="",
            description="Optional directory on the vBot server with a Transformers model of "
            "this engine's architecture, loaded instead of the installed model. "
            "Leave empty to use the installed model.",
            server_path="directory",
        ),
        _preload_field("transcription"),
    )
    # The codes Qwen3-ASR names in its forced-language prompt.
    qwen_language = _stt_language_field(
        tuple(
            _language_choice(code)
            for code in (
                *("ar", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fil", "fr", "hi"),
                *("hu", "id", "it", "ja", "ko", "mk", "ms", "nl", "pl", "pt", "ro", "ru"),
                *("sv", "th", "tr", "vi", "yue", "zh"),
            )
        )
    )
    # Nemotron's prompt dictionary keys for the locales it emits. A bare code
    # stands for the locale the dictionary maps it to (en: en-US, es: es-US,
    # fr: fr-FR, pt: pt-PT); the other variant is named by its locale.
    nemotron_language = _stt_language_field(
        (
            *(
                _language_choice(code)
                for code in (
                    *("ar", "bg", "cs", "da", "de", "el", "et", "fi", "hi", "hr", "hu", "it"),
                    *("ko", "lt", "lv", "nb", "nl", "nn", "pl", "ro", "ru", "sk", "sl", "sv"),
                    *("tr", "uk", "he-IL", "ja-JP", "th-TH", "vi-VN", "zh-CN"),
                )
            ),
            _language_choice("en", "US"),
            _language_choice("en-GB", "UK"),
            _language_choice("es", "US"),
            _language_choice("es-ES", "Spain"),
            _language_choice("fr", "France"),
            _language_choice("fr-CA", "Canada"),
            _language_choice("pt", "Portugal"),
            _language_choice("pt-BR", "Brazil"),
        )
    )
    qwen_options = (
        qwen_language,
        TaskModelOptionField(
            "prompt",
            "textarea",
            "Vocabulary and context",
            default="",
            description="Optional names or terminology to help recognize your recording.",
        ),
        *common,
    )

    def definition(
        local_id: str,
        label: str,
        license_name: str,
        options: tuple[TaskModelOptionField, ...],
        engine: Callable[[Mapping[str, Any]], LocalTranscriptionEngine],
    ) -> SpeechEngineDefinition:
        model = SPEECH_MODELS[local_id]
        return SpeechEngineDefinition(
            LocalTaskTargetDescriptor(
                id=local_id,
                label=label,
                task_types=(TASK_SPEECH_TO_TEXT,),
                metadata={"license": license_name, "download_bytes": model.download_bytes},
                option_fields=options,
            ),
            engine,
            _LOAD_OPTIONS,
            model,
        )

    return (
        definition("qwen3-asr-1.7b", "Qwen3 ASR 1.7B", "Apache-2.0", qwen_options, _QwenEngine),
        definition("qwen3-asr-0.6b", "Qwen3 ASR 0.6B", "Apache-2.0", qwen_options, _QwenEngine),
        definition("parakeet", "Parakeet TDT v3", "CC-BY-4.0", common, _ParakeetEngine),
        definition(
            "nemotron3.5-asr",
            "Nemotron 3.5 ASR Streaming 0.6B",
            "OpenMDW-1.1",
            (nemotron_language, *common),
            _NemotronEngine,
        ),
    )


def _tts_definitions(
    setup_for: Callable[[str, str, PinnedModel], LocalSpeechSetup],
    waiting: _WaitingWorkers,
) -> tuple[SpeechEngineDefinition, ...]:
    """The local TTS targets; *setup_for(id, environment, model)* supplies each installation."""

    def select(name: str, label: str, default: str, choices: Sequence[str]) -> TaskModelOptionField:
        return TaskModelOptionField(
            name,
            "select",
            label,
            default=default,
            required=True,
            options=tuple(TaskModelOptionChoice(x, x) for x in choices),
        )

    common = (
        select("device", "Device", "auto", ("auto", "cuda", "cpu", "mps")),
        _preload_field("speech output"),
    )
    qwen_options = (
        select(
            "voice",
            "Voice",
            "Ryan",
            ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"),
        ),
        select(
            "language",
            "Language",
            "Auto",
            (
                "Auto",
                "German",
                "English",
                "Chinese",
                "Japanese",
                "Korean",
                "French",
                "Russian",
                "Portuguese",
                "Spanish",
                "Italian",
            ),
        ),
    )
    instructions = TaskModelOptionField(
        "instructions",
        "textarea",
        "Speaking instructions",
        default="",
        description="Optional style instructions, such as a calm or cheerful voice.",
    )
    chatter_options = (
        TaskModelOptionField(
            "language",
            "select",
            "Language",
            default="en",
            required=True,
            options=tuple(
                sorted(
                    (
                        _language_choice(code)
                        for code in (
                            *("ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it"),
                            *("ja", "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr"),
                            "zh",
                        )
                    ),
                    key=lambda choice: choice.label,
                )
            ),
        ),
        TaskModelOptionField(
            "exaggeration",
            "number",
            "Expressiveness",
            default=0.5,
            min_value=0,
            max_value=1,
            step=0.05,
        ),
        TaskModelOptionField(
            "cfg_weight", "number", "Guidance", default=0.5, min_value=0, max_value=1, step=0.05
        ),
        *common,
    )
    definitions = []
    for local_id, environment, label, license_name, fields in (
        (
            "qwen3-tts-1.7b",
            "qwen3-tts",
            "Qwen3-TTS 1.7B",
            "Apache-2.0",
            (*qwen_options, instructions, *common),
        ),
        ("qwen3-tts-0.6b", "qwen3-tts", "Qwen3-TTS 0.6B", "Apache-2.0", (*qwen_options, *common)),
        ("chatterbox", "chatterbox", "Chatterbox Multilingual V3", "MIT", chatter_options),
    ):
        model = SPEECH_MODELS[local_id]
        setup = setup_for(local_id, environment, model)
        definitions.append(
            SpeechEngineDefinition(
                LocalTaskTargetDescriptor(
                    id=local_id,
                    label=label,
                    task_types=(TASK_TEXT_TO_SPEECH,),
                    availability=setup.available,
                    metadata={"license": license_name, "download_bytes": model.download_bytes},
                    option_fields=fields,
                ),
                partial(_TtsEngine, setup, waiting),
                ("device",),
                model,
                environment,
            )
        )
    return tuple(definitions)
