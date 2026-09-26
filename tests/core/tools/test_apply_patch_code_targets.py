"""Code locators copied from a Swarm must keep their intended target."""

from __future__ import annotations

import pytest

from core.tools.apply_patch import register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.tools import ToolCall, ToolExecutionConfig, ToolExecutor, ToolRegistry


@pytest.fixture
def dispatch(tmp_path):
    registry = ToolRegistry()
    register_apply_patch_tool(registry, file_state=FileReadState())
    config = ToolExecutionConfig(
        agent_id="agent-test",
        session_id="session-test",
        run_id="run-test",
        workspace=tmp_path,
        data_root=tmp_path,
        vbot_root=tmp_path,
        allowed_tools=["apply_patch"],
        input_contracts={"apply_patch": registry.get("apply_patch").contract},
    )

    async def run(patch):
        results = await ToolExecutor(registry).execute_many(
            [ToolCall(id="call-test", name="apply_patch", arguments={"patch": patch})], config
        )
        return results[0]

    return run


_BUFFER = """fn undo(&mut self) -> bool {
    let ok = self.history.undo(&mut self.text, &mut self.cursor);
    if ok {
        if self.track_revision {
            self.revision += 1;
        }
        self.shadow = Some(self.text.clone());
        self.shadow_cursor = self.cursor;
    }
    ok
}

fn redo(&mut self) -> bool {
    let ok = self.history.redo(&mut self.text, &mut self.cursor);
    if ok {
        self.revision += 1;
        self.shadow = Some(self.text.clone());
        self.shadow_cursor = self.cursor;
    }
    ok
}
"""


def _hunk(operation, *, guard=False):
    revision = (
        "         if self.track_revision {\n             self.revision += 1;\n         }\n"
        if guard
        else "         self.revision += 1;\n"
    )
    return (
        "@@\n"
        f"     let ok = self.history.{operation}(&mut self.text, &mut self.cursor);\n"
        "     if ok {\n" + revision + "-        self.shadow = Some(self.text.clone());\n"
        "+        self.shadow = Some(counted_clone(&self.text));\n"
        "         self.shadow_cursor = self.cursor;\n"
        "     }\n"
        "     ok\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("both", [False, True])
async def test_stale_undo_context_never_selects_redo(dispatch, tmp_path, both):
    target = tmp_path / "buffer.rs"
    target.write_text(_BUFFER, encoding="utf-8")
    patch = "*** Update File: buffer.rs\n" + _hunk("undo")
    if both:
        patch += _hunk("redo")

    result = await dispatch(patch)

    expected = _BUFFER
    if both:
        start = expected.index("fn redo")
        expected = expected[:start] + expected[start:].replace(
            "Some(self.text.clone())", "Some(counted_clone(&self.text))"
        )
        assert result["data"]["status"] == "partial"
        assert "hunk 1" in result["data"]["content"]
    else:
        assert not result["ok"]
        assert result["error"]["code"] == "text_not_found"
    assert target.read_text(encoding="utf-8") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("indent", ["    ", "\t"])
async def test_actual_undo_context_keeps_redo_unchanged(dispatch, tmp_path, indent):
    target = tmp_path / "buffer.rs"
    original = _BUFFER.replace("    ", indent)
    target.write_text(original, encoding="utf-8")

    result = await dispatch("*** Update File: buffer.rs\n" + _hunk("undo", guard=True))

    assert result["data"]["status"] == "applied"
    assert target.read_text(encoding="utf-8") == original.replace(
        "Some(self.text.clone())", "Some(counted_clone(&self.text))", 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("copied", "held"),
    [
        ("let ok = journal.undo(data);", "let ok = journal.redo(data);"),
        ("let ok = undo(data);", "let ok = redo(data);"),
        ("let ok = journal.primary;", "let ok = journal.backup;"),
        ("fn undo(data: State) {", "fn redo(data: State) {"),
        ("let ok = Undo::execute(data);", "let ok = Redo::execute(data);"),
        ("let ok = journal->undo(data);", "let ok = journal->redo(data);"),
    ],
)
async def test_plain_code_names_are_not_prose_substitutions(dispatch, tmp_path, copied, held):
    target = tmp_path / "target.txt"
    tail = "    let value = data.current_state();\n    let answer = value + delta;\n"
    original = held + "\n" + tail
    target.write_text(original, encoding="utf-8")
    patch = (
        "*** Update File: target.txt\n@@\n " + copied + "\n"
        "     let value = data.current_state();\n"
        "-    let answer = value + delta;\n"
        "+    let answer = value + offset;\n"
    )

    result = await dispatch(patch)

    assert not result["ok"]
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_a_unique_misspelled_call_name_still_recovers(dispatch, tmp_path):
    target = tmp_path / "target.txt"
    original = "let ok = journal.restore(data);\n    let answer = value + delta;\n"
    target.write_text(original, encoding="utf-8")

    result = await dispatch(
        "*** Update File: target.txt\n@@\n"
        " let ok = journal.restroe(data);\n"
        "-    let answer = value + delta;\n"
        "+    let answer = value + offset;\n"
    )

    assert result["data"]["status"] == "applied"
    assert target.read_text(encoding="utf-8") == original.replace("delta", "offset")


@pytest.mark.asyncio
async def test_surrounding_anchors_do_not_override_a_known_member_name(dispatch, tmp_path):
    target = tmp_path / "target.txt"
    original = (
        "let previous = journal.primary;\n"
        "fn dispatch(data: State) {\n"
        "    let selected = journal.backup;\n"
        "    let result = selected + delta;\n"
        "    return result;\n"
    )
    target.write_text(original, encoding="utf-8")

    result = await dispatch(
        "*** Update File: target.txt\n@@\n"
        " fn dispatch(data: State) {\n"
        "     let selected = journal.primary;\n"
        "-    let result = selected + delta;\n"
        "+    let result = selected + offset;\n"
        "     return result;\n"
    )

    assert not result["ok"]
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_an_existing_nearby_name_is_not_a_typo(dispatch, tmp_path):
    target = tmp_path / "target.txt"
    original = (
        "let elsewhere = journal.restroe(other);\n"
        "let ok = journal.restore(data);\n"
        "    let answer = value + delta;\n"
    )
    target.write_text(original, encoding="utf-8")

    result = await dispatch(
        "*** Update File: target.txt\n@@\n"
        " let ok = journal.restroe(data);\n"
        "-    let answer = value + delta;\n"
        "+    let answer = value + offset;\n"
    )

    assert not result["ok"]
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_explicit_name_changes_are_applied_to_the_named_preimage(dispatch, tmp_path):
    target = tmp_path / "target.txt"
    original = "let ok = journal.undo(data);\n    let answer = value + delta;\n"
    target.write_text(original, encoding="utf-8")

    result = await dispatch(
        "*** Update File: target.txt\n@@\n"
        "-let ok = journal.undo(data);\n"
        "+let ok = journal.redo(data);\n"
        "     let answer = value + delta;\n"
    )

    assert result["data"]["status"] == "applied"
    assert target.read_text(encoding="utf-8") == original.replace("undo", "redo")
