"""Dangling tool-result repair and reasoning replay shaping tests."""

from core.providers.reasoning import REASONING_REPLAY_CURRENT_RUN
from tests.core.chat.chat_loop_support import build_chat_loop

from .messages_test_support import (
    ERROR_KIND_PROVIDER_ERROR,
    FIXED_TIMESTAMP,
    INTERRUPTED_TOOL_RESULT_CODE,
    INTERRUPTED_TOOL_RESULT_MESSAGE,
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
    Any,
    ChatMessage,
    ToolCall,
    _assistant_continuation_dict,
    _effective_compaction_messages,
    _embed_notes_into_request,
    _repair_dangling_tool_calls,
    _restore_in_run_assistant_reasoning,
    asyncio,
    json,
    pytest,
)


def _synthesized_failure_envelope() -> dict:
    return {
        "ok": False,
        "error": {
            "code": INTERRUPTED_TOOL_RESULT_CODE,
            "message": INTERRUPTED_TOOL_RESULT_MESSAGE,
        },
        "data": None,
        "artifacts": [],
    }


class TestRepairDanglingToolCalls:
    """The shared history-build path must synthesize tool results for dangling tool_calls."""

    def test_dangling_assistant_followed_by_error_synthesizes_tool_results(self) -> None:
        # Arrange: a history broken by the bug-hunt repro — an assistant turn
        # with tool_calls persisted, but no tool results, followed by an error.
        messages = [
            ChatMessage.user("Do something", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[
                    ToolCall(id="call_one", name="read", arguments={"path": "x"}),
                    ToolCall(id="call_two", name="read", arguments={"path": "y"}),
                ],
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.error(
                ERROR_KIND_PROVIDER_ERROR,
                "Run aborted.",
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        # Act
        request = _embed_notes_into_request(messages)

        # Assert: synthesized tool results come immediately after the assistant
        # turn, followed by the LLM-visible error as a system-reminder note.
        assert [message["role"] for message in request] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "user",
        ]
        for entry, expected_id in zip(request[2:4], ["call_one", "call_two"], strict=True):
            assert entry["role"] == "tool"
            assert entry["tool_call_id"] == expected_id
            assert entry["name"] == "read"
            envelope = json.loads(entry["content"])
            assert envelope == _synthesized_failure_envelope()
        assert request[-1]["role"] == "user"
        assert "Run aborted." in request[-1]["content"]

    def test_partial_results_only_synthesizes_missing_call_preserves_order(self) -> None:
        # Arrange: 2 of 3 sibling tool calls were persisted; the missing one
        # must be synthesized and the existing two kept. Synthesized entries
        # appear in the assistant's original tool-call order relative to each
        # other (this is the only order the repair can establish without
        # re-ordering the existing persisted tool entries).
        messages = [
            ChatMessage.user("Multi", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[
                    ToolCall(id="call_alpha", name="read", arguments={}),
                    ToolCall(id="call_beta", name="read", arguments={}),
                    ToolCall(id="call_gamma", name="read", arguments={}),
                ],
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.tool(
                tool_call_id="call_alpha",
                name="read",
                content=json.dumps({"ok": True, "error": None, "data": {}, "artifacts": []}),
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.tool(
                tool_call_id="call_gamma",
                name="read",
                content=json.dumps({"ok": True, "error": None, "data": {}, "artifacts": []}),
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.user("Next request", timestamp=FIXED_TIMESTAMP),
        ]

        # Act
        request = _embed_notes_into_request(messages)

        # Assert: every tool_call_id is answered, exactly one synthetic entry
        # is added, and the synthetic one is the missing call (beta).
        assert [message["role"] for message in request] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "tool",
            "user",
        ]
        answered = [entry.get("tool_call_id") for entry in request if entry.get("role") == "tool"]
        assert sorted(answered) == ["call_alpha", "call_beta", "call_gamma"]  # type: ignore[type-var]
        synthetic_ids = [
            entry.get("tool_call_id")
            for entry in request
            if entry.get("role") == "tool" and "result_unavailable" in entry.get("content", "")
        ]
        assert synthetic_ids == ["call_beta"]
        # The synthesized entry must come after the dangling assistant turn;
        # the trailing user message stays last.
        assert request[-1]["role"] == "user"
        assert request[-1]["content"] == "Next request"

    def test_compaction_tail_path_gets_same_repair(self, tmp_path) -> None:
        # Arrange: tail of a compacted session contains a dangling assistant turn.
        from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

        agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
        runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
        session = runtime.chat_sessions.create("coder", session_id="session-one")

        tail_user = ChatMessage.user("Current question", timestamp=FIXED_TIMESTAMP)
        session.append(ChatMessage.user("Old question", timestamp=FIXED_TIMESTAMP))
        session.append(ChatMessage.assistant(model=agent.model, content="Old answer"))
        session.append(tail_user)
        tail_assistant = ChatMessage.assistant(
            model=agent.model,
            content=None,
            tool_calls=[ToolCall(id="dangling", name="read", arguments={})],
        )
        session.append(tail_assistant)
        session.append(
            ChatMessage.compaction_checkpoint(
                summary="Compacted earlier turns.",
                projection=[tail_user, tail_assistant],
                compacted_token_count=10,
            )
        )

        # Act: build the compacted request history through the same path the
        # chat loop uses (which calls _embed_notes_into_request internally).
        request_messages = asyncio.run(
            build_chat_loop(runtime)._build_request_messages(agent, session)
        )

        # Assert: dangling tool call is answered with a synthesized failure.
        tool_entries = [entry for entry in request_messages if entry.get("role") == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0]["tool_call_id"] == "dangling"
        assert tool_entries[0]["name"] == "read"
        envelope = json.loads(tool_entries[0]["content"])
        assert envelope == _synthesized_failure_envelope()

    def test_compaction_build_uses_self_contained_projection(self, tmp_path) -> None:
        from core.chat.messages import COMPACTION_SUMMARY_NOTE_PREFIX
        from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

        agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
        runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
        session = runtime.chat_sessions.create("coder", session_id="session-one")

        session.append(ChatMessage.user("Lost tail question", timestamp=FIXED_TIMESTAMP))
        session.append(ChatMessage.assistant(model=agent.model, content="Lost tail answer"))
        session.append(
            ChatMessage.compaction_checkpoint(
                summary="Compacted earlier turns.",
                projection=[
                    ChatMessage.note(f"{COMPACTION_SUMMARY_NOTE_PREFIX}Compacted earlier turns.")
                ],
                compacted_token_count=10,
            )
        )
        session.append(ChatMessage.user("Fresh question", timestamp=FIXED_TIMESTAMP))

        request_messages = asyncio.run(
            build_chat_loop(runtime)._build_request_messages(agent, session)
        )

        summary_entries = [
            entry
            for entry in request_messages
            if entry.get("role") == "user"
            and "Compacted earlier turns." in entry.get("content", "")
        ]
        assert len(summary_entries) == 1
        user_contents = [
            entry.get("content") for entry in request_messages if entry.get("role") == "user"
        ]
        assert any(content == "Fresh question" for content in user_contents)
        assert all("Lost tail question" not in (content or "") for content in user_contents)

    def test_repair_does_not_double_answer_already_answered_calls(self) -> None:
        # Arrange: every tool call already has a matching tool result.
        tool_envelope = json.dumps({"ok": True, "error": None, "data": {"x": 1}, "artifacts": []})
        request: list[dict[str, Any]] = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_ok", "name": "read", "arguments": {}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_ok", "name": "read", "content": tool_envelope},
        ]

        # Act
        repaired = _repair_dangling_tool_calls(request)

        # Assert: no synthetic entries are added.
        assert repaired == request

    def test_repair_preserves_synthesized_name_when_tool_call_has_name(self) -> None:
        # Arrange
        request: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "only_call", "name": "bash", "arguments": {}}],
            },
        ]

        # Act
        repaired = _repair_dangling_tool_calls(request)

        # Assert
        assert len(repaired) == 2
        assert repaired[1]["name"] == "bash"
        envelope = json.loads(repaired[1]["content"])
        assert envelope == _synthesized_failure_envelope()

    def test_repair_uses_unknown_name_when_tool_call_name_missing(self) -> None:
        # Arrange
        request: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "only_call", "arguments": {}}],
            },
        ]

        # Act
        repaired = _repair_dangling_tool_calls(request)

        # Assert
        assert repaired[1]["name"] == "unknown"
        assert repaired[1]["tool_call_id"] == "only_call"

    def test_repaired_entries_are_never_persisted_to_session_jsonl(self, tmp_path) -> None:
        # Arrange: a session with a dangling assistant turn in JSONL, then run
        # the build path. The synthesized entries must show up in the request
        # payload but not in the session file the next time we load it.
        from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

        agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
        runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
        session = runtime.chat_sessions.create("coder", session_id="session-one")

        session.append(ChatMessage.user("Please read", timestamp=FIXED_TIMESTAMP))
        session.append(
            ChatMessage.assistant(
                model=agent.model,
                content=None,
                tool_calls=[ToolCall(id="dangling_one", name="read", arguments={})],
            )
        )
        # No tool result persisted; this is the dangling state.

        jsonl_before = session.path.read_text(encoding="utf-8")

        # Act: run the build path that synthesizes the missing tool result.
        request_messages = asyncio.run(
            build_chat_loop(runtime)._build_request_messages(agent, session)
        )
        jsonl_after = session.path.read_text(encoding="utf-8")

        # Assert: the request payload now contains a synthesized tool entry.
        tool_entries = [entry for entry in request_messages if entry.get("role") == "tool"]
        assert any(entry.get("tool_call_id") == "dangling_one" for entry in tool_entries)
        # Assert: the JSONL file is byte-for-byte unchanged; no synthesized
        # tool message was appended by the repair.
        assert jsonl_after == jsonl_before
        # And re-loading the session still shows the dangling assistant turn
        # (not a tool entry), confirming the repair is request-only.
        reloaded = session.load()
        assert [message.role for message in reloaded] == ["user", "assistant"]


