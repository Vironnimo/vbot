"""Independent result expectations for every synthetic search Model case."""

from argparse import Namespace
from contextlib import contextmanager

import pytest

from scripts.provider_probe.workflow_search_files import _case, search_cases


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", [c for c in search_cases() if "arguments" in c], ids=lambda c: c["id"]
)
async def test_search_probe_case_has_a_verified_runtime_expectation(case):
    class Adapter:
        def request_context_kwargs(self, **_kwargs):
            return {}

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


@pytest.mark.asyncio
@pytest.mark.parametrize("path,allowed", [("src", True), ("../outside", False)])
async def test_search_probe_resolves_fixture_root_before_checking_scope(
    tmp_path, monkeypatch, path, allowed
):
    (tmp_path / "alias").mkdir()
    (tmp_path / "fixture").mkdir()

    @contextmanager
    def temporary_directory(**kwargs):
        # Windows CI's short Temp path similarly differs from Path.resolve().
        yield str(tmp_path / "alias" / ".." / "fixture")

    monkeypatch.setattr(
        "scripts.provider_probe.workflow_search_files.TemporaryDirectory", temporary_directory
    )
    case = {
        "id": "resolved_root",
        "arguments": {"action": "content", "patterns": ["beta"], "paths": [path]},
        "content": "src/a.py:2:beta",
    }

    class Adapter:
        def request_context_kwargs(self, **_kwargs):
            return {}

        async def send(self, *_args, **_kwargs):
            return {"tool_calls": [{"name": "search_files", "arguments": case["arguments"]}]}

        def normalize_response(self, response, **_kwargs):
            return response

    result = await _case(
        Adapter(),
        Namespace(total_timeout=30, model="fixture", thinking_effort="low", max_tokens=1500),
        case,
    )
    assert result["passed"] is allowed
