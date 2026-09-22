"""Identity-reference compensation preserves unrelated Session mutations."""

from __future__ import annotations

import pytest

from tests.core.sessions.sessions_test_support import _address
from tests.core.sessions.sessions_test_support import manager as manager


@pytest.mark.parametrize("new_parent", [False, True])
def test_rename_compensation_only_restores_its_unchanged_parent_reference(manager, new_parent):
    manager.create("worker", session_id="child")
    address = _address("worker", "child")
    original_parent = {
        "id": "original-work",
        "agent_id": "before",
        "session_id": "parent-session",
        "run_id": "parent-run",
        "project_id": None,
    }
    manager.set_metadata(address, {"title": "Before", "subagent_parent": original_parent})

    updates = manager.retarget_identity_agent_references("before", "after")
    assert manager.get_metadata(address)["subagent_parent"]["agent_id"] == "after"

    def concurrent_edit(metadata):
        metadata["title"] = "Concurrent title"
        metadata["unrelated"] = {"retained": True}
        if new_parent:
            # A later delegation can retain the same renamed Agent but establish
            # a different parent Run. Compensation must not rewrite that link.
            metadata["subagent_parent"] = {
                **metadata["subagent_parent"],
                "id": "new-work",
                "run_id": "new-parent-run",
            }

    manager.mutate_metadata(address, concurrent_edit)
    expected_parent = (
        manager.get_metadata(address)["subagent_parent"] if new_parent else original_parent
    )
    manager.restore_identity_agent_references(updates)

    assert manager.get_metadata(address) == {
        "title": "Concurrent title",
        "unrelated": {"retained": True},
        "subagent_parent": expected_parent,
    }
