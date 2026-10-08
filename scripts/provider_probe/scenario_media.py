"""Provider Tool probe: scenario media."""

from __future__ import annotations

import copy
import json
from typing import Any

from core.tools.image import (
    ANALYZE_IMAGE_TOOL_DESCRIPTION,
    ANALYZE_IMAGE_TOOL_NAME,
    ANALYZE_IMAGE_TOOL_PARAMETERS,
    GENERATE_IMAGE_TEXT_ONLY_TOOL_DESCRIPTION,
    GENERATE_IMAGE_TOOL_DESCRIPTION,
    GENERATE_IMAGE_TOOL_NAME,
    GENERATE_IMAGE_TOOL_PARAMETERS,
)
from core.tools.speech import (
    GENERATE_SPEECH_TOOL_DESCRIPTION,
    GENERATE_SPEECH_TOOL_NAME,
    GENERATE_SPEECH_TOOL_PARAMETERS,
)
from scripts.provider_probe.common import ProbeScenario, _probe_messages


def _analyze_image_scenario(case_name: str) -> ProbeScenario:
    analyze_arguments = {
        "single": {
            "prompt": "Read every visible label and report uncertainty.",
            "images": ["images/photo.png"],
        },
        "multiple": {
            "prompt": "Vergleiche beide Bilder.\nNenne Unterschiede und Unsicherheit.",
            "images": ["images/photo.png", "C:/images/reference.png"],
        },
    }
    expected_arguments = analyze_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"), ensure_ascii=False)
    instruction = (
        f"Call {ANALYZE_IMAGE_TOOL_NAME} exactly once with exactly this JSON object as "
        f"its arguments: {rendered_arguments}. Preserve every character, path, and array "
        "item; do not add any field."
    )
    return ProbeScenario(
        "analyze_image",
        [
            {
                "name": ANALYZE_IMAGE_TOOL_NAME,
                "description": ANALYZE_IMAGE_TOOL_DESCRIPTION,
                "parameters": ANALYZE_IMAGE_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        ANALYZE_IMAGE_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _generate_image_scenario(case_name: str) -> ProbeScenario:
    prompt = "A red fox in snow, cinematic light."
    one_source = ["images/source.png"]
    many_sources = ["images/source.png", "C:/images/reference.png"]
    generate_image_arguments: dict[str, dict[str, Any]] = {
        "full_default": {"prompt": prompt},
        "full_source_one": {"prompt": prompt, "source_images": one_source},
        "full_source_many": {"prompt": prompt, "source_images": many_sources},
        "full_aspect": {"prompt": prompt, "aspect_ratio": "16:9"},
        "full_resolution": {"prompt": prompt, "resolution": "4K"},
        "full_output_dir": {"prompt": prompt, "output_dir": "assets/generated"},
        "full_all": {
            "prompt": "Ändere das Licht.\nBehalte Motiv und Komposition unverändert.",
            "source_images": many_sources,
            "aspect_ratio": "16:9",
            "resolution": "4K",
            "output_dir": "assets/generated",
        },
        "text_default": {"prompt": prompt},
        "text_aspect": {"prompt": prompt, "aspect_ratio": "16:9"},
        "text_resolution": {"prompt": prompt, "resolution": "4K"},
        "text_output_dir": {"prompt": prompt, "output_dir": "assets/generated"},
        "text_all": {
            "prompt": prompt,
            "aspect_ratio": "16:9",
            "resolution": "4K",
            "output_dir": "assets/generated",
        },
    }
    expected_arguments = generate_image_arguments[case_name]
    text_only = case_name.startswith("text_")
    description = (
        GENERATE_IMAGE_TEXT_ONLY_TOOL_DESCRIPTION if text_only else GENERATE_IMAGE_TOOL_DESCRIPTION
    )
    parameters = copy.deepcopy(GENERATE_IMAGE_TOOL_PARAMETERS)
    if text_only:
        parameters["properties"].pop("source_images")
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"), ensure_ascii=False)
    instruction = (
        f"Call {GENERATE_IMAGE_TOOL_NAME} exactly once with exactly this JSON object as "
        f"its arguments: {rendered_arguments}. Preserve every character, path, and array "
        "item; omit every field not shown."
    )
    return ProbeScenario(
        "generate_image",
        [
            {
                "name": GENERATE_IMAGE_TOOL_NAME,
                "description": description,
                "parameters": parameters,
            }
        ],
        _probe_messages(instruction),
        GENERATE_IMAGE_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _generate_speech_scenario(case_name: str) -> ProbeScenario:
    speech_arguments = {
        "plain": {"text": "Please read this sentence aloud."},
        "unicode_multiline": {"text": "Grüße aus Köln.\nZweite Zeile: 你好 — fertig."},
    }
    expected_arguments = speech_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"), ensure_ascii=False)
    instruction = (
        f"Call {GENERATE_SPEECH_TOOL_NAME} exactly once with exactly this JSON object as "
        f"its arguments: {rendered_arguments}. Preserve every character and do not add "
        "any field."
    )
    return ProbeScenario(
        "generate_speech",
        [
            {
                "name": GENERATE_SPEECH_TOOL_NAME,
                "description": GENERATE_SPEECH_TOOL_DESCRIPTION,
                "parameters": GENERATE_SPEECH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        GENERATE_SPEECH_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )
