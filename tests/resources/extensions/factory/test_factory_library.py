"""Published Factory definitions retain identity and preserve Project adaptations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from resources.extensions.factory import _library, _workflows
from resources.extensions.factory.store import FactoryError

type Json = dict[str, Any]


def step(identity: str, after: list[str] | None = None, **values: Any) -> Json:
    return {
        "id": identity,
        "name": identity.title(),
        "task": f"Do {identity}",
        "after": after or [],
        **values,
    }


def workflow(*steps: Json) -> Json:
    return _workflows.normalize([{"id": "work", "name": "Work", "steps": list(steps)}])[0]


def template(*steps: Json, version: int = 1, kind: str = "sequence") -> Json:
    return {
        "id": "ftpl_test",
        "version": version,
        "kind": kind,
        "name": "Shared",
        "description": "",
        "workflow": workflow(*steps),
    }


@pytest.mark.parametrize("placement", [None, [], ["a"]])
def test_insertion_copies_identity_and_connects_results(placement: list[str] | None) -> None:
    original = workflow(step("a"), step("b", ["a"]))
    definition = template(step("test"), step("review", ["test"], returns_to="test"))
    before = deepcopy(original)
    result = _library.insert(original, definition, after=placement)
    source = result["sources"][0]
    first, last = (source["step_map"][key] for key in ("test", "review"))
    by_id = {item["id"]: item for item in result["steps"]}
    expected_anchor = ["b"] if placement is None else placement
    assert by_id[first]["after"] == expected_anchor
    assert by_id[last]["after"] == [first]
    assert by_id[last]["returns_to"] == first
    if placement == ["a"]:
        assert by_id["b"]["after"] == [last]
    elif placement == []:
        assert by_id["a"]["after"] == [last]
    assert original == before
    again = _library.insert(result, definition)
    assert len({item["id"] for item in again["steps"]}) == 6
    assert again["sources"][1]["step_map"] != source["step_map"]
    assert _workflows.normalize([again])[0] == again


@pytest.mark.parametrize("choice", [None, "local", "template"])
def test_update_preserves_local_fields_and_requires_conflict_choices(choice: str | None) -> None:
    definition = template(step("review", limits={"tool_calls": 20}), kind="workflow")
    definition["workflow"]["limits"] = {"cost_usd": 2}
    use = _library.insert(None, definition)
    assert use["limits"] == {"cost_usd": 2}
    assert use["steps"][0]["limits"] == {"tool_calls": 20}
    source = use["sources"][0]
    local_id = source["step_map"]["review"]
    use["name"] = "Project review"
    use["steps"][0].update(task="Check the Project API", agent="project-reviewer")
    newer = template(
        step("review", task="Check all acceptance criteria", effort="high", limits={"tokens": 500}),
        version=2,
        kind="workflow",
    )
    resolutions = {f"{local_id}.task": choice} if choice else None
    result = _library.preview_update(use, source["id"], newer, resolutions=resolutions)
    assert result["valid"]
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["resolution"] == (choice or "")
    updated = result["workflow"]
    assert updated["name"] == "Project review"
    assert updated["steps"][0]["agent"] == "project-reviewer"
    assert updated["steps"][0]["effort"] == "high"
    assert "limits" not in updated
    assert updated["steps"][0]["limits"] == {"tokens": 500}
    assert updated["steps"][0]["task"] == (
        "Check all acceptance criteria" if choice == "template" else "Check the Project API"
    )
    assert updated["sources"][0]["version"] == 2
    assert use["sources"][0]["version"] == 1
    repeated = _library.preview_update(updated, source["id"], newer)
    assert not repeated["conflicts"] and not repeated["changes"]
    with pytest.raises(FactoryError) as stale:
        _library.preview_update(
            updated, source["id"], newer, resolutions={f"{local_id}.task": "local"}
        )
    assert stale.value.code == "invalid_resolution"


def test_update_adds_and_removes_steps_and_reconnects_the_following_step() -> None:
    definition = template(step("test"), step("review", ["test"]))
    use = _library.insert(workflow(step("a"), step("b", ["a"])), definition, after=["a"])
    source = use["sources"][0]
    local_test = source["step_map"]["test"]
    local_review = source["step_map"]["review"]
    use["steps"][1]["agent"] = "project-checker"
    newer = template(step("test"), step("proof", ["test"]), version=2)
    result = _library.preview_update(use, source["id"], newer)
    assert result["valid"] and not result["conflicts"]
    updated = result["workflow"]
    mapping = updated["sources"][0]["step_map"]
    assert [item["id"] for item in updated["steps"]] == ["a", local_test, mapping["proof"], "b"]
    assert updated["steps"][1]["agent"] == "project-checker"
    assert updated["steps"][-1]["after"] == [mapping["proof"]]
    assert all(item["id"] != local_review for item in updated["steps"])
    assert _library.preview_update(use, source["id"], newer)["workflow"] == updated

    # A later inserted use is also a consumer of the earlier template's output.
    implementation = template(step("implement"))
    chain = _library.insert(None, implementation)
    implementation_source = chain["sources"][0]
    reviewer = {**template(step("review")), "id": "ftpl_reviewer"}
    chain = _library.insert(chain, reviewer)
    reviewer_id = chain["sources"][1]["step_map"]["review"]
    revision = template(step("implement"), step("proof", ["implement"]), version=2)
    updated_chain = _library.preview_update(chain, implementation_source["id"], revision)
    assert updated_chain["valid"] and not updated_chain["conflicts"]
    updated_workflow = updated_chain["workflow"]
    proof_id = updated_workflow["sources"][0]["step_map"]["proof"]
    assert _workflows.step(updated_workflow, reviewer_id)["after"] == [proof_id]
    reviewer_revision = {
        **template(step("prepare"), step("review", ["prepare"]), version=2),
        "id": "ftpl_reviewer",
    }
    second_update = _library.preview_update(
        updated_workflow, chain["sources"][1]["id"], reviewer_revision
    )
    assert second_update["valid"] and not second_update["conflicts"]
    second_workflow = second_update["workflow"]
    entry_id = second_workflow["sources"][1]["step_map"]["prepare"]
    assert _workflows.step(second_workflow, entry_id)["after"] == [proof_id]
    assert _workflows.step(second_workflow, reviewer_id)["after"] == [entry_id]

    # Explicit later rewiring remains a Project adaptation and needs a choice.
    local_chain = deepcopy(chain)
    _workflows.step(local_chain, reviewer_id)["after"] = []
    local_update = _library.preview_update(local_chain, implementation_source["id"], revision)
    assert any(item["id"] == f"{reviewer_id}.after" for item in local_update["conflicts"])
    assert _workflows.step(local_update["workflow"], reviewer_id)["after"] == []


def test_local_deletions_additions_and_independent_reordering_survive_updates() -> None:
    definition = template(step("a"), step("b"), step("c"))
    use = _library.insert(None, definition)
    source = use["sources"][0]
    ids = source["step_map"]
    use["steps"] = [
        use["steps"][1],
        _workflows.normalize([{"name": "Extra", "steps": [step("local")]}])[0]["steps"][0],
        use["steps"][0],
    ]
    newer = template(step("a", task="New a"), step("b"), step("c"), version=2)
    result = _library.preview_update(use, source["id"], newer)
    assert result["valid"] and not result["conflicts"]
    assert [item["id"] for item in result["workflow"]["steps"]] == [ids["b"], "local", ids["a"]]
    assert result["workflow"]["steps"][-1]["task"] == "New a"

    # Keeping an edited step that upstream removed must keep its Project place.
    kept = _library.insert(None, definition)
    kept_source = kept["sources"][0]
    kept["steps"][1]["task"] = "Project-specific B"
    removed = template(step("a"), step("c"), version=2)
    middle = kept_source["step_map"]["b"]
    preview = _library.preview_update(
        kept, kept_source["id"], removed, resolutions={f"{middle}.step": "local"}
    )
    assert preview["valid"]
    assert [item["id"] for item in preview["workflow"]["steps"]] == [
        item["id"] for item in kept["steps"]
    ]


def test_upstream_reordering_reports_a_conflict_with_different_local_reordering() -> None:
    definition = template(step("a"), step("b"), step("c"))
    use = _library.insert(None, definition)
    source = use["sources"][0]
    use["steps"] = [use["steps"][1], use["steps"][0], use["steps"][2]]
    newer = template(step("c"), step("b"), step("a"), version=2)
    preview = _library.preview_update(use, source["id"], newer)
    assert preview["valid"] and preview["conflicts"][0]["id"] == "workflow.order"
    adopted = _library.preview_update(
        use, source["id"], newer, resolutions={"workflow.order": "template"}
    )
    assert [item["id"] for item in adopted["workflow"]["steps"]] == [
        source["step_map"][key] for key in ("c", "b", "a")
    ]


def test_external_local_dependency_prevents_silent_removal_of_its_target() -> None:
    definition = template(step("a"), step("b", ["a"]))
    use = _library.insert(None, definition)
    source = use["sources"][0]
    target = source["step_map"]["b"]
    use["steps"].append(_workflows._step("local", "Local", "Follow B", [target]))
    newer = template(step("a"), version=2)
    preview = _library.preview_update(use, source["id"], newer)
    assert preview["valid"]
    assert preview["conflicts"][0]["id"] == f"{target}.step"
    removed = _library.preview_update(
        use, source["id"], newer, resolutions={f"{target}.step": "template"}
    )
    assert not removed["valid"] and removed["problems"]


def test_export_link_and_republish_preserve_template_step_identities() -> None:
    original = workflow(
        step("a"),
        step("test", ["a"]),
        step("review", ["test"], returns_to="test"),
        step("land", ["review"]),
    )
    selected = ["test", "review"]
    exported = _library.extract(original, selected)
    assert exported["steps"][0]["after"] == []
    definition = {"id": "ftpl_test", "version": 1, "kind": "sequence", "workflow": exported}
    linked = _library.link(original, definition, step_ids=selected)
    assert linked["steps"] == original["steps"]
    inserted = _library.insert(None, definition)
    source = inserted["sources"][0]
    inserted["steps"][0]["task"] = "Improved review"
    republished = _library.extract(inserted, source_id=source["id"])
    assert [item["id"] for item in republished["steps"]] == selected
    assert republished["steps"][1]["after"] == ["test"]
    assert republished["steps"][1]["returns_to"] == "test"
    assert republished["steps"][0]["task"] == "Improved review"
    inserted["steps"].insert(1, _workflows._step("extra", "Extra", "Check additional evidence", []))
    republished = _library.extract(inserted, source_id=source["id"])
    revision = {**definition, "version": 2, "workflow": republished}
    adopted = _library.preview_update(inserted, source["id"], revision)
    assert adopted["valid"] and not adopted["conflicts"]
    assert [item["id"] for item in adopted["workflow"]["steps"]] == [
        item["id"] for item in inserted["steps"]
    ]
    assert adopted["workflow"]["sources"][0]["step_map"]["extra"] == "extra"

    # Publishing a composed Workflow can explicitly replace its complete uses.
    full = {
        **definition,
        "id": "ftpl_whole",
        "kind": "workflow",
        "workflow": _library.extract(linked),
    }
    snapshot = deepcopy(linked)
    with pytest.raises(FactoryError) as overlap:
        _library.link(linked, full)
    assert overlap.value.code == "source_overlap"
    replacement = _library.link(linked, full, replace_sources=True)
    assert linked == snapshot
    assert replacement["steps"] == linked["steps"]
    assert len(replacement["sources"]) == 1
    assert replacement["sources"][0]["template_id"] == "ftpl_whole"
    partial = {**definition, "workflow": _library.extract(replacement, ["review"]), "kind": "step"}
    with pytest.raises(FactoryError) as partial_overlap:
        _library.link(replacement, partial, step_ids=["review"], replace_sources=True)
    assert partial_overlap.value.code == "source_overlap"
    replacement["steps"].pop(0)
    replacement["steps"][0]["after"] = []
    remaining = {**full, "id": "ftpl_remaining", "workflow": _library.extract(replacement)}
    assert len(_library.link(replacement, remaining, replace_sources=True)["sources"]) == 1


@pytest.mark.parametrize("change", ["same", "task", "extend", "replace"])
def test_linked_consumers_keep_their_selected_output_branches(change: str) -> None:
    original = workflow(step("a"), step("b"), step("consumer", ["a"]))
    published = template(step("a"), step("b"))
    linked = _library.link(original, published, step_ids=["a", "b"])
    source = linked["sources"][0]
    if change == "same":
        newer = published
    elif change == "task":
        newer = template(step("a", task="Improved A"), step("b"), version=2)
    elif change == "extend":
        newer = template(step("a"), step("b"), step("proof", ["a"]), version=2)
    else:
        newer = template(step("replacement"), step("b"), version=2)
    preview = _library.preview_update(linked, source["id"], newer)
    if change == "replace":
        assert any(item["id"] == "consumer.after" for item in preview["conflicts"])
        assert preview["workflow"]["steps"][-1]["after"] == ["a"]
    else:
        assert preview["valid"] and not preview["conflicts"]
        expected = (
            [preview["workflow"]["sources"][0]["step_map"]["proof"]]
            if change == "extend"
            else ["a"]
        )
        assert preview["workflow"]["steps"][-1]["after"] == expected
        if change == "same":
            assert not preview["changes"]
            # Separate inputs of a linked sequence are also separate branches.
            separate_inputs = workflow(step("x"), step("y"), step("a", ["x"]), step("b", ["y"]))
            linked_inputs = _library.link(separate_inputs, published, step_ids=["a", "b"])
            no_change = _library.preview_update(
                linked_inputs, linked_inputs["sources"][0]["id"], published
            )
            assert no_change["valid"] and not no_change["conflicts"]
            assert no_change["workflow"]["steps"] == separate_inputs["steps"]


def test_source_metadata_refuses_identity_rewrites_and_bounded_expansion() -> None:
    definition = template(step("a"), kind="step")
    use = _library.insert(None, definition)
    bad = deepcopy(use)
    bad["steps"][0]["id"] = "Not valid"
    with pytest.raises(FactoryError):
        _workflows.normalize([bad])
    for index in range(_workflows.MAX_STEPS - 1):
        use["steps"].append(_workflows._step(f"local{index}", f"Local {index}", "Work", []))
    with pytest.raises(FactoryError):
        _library.insert(use, definition)
