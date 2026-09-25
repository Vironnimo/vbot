"""The probe runs case files through the production Tool path in fresh Workspaces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.tool_lab import probe
from scripts.tool_lab.probe import CaseFileError, Step, StepOutcome


@pytest.mark.asyncio
async def test_probe_runs_each_case_in_a_fresh_workspace_and_checks_expectations(
    tmp_path: Path,
) -> None:
    case_file = tmp_path / "cases.json"
    case_file.write_text(
        json.dumps(
            {
                "files": {"a.txt": "one\r\ntwo\r\n", "long.md": {"repeat": "row {n}", "count": 3}},
                "cases": [
                    {
                        "name": "edit",
                        "tool": "apply_patch",
                        "arguments": {
                            "patch": "*** Begin Patch\n*** Update File: a.txt\n@@\n one\n-two\n"
                            "+three\n*** End Patch"
                        },
                        "expect": {"ok": True, "files": {"a.txt": "one\r\nthree\r\n"}},
                    },
                    {
                        "name": "read sees the seed again",
                        "tool": "read",
                        "arguments": {"path": "a.txt"},
                        "show": ["long.md"],
                        "expect": {"ok": True, "contains": ["two"], "absent": ["three"]},
                    },
                    {
                        "name": "unmet expectation",
                        "tool": "read",
                        "arguments": {"path": "missing.txt"},
                        "expect": {"ok": True, "files": {"missing.txt": "x"}},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    files, cases = probe.load_cases(case_file)

    outcomes = await probe.run_cases(files, cases, agent_id="main")

    edit, reread, unmet = outcomes
    assert edit.failures == []
    assert reread.failures == []
    assert reread.files == {"long.md": "row 1\nrow 2\nrow 3\n"}
    assert unmet.failures == [
        "ok is False, expected True",
        "missing.txt differs from the expected content",
    ]
    text = probe.report(outcomes, max_chars=0, visible=True)
    assert "--> apply_patch" in text
    assert "--- a.txt\none\\r\nthree\\r\n" in text
    assert "2/3 checked cases pass" in text


def test_probe_step_references_read_earlier_result_data() -> None:
    earlier = StepOutcome(
        Step("bash", {}),
        {},
        {"ok": True, "data": {"process_id": "proc_1", "nested": {"id": 7}}},
        "",
    )

    resolved = probe._resolve_references(
        {"process_id": "$1.process_id", "ids": ["$1.nested.id", "$9 literal"]}, [earlier]
    )

    assert resolved == {"process_id": "proc_1", "ids": [7, "$9 literal"]}
    with pytest.raises(CaseFileError, match="step 2 has not run yet"):
        probe._resolve_references("$2.process_id", [earlier])
    with pytest.raises(CaseFileError, match="has no missing"):
        probe._resolve_references("$1.missing", [earlier])


def test_case_file_needs_a_tool_for_every_step(tmp_path: Path) -> None:
    case_file = tmp_path / "cases.json"
    case_file.write_text(json.dumps({"cases": [{"name": "broken", "steps": [{}]}]}), "utf-8")

    with pytest.raises(CaseFileError, match="broken: every step needs a 'tool' name"):
        probe.load_cases(case_file)
