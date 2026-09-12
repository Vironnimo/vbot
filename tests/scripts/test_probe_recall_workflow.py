"""Workflow evidence must come from production dispatch and observed effects."""

import asyncio
import json

from scripts.provider_probe.recall_cases import recall_cases, recall_matrix
from scripts.provider_probe.workflow_recall import _evaluate_case
from tests.scripts.provider_probe_helpers import PROBE


class SearchAdapter:
    def __init__(self, arguments):
        self.arguments = arguments
        self.seen = []

    async def send(self, messages, **kwargs):
        self.seen.append(list(messages))
        if len(self.seen) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {"id": "search", "name": "session_search", "arguments": self.arguments}
                ],
            }
        return {"content": "30 Tage", "tool_calls": []}

    def normalize_response(self, raw, **kwargs):
        return raw


def test_natural_recall_probe_hides_expectations_and_executes_real_search():
    args = PROBE._parser().parse_args(["--scenario", "recall_workflow"])
    case = recall_cases()[0]
    adapter = SearchAdapter({"query": "Aurora Aufbewahrungsdauer"})
    row = asyncio.run(_evaluate_case(adapter, args, case))
    assert row["passed"]
    assert adapter.seen[0][-1] == {"role": "user", "content": case["task"]}
    assert "Korrektur: Beschlossen" not in json.dumps(adapter.seen[0])
    data = row["observed"][0]["result"]["data"]
    assert data["project_id"] is None
    assert data["items"][0]["session_id"] == "session-retention"
    assert "30 Tage" in str(data["items"][0]["context"])


def test_recovery_probe_executes_failure_and_requires_retrieved_evidence():
    args = PROBE._parser().parse_args(["--scenario", "recall_workflow"])
    case = next(c for c in recall_cases() if c["id"] == "unsupported_option_recovery")
    row = asyncio.run(_evaluate_case(SearchAdapter({"query": "Aurora"}), args, case))
    assert row["passed"]
    assert row["observed"][0]["result"]["error"]["code"] == "invalid_arguments"
    wrong = asyncio.run(_evaluate_case(SearchAdapter({"query": "absent"}), args, case))
    assert not wrong["passed"]


def test_matrix_accepts_equivalent_boolean_representations():
    args = PROBE._parser().parse_args(["--scenario", "recall_workflow"])
    case = next(c for c in recall_matrix() if c["id"] == "matrix_sqlite_fts_encoded_boolean")
    row = asyncio.run(
        _evaluate_case(SearchAdapter({"query": "Aurora", "include_subagents": True}), args, case)
    )
    assert row["passed"]


def test_transcript_success_requires_file_with_full_ordered_conversation():
    args = PROBE._parser().parse_args(["--scenario", "recall_workflow"])
    case = next(c for c in recall_cases() if c["id"] == "transcript")
    row = asyncio.run(_evaluate_case(SearchAdapter({"query": "Aurora"}), args, case))
    assert not row["passed"]
    assert row["checks"]["transcript"] is False
