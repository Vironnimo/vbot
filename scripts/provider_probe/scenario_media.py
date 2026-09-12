"""Provider Tool probe: scenario media."""

from __future__ import annotations

import json
from typing import Any

from core.tools.image import (
    ANALYZE_IMAGE_TOOL_DESCRIPTION,
    ANALYZE_IMAGE_TOOL_NAME,
    ANALYZE_IMAGE_TOOL_PARAMETERS,
    IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TEXT_ONLY_TOOL_PARAMETERS,
    IMAGE_GENERATION_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_NAME,
    IMAGE_GENERATION_TOOL_PARAMETERS,
)
from core.tools.speech import (
    TEXT_TO_SPEECH_TOOL_DESCRIPTION,
    TEXT_TO_SPEECH_TOOL_NAME,
    TEXT_TO_SPEECH_TOOL_PARAMETERS,
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


def _image_generation_scenario(case_name: str) -> ProbeScenario:
    prompt = "A red fox in snow, cinematic light."
    one_source = ["images/source.png"]
    many_sources = ["images/source.png", "C:/images/reference.png"]
    image_generation_arguments: dict[str, dict[str, Any]] = {
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
    expected_arguments = image_generation_arguments[case_name]
    text_only = case_name.startswith("text_")
    description = (
        IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION
        if text_only
        else IMAGE_GENERATION_TOOL_DESCRIPTION
    )
    parameters = (
        IMAGE_GENERATION_TEXT_ONLY_TOOL_PARAMETERS
        if text_only
        else IMAGE_GENERATION_TOOL_PARAMETERS
    )
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"), ensure_ascii=False)
    instruction = (
        f"Call {IMAGE_GENERATION_TOOL_NAME} exactly once with exactly this JSON object as "
        f"its arguments: {rendered_arguments}. Preserve every character, path, and array "
        "item; omit every field not shown."
    )
    return ProbeScenario(
        "image_generation",
        [
            {
                "name": IMAGE_GENERATION_TOOL_NAME,
                "description": description,
                "parameters": parameters,
            }
        ],
        _probe_messages(instruction),
        IMAGE_GENERATION_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _text_to_speech_scenario(case_name: str) -> ProbeScenario:
    speech_arguments = {
        "plain": {"text": "Please read this sentence aloud."},
        "unicode_multiline": {"text": "Grüße aus Köln.\nZweite Zeile: 你好 — fertig."},
    }
    expected_arguments = speech_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"), ensure_ascii=False)
    instruction = (
        f"Call {TEXT_TO_SPEECH_TOOL_NAME} exactly once with exactly this JSON object as "
        f"its arguments: {rendered_arguments}. Preserve every character and do not add "
        "any field."
    )
    return ProbeScenario(
        "text_to_speech",
        [
            {
                "name": TEXT_TO_SPEECH_TOOL_NAME,
                "description": TEXT_TO_SPEECH_TOOL_DESCRIPTION,
                "parameters": TEXT_TO_SPEECH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        TEXT_TO_SPEECH_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )
