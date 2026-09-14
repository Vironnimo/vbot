"""Independent result expectations for every synthetic search Model case."""

from argparse import Namespace

import pytest

from scripts.provider_probe.workflow_search_files import _case, search_cases


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", [c for c in search_cases() if "arguments" in c], ids=lambda c: c["id"]
)
async def test_search_probe_case_has_a_verified_runtime_expectation(case):
    class Adapter:
        async def send(self, *_args, **_kwargs):
            return {"tool_calls": [{"name": "search_files", "arguments": case["arguments"]}]}

        def normalize_response(self, response, **_kwargs):
            return response

    result = await _case(
        Adapter(),
        Namespace(total_timeout=30, model="fixture", thinking_effort="low", max_tokens=1500),
        case,
    )
    assert result["passed"], result
