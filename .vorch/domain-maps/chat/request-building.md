# Chat Request Building

Task-gated reference for turning canonical Session history into one provider-ready request. Read this when changing message rendering, notes, Skills, Content Blocks, Compaction-tail reconstruction, reasoning replay, or Tool-cycle repair; it is not required for unrelated Chat work.

## Pipeline and ownership

`core/chat/messages.py` owns canonical message validation and history shaping; `core/chat/chat.py::_build_request_state` coordinates the build; `core/chat/block_resolver.py` resolves attachment-backed content; `core/chat/file_mentions.py` snapshots and renders `@` mentions; `core/chat/tool_dispatch.py` owns Tool definitions and Skill activation. Provider adapters receive normalized request messages and definitions only. They own wire translation, while `core/sessions/` remains the persistence owner and `core/compaction/` owns checkpoint policy and Projection creation.

Each provider request is assembled from one loaded canonical Session snapshot. Chat first derives Session-scoped Tool grants, obtains the allowed/ready Tool definitions, applies effective Model-route gates, passes the surviving Tool names into System Prompt assembly, and only then shapes message history. The current route gate keeps `analyze_image` only when the target Model/Adapter intersection cannot carry images and the configured `image_understanding` binding is live-usable. The System Prompt is assembled whole by `SystemPromptManager`; it is never appended to Session history, and an empty/whitespace prompt is omitted from the provider request.

When a Compaction checkpoint exists, its canonical Projection replaces compacted history. If that Projection omitted the newest complete, unconsumed Assistant Tool-call batch, `_overlay_pending_tool_batch` adds the canonical batch until a later Assistant turn consumes it. Checkpoints themselves are never sent directly. Auto-compaction rebuilds and the live path both flow through the same note embedding and dangling-Tool repair.

## Canonical roles and request visibility

- `system`, `user`, `assistant`, and `tool` map to provider-visible conversation roles after shaping. Only `user` may persist `list[ContentBlock]`; other content is string-or-null according to the `ChatMessage` validator.
- `note` is a kernel-internal persistence role. Public history filters it. Ordinary notes become synthetic user messages containing `<system-reminder>` blocks; consecutive notes are grouped, and notes created during a Tool cycle are delayed until all sibling Tool Results have been persisted so they never split the cycle.
- `[skill-context] ` notes are the exception to ordinary note rendering: they become chronological standalone `<skill_content>` user messages. A Skill loaded through the `skill` Tool has no such note because the Tool Result already replays its content.
- `[channel-message] ` notes are coalesced into a separate untrusted-background user message, not a System Reminder. Stored records are JSON-quoted and escape control characters plus angle brackets so channel participants cannot break or impersonate the wrapper.
- `[reply-surface] ` notes record WebUI/Desktop or Channel identity in append-only Session chronology. At executor start Chat appends one only for the first surface, a surface change, or the first interactive Run after a newer checkpoint; the tag is stripped and the model sees a turn-bounded System Reminder. Runs without a supplied surface do not change this state.
- `run_summary` and `agent_takeover` remain visible history annotations but never enter provider requests. `error` enters later requests only when `error_kind_llm_visible` allows it. `compaction_checkpoint` contributes through its Projection, not as a role.
- User `sender` attribution is rendered only while building the provider request and is sanitized before insertion. `input_origin="speech_transcription"` appends a hidden System Reminder immediately before the unchanged visible user message.

## Reasoning replay

Adapters declare `ReasoningReplayPolicy` (`none`, `current_run`, or `full_history`) for the active Connection/Wire and Model. Chat owns history shaping and never interprets `reasoning_meta`. `none` strips readable and opaque reasoning everywhere; `current_run` preserves it only across Tool continuation inside the active Run; `full_history` may replay persisted reasoning only when the Assistant's internal `reasoning_scope` exactly matches the current resolved Provider/Model/Connection/Account scope. `reasoning_scope` is persisted only on reasoning-bearing Assistant messages and is always removed before the provider request.

The current Assistant turn is appended through `_assistant_continuation_dict`, which strips `usage` and `reasoning_scope` but preserves policy-permitted reasoning. Mid-Run rebuild after textual Compaction restores active-Run reasoning by message id so an in-flight Tool cycle remains valid; the persisted checkpoint Projection itself contains no `reasoning`, `reasoning_meta`, or `reasoning_scope`, so later Runs start a new reasoning context. Assistant `phase` remains because it is semantic history required by Responses Models, not opaque provider state.

When run-local Model fallback begins, Chat rebuilds request state with the fallback target's replay policy, exact reasoning scope, input modalities, wire media support, and route-gated Tool definitions, then strips prior Assistant reasoning/reasoning metadata before the first fallback request regardless of the primary policy. Any reasoning produced by the fallback is persisted with the fallback scope even though the Agent's configured primary Model remains unchanged, so a later primary Run cannot inherit it.

