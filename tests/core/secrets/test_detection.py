"""The supported input formats and deliberate false-positive boundary."""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.secrets.detection import contains_potential_api_key, detect_api_keys

_CASES = json.loads(Path(__file__).with_name("api_key_detection_cases.json").read_text())


def _key(case: dict[str, Any]) -> str:
    body = (case["seed"] * 10)[: case["length"]]
    return cast(
        str,
        (
            case["prefix"]
            + body
            + (case.get("middle", "") + body if case.get("repeat_body") else "")
            + case["suffix"]
        ),
    )


@pytest.mark.parametrize("case", _CASES, ids=[case["prefix"] for case in _CASES])
def test_complete_key_formats_preserve_exact_spans(case: dict[str, Any]) -> None:
    value = _key(case)
    text = f'🔑 Use "{value}"; then `{value}`.'
    found = detect_api_keys(text)
    assert [item.value for item in found] == [value, value]
    assert all(item.provider == case["provider"] for item in found)
    assert all(text[item.start : item.end] == value for item in found)
    assert contains_potential_api_key(text)
    # Never carve a plausible key out of a longer token.
    assert not detect_api_keys(f"before{value}after")
    assert not detect_api_keys(value + "_")


@pytest.mark.parametrize(
    "text",
    [
        "API_KEY=0123456789abcdef0123456789abcdef",
        "sk-proj-example",
        "ghp_" + "x" * 36,
        "github_pat_" + "a" * 82,
        "sk-" + "aB3dE6gH9jK2mN5pQ8sT1" * 3,
        "ghp_" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7",
        "{{secret:GITHUB_TOKEN}}",
        "pk_live_" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7",
        "AKIA0123456789ABCDEF",
    ],
)
def test_unrecognized_values_and_placeholders_stay_literal(text: str) -> None:
    assert not detect_api_keys(text)


def test_draft_prefix_protection_precedes_complete_detection() -> None:
    for case in _CASES:
        assert contains_potential_api_key(json.dumps({"text": case["prefix"] + "a"}))
    assert not contains_potential_api_key("Task: review API_KEY and {{secret:OPENAI_KEY}}")


def test_multiple_services_are_reported_in_message_order() -> None:
    values = [_key(_CASES[14]), _key(_CASES[6]), _key(_CASES[0])]
    assert [item.value for item in detect_api_keys("\n".join(values))] == values
