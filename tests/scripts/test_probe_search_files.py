"""Independent result expectations for every synthetic search Model case."""

from argparse import Namespace
from contextlib import contextmanager

import pytest

from scripts.provider_probe.workflow_search_files import _case, _probe_search_files, search_cases


@pytest.mark.asyncio
async def test_search_probe_rejects_unknown_case_before_any_model_request():
    with pytest.raises(ValueError):
        await _probe_search_files(object(), Namespace(search_case="content_defaults,unknown"))


@pytest.mark.asyncio
async def test_search_probe_selects_every_requested_case(monkeypatch):
    calls = []

    async def evaluate(_adapter, _args, case):
        calls.append(case["id"])
        return {"case": case["id"], "passed": True}

    monkeypatch.setattr("scripts.provider_probe.workflow_search_files._case", evaluate)
    result = await _probe_search_files(
        object(), Namespace(search_case="natural_recipe_call,natural_name_only")
    )
    assert set(calls) == {"natural_recipe_call", "natural_name_only"}
    assert result["cases"] == 2
    assert result["passed"]


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
        "arguments": {"args": ["beta", path]},
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