## Skills and prompt-cache stability

Pure-text `/skill-name` at the start and `$skill-name` anywhere are deterministic Skill activation hints; `$` never routes to a built-in command. The original user message stays unchanged. Only allowed, loadable, currently available Skills activate. An identical name+content activation deduplicates, but committing a changed package permits the same Skill name to activate again; carrier reconstruction is latest-wins by content. After Compaction, Chat reinjects a wholly missing Skill before the Summary as before; when the surviving Projection/tail contains an older version, it places the newest content after that shaped history so the stale carrier cannot regain precedence over the committed package.

`_pinned_skill_catalog` snapshots the rendered Skill catalog text in Session metadata on first build. Later requests reuse that exact text to keep the System Prompt stable for provider prompt caching. A newly available+allowed Skill therefore does not rewrite the catalog; `_announce_newly_available_skills` adds a one-time System Reminder note instead. The admitted working Project supplies Project Skills while an Identity Agent retains its private Skill layer; Config Agents remain under Project ceilings.

## Rooted Working Project Context

For a Rooted Identity Agent only, `_pinned_working_project_context` asks `SystemPromptManager.render_working_project_context` to render the selected Project's id, display name, cwd, and readable auto-load files before the first provider request, stores the exact text under `pinned_working_project_context` in Session metadata, and supplies that text verbatim to every later System Prompt build. A Project-file change or an explicit `project` Tool call cannot regenerate or replace it. This pins only the automatic Working Project block; Config/Project Agents retain the live `project_context` render, the explicit `project` Tool retains its ordinary persisted Tool result, and every unrelated System Prompt input keeps its existing lifecycle. Prompt preview renders a prospective snapshot without persisting it.

## Content Blocks, mentions, and media

`core/chat/content_blocks.py` owns `TextBlock`, `MediaBlock`, `FileBlock`, and `FileMentionBlock` plus JSON round-trip. `core/chat/file_mentions.py` lists cwd files for autocomplete and expands verified mentioned paths into immutable snapshots before Run admission. A `FileMentionBlock` records `path`, `status`, `text`, and `size_bytes`; request rendering always identifies it as a user-supplied file snapshot. Auto-injected Project files and expanded mentions are stamped into `runtime.file_read_state` so later write/edit Tools satisfy read-before-write without duplicating a read.

`ContentBlockResolver` performs provider-agnostic last-mile media routing by intersecting the Model's input modalities with `ProviderAdapter.wire_media_support(model_id)`. A current-turn supported image/audio/PDF becomes a native block followed by a path note. Unsupported, historical, or otherwise non-native media degrades to an explanatory path note; audio may first use Speech-to-Text. Text files carry the bounded rendering used by the `read` Tool. Only stored-blob I/O failure aborts the Run; capability mismatch degrades.

A Tool may return a `read_media` artifact. Tool dispatch persists only the small `MediaBlock` reference, then injects an already-resolved synthetic user message after every sibling Tool Result. This preserves the Tool-cycle order and avoids re-resolving inside the loop. A non-capable Model or absent resolver receives a path note instead of a failure. Full modality behavior belongs in `attachments.md`; Tool-side artifact production belongs in the relevant Tool reference.

## Tool-cycle invariant and repair

Every Assistant request entry with `tool_calls` must be followed by exactly one Tool Result for each `tool_call_id`, in Assistant-declared order, before any non-Tool message. Sibling calls may execute concurrently, but Session persistence and provider request history retain that declared order.

If loaded history is missing a Result because a Run was cancelled, crashed, or was interrupted before persistence, `_repair_dangling_tool_calls` creates request-only `result_unavailable` failure envelopes for the missing calls. It never mutates Session JSONL. Cancel during dispatch likewise cannot discard Results already computed: all sibling Result messages persist before the Run honors cancellation.

## Source and tests

- Canonical messages and shaping: `core/chat/messages.py`; tests under `tests/core/chat/test_messages_*.py`, `test_chat_loop_messages.py`, `test_chat_loop_requests.py`, and `test_chat_loop_reasoning_replay.py`.
- Skills and Tool definitions: `core/chat/tool_dispatch.py`, `core/chat/chat.py`; tests in `test_chat_loop_skills.py`, `test_tool_dispatch.py`, and `test_chat_prompt.py`.
- Content inputs: `core/chat/content_blocks.py`, `file_mentions.py`, `block_resolver.py`; matching `test_content_blocks.py`, `test_file_mentions.py`, and `test_block_resolver.py`.
