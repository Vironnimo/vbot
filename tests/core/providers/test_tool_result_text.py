"""Model-facing text of canonical Tool Result envelopes."""

from __future__ import annotations

import json
from typing import Any

from core.providers._chat_completions_wire import _to_openai_message
from core.providers._opencode_zen_gemini import _to_gemini_content
from core.providers._tool_result_text import (
    tool_result_function_response,
    tool_result_text,
)
from core.providers.adapter import (
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
    project_tool_result_content_fallbacks,
)
from core.providers.github_copilot_messages import _to_tool_result_block
from core.providers.github_copilot_responses import _tool_message_to_function_output
from core.tools import tool_failure, tool_success


def _content(envelope: dict) -> str:
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def test_success_renders_fields_then_the_body_verbatim() -> None:
    content = _content(
        tool_success(
            {
                "status": "completed",
                "exit_code": 1,
                "truncated": False,
                "output": 'line "one"\n\tline two\n',
            }
        )
    )

    assert tool_result_text(content) == (
        'status: completed\nexit_code: 1\ntruncated: false\n\nline "one"\n\tline two\n'
    )


def test_a_body_without_other_fields_is_returned_alone() -> None:
    assert tool_result_text(_content(tool_success({"content": "1\talpha\n"}))) == "1\talpha\n"


def test_content_is_the_body_before_output() -> None:
    text = tool_result_text(_content(tool_success({"output": "second", "content": "first"})))

    assert text == "output: second\n\nfirst"


def test_empty_body_null_fields_and_nested_values() -> None:
    text = tool_result_text(
        _content(
            tool_success(
                {
                    "content": "",
                    "next_offset": None,
                    "files": [{"path": "a.py", "status": "modified"}],
                    "guidance": "Re-read a.py.\nThen retry.",
                }
            )
        )
    )

    assert text == (
        'content: ""\n'
        'files: [{"path":"a.py","status":"modified"}]\n'
        "guidance:\n  Re-read a.py.\n  Then retry."
    )


def test_empty_success_data_reads_ok() -> None:
    assert tool_result_text(_content(tool_success({}))) == "ok"


def test_failure_leads_with_code_and_message() -> None:
    content = _content(
        tool_failure("timeout", "The request timed out.", retryable=True, attempts_made=3)
    )

    assert tool_result_text(content) == (
        "Error (timeout): The request timed out.\nretryable: true\nattempts_made: 3"
    )


def test_artifacts_are_kept_as_a_field() -> None:
    artifact = {"kind": "read_media", "attachment_id": "a1", "filename": "x.png"}
    text = tool_result_text(_content(tool_success({"content": "image"}, [artifact])))

    assert text == (
        'artifacts: [{"kind":"read_media","attachment_id":"a1","filename":"x.png"}]\n\nimage'
    )


def test_content_that_is_not_an_envelope_passes_through() -> None:
    digest = '{"_vbot_compacted_tool_result":true,"tool":"read"}'

    assert tool_result_text("plain legacy text") == "plain legacy text"
    assert tool_result_text(digest) == digest
    assert tool_result_text('{"ok":true,"data":{}}') == '{"ok":true,"data":{}}'
    assert tool_result_text(None) is None


def test_a_body_that_looks_like_an_envelope_is_literal_content() -> None:
    literal = _content(tool_failure("not_found", "This is file content, not a failure."))

    assert tool_result_text(_content(tool_success({"content": literal}))) == literal


def test_function_response_marks_failures_as_errors() -> None:
    success = tool_result_function_response(_content(tool_success({"content": "hi"})))
    failure = tool_result_function_response(_content(tool_failure("not_found", "No file x.")))

    assert success == {"output": "hi"}
    assert failure == {"error": "Error (not_found): No file x."}
    assert tool_result_function_response('{"a":1}') == {"a": 1}
    assert tool_result_function_response("legacy") == {"output": "legacy"}


_SUCCESS = _content(tool_success({"exit_code": 0, "output": 'print("hi")\n'}))
_FAILURE = _content(tool_failure("not_found", "No file x."))
_SUCCESS_TEXT = 'exit_code: 0\n\nprint("hi")\n'
_FAILURE_TEXT = "Error (not_found): No file x."


def _tool_message(content: str) -> dict:
    return {"role": "tool", "tool_call_id": "call_1", "name": "bash", "content": content}


def test_chat_completions_and_text_only_wires_send_the_rendered_text() -> None:
    rich = {
        **_tool_message(_SUCCESS),
        TOOL_RESULT_CONTENT_BLOCKS_FIELD: [{"type": "text", "text": "[Image path: a.png]"}],
    }

    assert _to_openai_message(_tool_message(_FAILURE))["content"] == _FAILURE_TEXT
    projected = project_tool_result_content_fallbacks([rich])[0]
    assert projected["content"] == _SUCCESS
    assert _to_openai_message(projected)["content"] == (f"{_SUCCESS_TEXT}\n\n[Image path: a.png]")


def test_responses_wire_sends_the_rendered_text() -> None:
    output = _tool_message_to_function_output(
        _tool_message(_SUCCESS), document_media_types=frozenset()
    )

    assert output["output"] == _SUCCESS_TEXT


def test_copilot_messages_wire_sends_text_and_marks_failures() -> None:
    success = _to_tool_result_block(_tool_message(_SUCCESS))
    failure = _to_tool_result_block(_tool_message(_FAILURE))

    assert success["content"] == _SUCCESS_TEXT
    assert "is_error" not in success
    assert failure["content"] == _FAILURE_TEXT
    assert failure["is_error"] is True


def test_gemini_wire_sends_output_or_error_objects() -> None:
    def response(content: str) -> Any:
        converted, _ = _to_gemini_content(_tool_message(content))
        assert converted is not None
        return converted["parts"][0]["functionResponse"]["response"]

    assert response(_SUCCESS) == {"output": _SUCCESS_TEXT}
    assert response(_FAILURE) == {"error": _FAILURE_TEXT}
