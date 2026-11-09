"""Search failure phases and bounded partial-result recovery through dispatch."""

from types import SimpleNamespace

import pytest

from core.tools._search_execution import ScanResult, SearchExecution
from tests.core.tools.search_files_test_support import context, dispatch, search_registry


@pytest.mark.asyncio
async def test_count_timeout_reserves_time_for_content(tmp_path, monkeypatch):
    import core.tools.search as shared

    (tmp_path / "a.txt").write_text("token\n")
    now = 0.0
    monkeypatch.setattr(shared, "time", SimpleNamespace(monotonic=lambda: now))

    def partial_count(execution, _query, _patterns, _scope):
        nonlocal now
        now = 25.0
        assert not execution.budget.keep_going()
        return ScanResult(entries=[(b"a.txt", 1)], files_searched=1, interrupted=True)

    monkeypatch.setattr(SearchExecution, "count_scan", partial_count)
    result = await dispatch(tmp_path, {"pattern": "token", "limit": 1})
    assert result["ok"], result
    assert result["data"]["content"] == "a.txt\n1:token"
    assert result["data"].get("warnings")
    assert "1-0" not in result["data"]["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["engine", "data"])
async def test_internal_failures_are_not_argument_errors(tmp_path, monkeypatch, failure):
    registry = search_registry()
    (tmp_path / "a.txt").write_text("token")

    def fail(*_args, **_kwargs):
        raise ValueError("internal sentinel")

    if failure == "engine":
        monkeypatch.setattr("core.tools.search_files.require_binary", fail)
        help_result = await registry.dispatch(context(tmp_path), {"args": ["--help"]})
        assert help_result["ok"], help_result
    else:
        monkeypatch.setattr(SearchExecution, "line_events", fail)
    result = await registry.dispatch(context(tmp_path), {"pattern": "token"})
    assert result["error"]["code"] == ("tool_not_ready" if failure == "engine" else "search_error")
    if failure == "data":
        assert "internal sentinel" not in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["--null-data", "-z", "--search-zip"])
async def test_unsupported_record_and_process_modes_are_refused_before_search(
    tmp_path, monkeypatch, flag
):
    def unexpected(*_args, **_kwargs):
        pytest.fail("unsupported query reached the search engine")

    monkeypatch.setattr(SearchExecution, "count_scan", unexpected)
    result = await dispatch(tmp_path, {"pattern": "token", "args": [flag]})
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_paging_never_offers_an_offset_the_tool_rejects(tmp_path, monkeypatch):
    # Use the real two-pass path with a small offset cap instead of a million rows.
    monkeypatch.setattr("core.tools.search_files.MAX_OFFSET", 2)
    monkeypatch.setattr("core.tools._search_results.MAX_OFFSET", 2)
    monkeypatch.setattr("core.tools._search_arguments.MAX_OFFSET", 2)
    (tmp_path / "a.txt").write_text("token\n" * 6)
    result = await dispatch(tmp_path, {"pattern": "token", "offset": 2, "limit": 2})
    assert result["ok"], result
    assert result["data"]["content"] == "a.txt\n3:token\n4:token"
    assert "next_offset" not in result["data"]
    refused = await dispatch(tmp_path, {"pattern": "token", "offset": 4})
    assert refused["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 10])
async def test_max_count_keeps_matching_trailing_context(tmp_path, limit):
    (tmp_path / "a.py").write_text("import os\ntoken = 1\nprint(token)\n# token three\nend = 0\n")
    result = await dispatch(tmp_path, {"pattern": "token", "args": ["-m1", "-C1"], "limit": limit})
    assert result["ok"], result
    assert result["data"]["content"] == "a.py\n1-import os\n2:token = 1\n3-print(token)"
    assert "next_offset" not in result["data"]