class TestReasoningReplayShaping:
    """History shaping follows the adapter's reasoning replay policy."""

    def _assistant_with_reasoning(self, model: str, content: str | None) -> ChatMessage:
        return ChatMessage.assistant(
            model=model,
            content=content,
            reasoning="Readable thinking.",
            reasoning_meta={"content_blocks": [{"type": "thinking", "signature": "signed"}]},
            timestamp=FIXED_TIMESTAMP,
        )

    def test_full_history_keeps_reasoning_on_same_model_entries(self) -> None:
        # Arrange: persisted model carries a connection suffix; the gate must
        # compare bare models on both sides.
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            self._assistant_with_reasoning("anthropic/claude-sonnet-4::api-key", "Answer"),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4::api-key",
        )

        assert request[1]["reasoning"] == "Readable thinking."
        assert request[1]["reasoning_meta"] == {
            "content_blocks": [{"type": "thinking", "signature": "signed"}]
        }
        assert "usage" not in request[1]

    @pytest.mark.parametrize("interruption_cause", ["user", "provider", "timeout"])
    def test_full_history_strips_interrupted_same_scope_reasoning(
        self,
        interruption_cause: str,
    ) -> None:
        interrupted = ChatMessage.assistant(
            model="anthropic/claude-sonnet-4::api-key",
            content="Partial answer",
            reasoning="Incomplete readable thinking.",
            reasoning_meta={
                "content_blocks": [{"type": "thinking", "signature": "incomplete-signed"}]
            },
            reasoning_scope="anthropic/claude-sonnet-4::api-key",
            interrupted=True,
            interruption_cause=interruption_cause,
            timestamp=FIXED_TIMESTAMP,
        )
        complete = self._assistant_with_reasoning(
            "anthropic/claude-sonnet-4::api-key",
            "Complete answer",
        )
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            interrupted,
            ChatMessage.user("Follow up", timestamp=FIXED_TIMESTAMP),
            complete,
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4::api-key",
        )

        interrupted_entry = request[1]
        assert interrupted_entry["content"] == "Partial answer"
        assert "reasoning" not in interrupted_entry
        assert "reasoning_meta" not in interrupted_entry
        assert "interrupted" not in interrupted_entry
        assert "interruption_cause" not in interrupted_entry
        assert request[3]["reasoning"] == "Readable thinking."
        assert request[3]["reasoning_meta"] == {
            "content_blocks": [{"type": "thinking", "signature": "signed"}]
        }
        assert interrupted.reasoning == "Incomplete readable thinking."
        assert interrupted.reasoning_meta == {
            "content_blocks": [{"type": "thinking", "signature": "incomplete-signed"}]
        }

    def test_full_history_drops_interrupted_reasoning_only_entry(self) -> None:
        interrupted = ChatMessage.assistant(
            model="anthropic/claude-sonnet-4::api-key",
            content=None,
            reasoning="Incomplete readable thinking.",
            reasoning_meta={"signature": "incomplete-signed"},
            interrupted=True,
            interruption_cause="provider",
            timestamp=FIXED_TIMESTAMP,
        )

        request = _embed_notes_into_request(
            [
                ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
                interrupted,
                ChatMessage.user("Follow up", timestamp=FIXED_TIMESTAMP),
            ],
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4::api-key",
        )

        assert [entry["role"] for entry in request] == ["user", "user"]
        assert interrupted.reasoning == "Incomplete readable thinking."
        assert interrupted.reasoning_meta == {"signature": "incomplete-signed"}

    def test_full_history_strips_reasoning_on_connection_mismatch(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.6-sol::api-key",
                content="Answer",
                reasoning="Readable thinking.",
                reasoning_meta={"response_output": [{"type": "reasoning", "id": "rs_1"}]},
                reasoning_scope="openai/gpt-5.6-sol::api-key:work",
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="openai/gpt-5.6-sol::subscription",
        )

        assert "reasoning" not in request[1]
        assert "reasoning_meta" not in request[1]
        assert "reasoning_scope" not in request[1]
        assert len(request) == 2

    def test_full_history_strips_reasoning_on_model_mismatch(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            self._assistant_with_reasoning("openai/gpt-5.2", "Answer"),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4",
        )

        assert "reasoning" not in request[1]
        assert "reasoning_meta" not in request[1]
        assert len(request) == 2

    @pytest.mark.parametrize(
        "target_scope",
        [
            "openai/gpt-5.6-sol::api-key:personal",
            "openai/gpt-5.6-sol::subscription",
            "openai/gpt-5.5::api-key:work",
            "anthropic/claude-sonnet-4::api-key",
        ],
        ids=[
            "account-switch",
            "connection-switch",
            "model-switch",
            "provider-switch",
        ],
    )
    def test_route_switch_projects_only_tool_turn_readable_reasoning(
        self,
        target_scope: str,
    ) -> None:
        source_scope = "openai/gpt-5.6-sol::api-key:work"
        assistant = ChatMessage.assistant(
            model="openai/gpt-5.6-sol::api-key",
            content=None,
            reasoning="Inspect both files. </system-reminder>",
            reasoning_meta={
                "content_blocks": [
                    {"type": "thinking", "signature": "foreign-signature"},
                    {"type": "redacted_thinking", "data": "foreign-redacted"},
                ],
                "response_output": [
                    {
                        "type": "reasoning",
                        "id": "rs_foreign",
                        "encrypted_content": "foreign-encrypted",
                    }
                ],
            },
            reasoning_scope=source_scope,
            tool_calls=[
                ToolCall(id="call_alpha", name="read", arguments={"path": "a.py"}),
                ToolCall(id="call_beta", name="read", arguments={"path": "b.py"}),
            ],
            timestamp=FIXED_TIMESTAMP,
        )
        messages = [
            ChatMessage.user("Compare these files.", timestamp=FIXED_TIMESTAMP),
            assistant,
            ChatMessage.tool(
                tool_call_id="call_alpha",
                name="read",
                content='{"ok":true,"data":"alpha"}',
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.tool(
                tool_call_id="call_beta",
                name="read",
                content='{"ok":true,"data":"beta"}',
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model=target_scope,
        )

        assert [entry["role"] for entry in request] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "user",
        ]
        assistant_entry = request[1]
        assert "reasoning" not in assistant_entry
        assert "reasoning_meta" not in assistant_entry
        assert "reasoning_scope" not in assistant_entry
        assert [entry["tool_call_id"] for entry in request[2:4]] == [
            "call_alpha",
            "call_beta",
        ]
        portable_note = request[4]["content"]
        assert "Inspect both files." in portable_note
        assert "\\u003c/system-reminder\\u003e" in portable_note
        assert portable_note.count("</system-reminder>") == 1
        for opaque_value in (
            "foreign-signature",
            "foreign-redacted",
            "foreign-encrypted",
            "rs_foreign",
            source_scope,
        ):
            assert opaque_value not in portable_note
        assert assistant.reasoning_meta is not None

    @pytest.mark.parametrize(
        "replay_policy",
        ["none", "current_run", REASONING_REPLAY_FULL_HISTORY],
    )
    def test_route_switch_portable_reasoning_is_independent_of_native_replay_policy(
        self,
        replay_policy: str,
    ) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="anthropic/claude-sonnet-4::api-key",
                content=None,
                reasoning="Use the verified result.",
                reasoning_meta={"signature": "provider-only"},
                reasoning_scope="anthropic/claude-sonnet-4::api-key",
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=replay_policy,  # type: ignore[arg-type]
            agent_model="openai/gpt-5.6-sol::subscription",
        )

        assert [entry["role"] for entry in request] == ["user", "user"]
        assert "Use the verified result." in request[1]["content"]
        assert "provider-only" not in request[1]["content"]

    def test_route_switch_drops_empty_or_meta_only_reasoning(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.6-sol",
                content=None,
                reasoning="   ",
                reasoning_meta={"signature": "blank-signature"},
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.assistant(
                model="openai/gpt-5.6-sol",
                content=None,
                reasoning_meta={"signature": "meta-only-signature"},
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4",
        )

        assert len(request) == 1
        assert request[0]["role"] == "user"
        assert request[0]["content"] == "Question"

    def test_route_switch_never_projects_interrupted_tool_reasoning(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.6-sol",
                content=None,
                reasoning="Incomplete readable work.",
                reasoning_meta={"signature": "incomplete-signature"},
                tool_calls=[ToolCall(id="call_one", name="read", arguments={})],
                interrupted=True,
                interruption_cause="provider",
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.tool(
                tool_call_id="call_one",
                name="read",
                content='{"ok":true}',
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4",
        )

        assert [entry["role"] for entry in request] == ["user", "assistant", "tool"]
        serialized = json.dumps(request)
        assert "Incomplete readable work." not in serialized
        assert "incomplete-signature" not in serialized

    def test_route_switch_repairs_dangling_tools_before_portable_reasoning(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.6-sol",
                content=None,
                reasoning="The Tool result is still needed.",
                reasoning_meta={"signature": "foreign-signature"},
                tool_calls=[ToolCall(id="dangling", name="read", arguments={})],
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4",
        )

        assert [entry["role"] for entry in request] == ["user", "assistant", "tool", "user"]
        assert request[2]["tool_call_id"] == "dangling"
        assert "result_unavailable" in request[2]["content"]
        assert "The Tool result is still needed." in request[3]["content"]

    def test_route_switch_projects_unconsumed_tool_reasoning_across_compaction(self) -> None:
        assistant = ChatMessage.assistant(
            model="openai/gpt-5.6-sol::api-key",
            content=None,
            reasoning="Use the Tool output after Compaction.",
            reasoning_meta={"signature": "foreign-signature"},
            reasoning_scope="openai/gpt-5.6-sol::api-key:work",
            tool_calls=[ToolCall(id="call_one", name="read", arguments={})],
            timestamp=FIXED_TIMESTAMP,
        )
        messages = [
            ChatMessage.user("Old question", timestamp=FIXED_TIMESTAMP),
            assistant,
            ChatMessage.tool(
                tool_call_id="call_one",
                name="read",
                content='{"ok":true}',
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.compaction_checkpoint(
                summary="Earlier context.",
                projection=[],
                compacted_token_count=10,
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        effective = _effective_compaction_messages(messages)
        request = _embed_notes_into_request(
            effective,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4::api-key",
        )

        assert [entry["role"] for entry in request] == ["user", "assistant", "tool", "user"]
        assert "Earlier context." in request[0]["content"]
        assert request[2]["tool_call_id"] == "call_one"
        assert "Use the Tool output after Compaction." in request[3]["content"]
        assert "foreign-signature" not in json.dumps(request)

    def test_explicit_current_run_strips_reasoning_even_for_same_model(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            self._assistant_with_reasoning("anthropic/claude-sonnet-4", "Answer"),
        ]

        request = _embed_notes_into_request(
            messages,
            agent_model="anthropic/claude-sonnet-4",
            replay_policy=REASONING_REPLAY_CURRENT_RUN,
        )

        assert "reasoning" not in request[1]
        assert "reasoning_meta" not in request[1]

    def test_current_run_strips_opaque_reasoning_but_preserves_phase(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.5",
                content="Answer",
                reasoning="Readable thinking.",
                reasoning_meta={"response_output": [{"type": "reasoning", "id": "rs_1"}]},
                phase="commentary",
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        request = _embed_notes_into_request(
            messages,
            agent_model="openai/gpt-5.5",
            replay_policy=REASONING_REPLAY_CURRENT_RUN,
        )

        assert request[1]["phase"] == "commentary"
        assert "reasoning" not in request[1]
        assert "reasoning_meta" not in request[1]

    def test_full_history_keeps_same_model_reasoning_only_turn_in_history(self) -> None:
        # Arrange: a reasoning-only assistant turn (no content, no tool calls).
        # Same model → it survives the gate and must stay in the request;
        # mismatched model → the empty Assistant entry is skipped while its
        # readable work is projected as provider-neutral context.
        same_model = self._assistant_with_reasoning("anthropic/claude-sonnet-4", None)
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            same_model,
            ChatMessage.user("Follow up", timestamp=FIXED_TIMESTAMP),
            self._assistant_with_reasoning("openai/gpt-5.2", None),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4",
        )

        assert [message["role"] for message in request] == [
            "user",
            "assistant",
            "user",
            "user",
        ]
        assert request[1]["id"] == same_model.id
        assert request[1]["reasoning"] == "Readable thinking."
        assert "Readable thinking." in request[3]["content"]

    def test_none_policy_strips_reasoning_from_live_continuation_dict(self) -> None:
        message = self._assistant_with_reasoning("anthropic/claude-sonnet-4", "Answer")

        continuation = _assistant_continuation_dict(message, replay_policy=REASONING_REPLAY_NONE)
        default_continuation = _assistant_continuation_dict(message)

        assert "reasoning" not in continuation
        assert "reasoning_meta" not in continuation
        assert default_continuation["reasoning"] == "Readable thinking."

    def test_restore_in_run_assistant_reasoning_restores_all_matching_turns(self) -> None:
        # Arrange: the live request list carries reasoning for two in-run
        # assistant turns; the rebuilt list (post-compaction) lost both. The
        # old behavior restored only the latest tool-continuation turn.
        live_messages: list[dict[str, Any]] = [
            {
                "id": "assistant-one",
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_one", "name": "read", "arguments": {}}],
                "reasoning": "First step.",
                "reasoning_meta": {"signature": "one"},
            },
            {"id": "tool-one", "role": "tool", "tool_call_id": "call_one", "content": "{}"},
            {
                "id": "assistant-two",
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_two", "name": "read", "arguments": {}}],
                "reasoning": "Second step.",
                "reasoning_meta": {"signature": "two"},
            },
            {"id": "tool-two", "role": "tool", "tool_call_id": "call_two", "content": "{}"},
        ]
        rebuilt_messages: list[dict[str, Any]] = [
            {"role": "user", "content": "<system-reminder>\nSummary.\n</system-reminder>"},
            {
                "id": "assistant-one",
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_one", "name": "read", "arguments": {}}],
            },
            {"id": "tool-one", "role": "tool", "tool_call_id": "call_one", "content": "{}"},
            {
                "id": "assistant-two",
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_two", "name": "read", "arguments": {}}],
            },
            {"id": "tool-two", "role": "tool", "tool_call_id": "call_two", "content": "{}"},
        ]

        restored = _restore_in_run_assistant_reasoning(rebuilt_messages, live_messages)

        assert restored[1]["reasoning"] == "First step."
        assert restored[1]["reasoning_meta"] == {"signature": "one"}
        assert restored[3]["reasoning"] == "Second step."
        assert restored[3]["reasoning_meta"] == {"signature": "two"}
        assert "reasoning" not in restored[0]

    def test_restore_in_run_assistant_reasoning_skips_unmatched_entries(self) -> None:
        # Arrange: a historical assistant turn that is not part of the live
        # request list must stay stripped after the rebuild.
        live_messages: list[dict[str, Any]] = [
            {
                "id": "assistant-live",
                "role": "assistant",
                "content": "Live answer",
                "reasoning": "Live thinking.",
            },
        ]
        rebuilt_messages: list[dict[str, Any]] = [
            {"id": "assistant-old", "role": "assistant", "content": "Old answer"},
            {"id": "assistant-live", "role": "assistant", "content": "Live answer"},
        ]

        restored = _restore_in_run_assistant_reasoning(rebuilt_messages, live_messages)

        assert "reasoning" not in restored[0]
        assert restored[1]["reasoning"] == "Live thinking."
