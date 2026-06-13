# Plan: Reasoning-Replay Policy (per provider/model, adapter-coded)

**Goal:** Reasoning replay (`reasoning`/`reasoning_meta` on assistant history) is governed by an explicit per-provider/model policy declared in adapter code, Anthropic replays thinking blocks across runs as its API guidance requires, in-run replay behavior is consistent across all paths, and the partial-thinking interruption note is bounded instead of permanent.

**Context:**
- Today the replay decision is hardcoded once in the chat layer: `_message_to_request_dict` (core/chat/messages.py:427) strips `reasoning`/`reasoning_meta` from every assistant message on history rebuild, for every provider. Adapters never see prior-run reasoning, so no adapter can implement cross-run replay regardless of what its wire wants.
- Anthropic's current guidance (adaptive-thinking era, Opus 4.6+/Fable 5): pass thinking blocks back **unchanged across the whole conversation** when continuing on the same model — not just inside the tool loop. Stripping regular thinking blocks can trigger ordering/signature 400s. Cross-model, the server drops them silently (unbilled), so sending is safe and omitting is the risk. Stripping also changes the request prefix vs. what was sent during the run → provider-side prompt-cache misses across the whole prior conversation on every new run.
- Other wires want different things: OpenCode Go's gateway expects full `reasoning_content` round-tripping over history; DeepSeek-style wires reject replayed reasoning; the generic OpenAI-compatible default should stay conservative. Hence a real policy axis: `none` / `current_run` / `full_history`.
- Known inconsistencies to resolve along the way: (a) mid-run rebuilds (`_restore_active_tool_continuation`, compaction tail) keep reasoning only for the latest tool-continuation turn while the incremental in-run path keeps all in-run turns; (b) OpenCode Go's Anthropic route strips stricter than the chat layer; (c) the partial-thinking note (`_maybe_persist_partial_thinking`, core/chat/events.py:81) persists forever and is re-embedded into every future request until compaction.
- Verified: the adapter instance exists before history build (`_execute_run` creates it at core/chat/chat.py:380, history build at :424), so the chat layer can query the adapter's policy at request-build time. `reasoning`/`reasoning_meta` are fully persisted in session JSONL — no storage migration needed.

**User decisions (2026-06-12):**
- Policy is **adapter-coded only** — no catalog field, no provider JSON field, no agent setting. A catalog/override knob can be added later if a provider ever needs per-model exceptions that adapter code can't express.
- Partial-thinking system-reminder note: remove **if** replay makes it redundant. Finding: it does not — an interrupted streaming request never persists an assistant message, so the note is the only surviving trace of that turn's thinking. Therefore: keep, but bound it (Phase 4). Flagged as an open decision below.
- Sessions are the work unit: each phase below is sized to be completed in one session, sequentially. No parallel execution planned.

**Scope:**
- In: policy type + adapter hook, chat-layer policy threading with same-model gating, Anthropic `full_history`, per-provider policy rollout (OpenCode Go, Copilot, generic/Mistral/MiniMax review), in-run consistency fixes, partial-thinking note bounding, spec + glossary updates.
- Out: any settings/catalog/UI configuration surface for replay; compaction strategy changes; persisting partial assistant output of cancelled runs; provider catalog refresh changes.

**Assumptions & Constraints:**
- Default policy is `current_run` = exactly today's behavior, so unmigrated adapters behave byte-identically. No big-bang switch.
- Same-model gate compares the assistant message's persisted `model` (`<provider>/<model-id>`) with the agent's current model after stripping the optional `::<connection>[:<account>]` suffix (chat.md convention). Mismatch → strip, as today.
- The unconditional mid-run fallback strip (`_strip_assistant_reasoning_fields`) stays — switching providers mid-run must never replay foreign reasoning.
- No legacy compatibility: old sessions need no migration (reasoning fields are already persisted; old-format partial-thinking notes simply stay ordinary notes).

### Architecture decisions

- **Policy enum:** `ReasoningReplayPolicy` with values `none`, `current_run`, `full_history`. Lives in `core/providers/reasoning.py` (shared reasoning helpers module).
- **Adapter hook:** `ProviderAdapter.reasoning_replay_policy(model_id) -> ReasoningReplayPolicy`, default `current_run` on the ABC. `model_id` parameter is required because one adapter can route models to different wires (OpenCode Go).
- **Division of labor unchanged:** the chat layer owns history shaping — it queries the adapter's policy once per request build and strips/keeps accordingly. Adapters keep owning the wire channel (visible text vs. opaque blocks) for whatever reasoning survives shaping. Adapters must not re-implement history-wide strips once the policy expresses their intent (OpenCode Go's `_bound_assistant_reasoning_replay` gets retired/reduced in Phase 3).
- **Policy semantics:**
  - `none` — assistant entries never carry `reasoning`/`reasoning_meta`, not even the in-run continuation append.
  - `current_run` — today's behavior: history rebuild strips; in-run incremental appends keep; mid-run rebuilds restore the current run's turns (see consistency fix in Phase 1).
  - `full_history` — history rebuild keeps `reasoning`/`reasoning_meta` on assistant entries whose `model` passes the same-model gate; mismatched entries are stripped. Reasoning-only assistant turns that survive the gate stay in the request history instead of being skipped as empty.

### Milestones

| # | Milestone | Deliverable | Status |
|---|---|---|---|
| M1 | Policy infrastructure (Phase 1) | Hook + chat-layer threading live; all providers still on `current_run`; behavior byte-identical for them | ✅ Done (2026-06-13) |
| M2 | Anthropic cross-run replay (Phase 2) | Anthropic sends prior-run thinking blocks unchanged on same-model sessions | ✅ Done (2026-06-13) — live probe/verification deferred (no credentials) |
| M3 | Per-provider rollout (Phase 3) | Every adapter has a deliberate policy; OpenCode Go divergence resolved | ✅ Done (2026-06-13) — all four probes performed live, replay accepted everywhere probed |
| M4 | Bounded interruption note (Phase 4) | Partial-thinking note is truncated and one-shot | ✅ Done (2026-06-13) |
| M5 | Mistral + MiniMax → `full_history` (research-driven, was "candidate Phase 5") | Both providers replay reasoning per their own docs | ✅ Done (2026-06-13) — Mistral live-probe-verified; MiniMax probe deferred (no creds) |

---

## Phase 1 (Session 1): Policy hook + chat-layer integration — ✅ DONE (2026-06-13)

**Goal of this phase:** The policy axis exists end-to-end with `current_run` as universal default; chat-layer history shaping is policy-driven and internally consistent; no provider behavior changes yet.

**Completion notes (2026-06-13):**
- All done-when criteria verified: full quality gates green (`python scripts/quality.py core/providers/ core/chat/`, 1061/1061 tests), existing tests passed unchanged, new unit + loop tests added for `full_history` gate, `none` continuation strip, and the mid-run rebuild regression.
- `ReasoningReplayPolicy` implemented as a `Literal` + constants (`REASONING_REPLAY_*`, `REASONING_REPLAY_POLICIES`) in `core/providers/reasoning.py`, exported from `core.providers`; hook `ProviderAdapter.reasoning_replay_policy(model_id)` defaults to `current_run` on the ABC.
- Chat layer resolves the policy tolerantly via `_resolve_reasoning_replay_policy` (`hasattr` probe like `set_debug_context`, default `current_run`) — keeps test doubles and the fallback-adapter path working; `_send_until_final` resolves from its own adapter, so the fallback path uses the fallback adapter's policy.
- Same-model gate reuses `parse_bare_model` (suffix-strip convention) in `_replays_assistant_reasoning` (core/chat/messages.py).
- `_restore_active_tool_continuation` was renamed/generalized to `_restore_in_run_assistant_reasoning`: restores `reasoning`/`reasoning_meta` for every live-list assistant entry matched by message id (copy of the two fields instead of whole-dict replacement — equivalent, since live and rebuilt dicts only differ in those fields).
- Specs updated: chat.md (Conventions replay-policy + gate + rebuild fix, auto-compaction bullet, provider-switch bullet, Token Usage `_message_to_request_dict` line), providers.md (Interfaces hook bullet, Conventions no-duplicate-strips bullet).

- Add `ReasoningReplayPolicy` (Literal or StrEnum, follow existing style in the module) and export it; add `ProviderAdapter.reasoning_replay_policy(model_id)` returning `current_run` by default — read: [.vorch/specs/providers.md], files: [core/providers/reasoning.py, core/providers/adapter.py, tests/core/providers/test_reasoning.py, tests/core/providers/test_adapter.py]
- Thread the policy through request building: `_execute_run`/`send()` resolve the policy from the live adapter and pass it into `_build_request_messages` → `_embed_notes_into_request` → `_assemble_request_history` → `_message_to_request_dict`. Implement the three semantics incl. the same-model gate for `full_history` (compare message `model` against agent model with `::suffix` stripped — reuse/extract the existing suffix-strip convention). `_assistant_continuation_dict` honors `none`. — read: [.vorch/specs/chat.md], files: [core/chat/chat.py, core/chat/messages.py, tests/core/chat/test_chat_messages.py, tests/core/chat/test_chat_loop.py]
- Make `_is_empty_assistant_history_message` policy-aware: under `full_history`, a reasoning-only assistant turn that passes the same-model gate is not "empty" and must stay in the request history (Anthropic thinking-only turns carry signed blocks). Under `current_run`/`none`, or when the gate fails, behavior stays as today (skip). — files: [core/chat/messages.py, tests/core/chat/test_chat_messages.py]
- Fix the in-run rebuild inconsistency: generalize `_restore_active_tool_continuation` so mid-run rebuilds (notes drained, auto-compaction) restore `reasoning`/`reasoning_meta` for **all** assistant entries of the live request list (match by message id), not just the latest tool-continuation turn. This makes `current_run` mean the same thing on the incremental and the rebuild path. Compaction-tail rebuilds flow through the same policy-aware helpers. — read: [.vorch/specs/compaction.md], files: [core/chat/messages.py, core/chat/chat.py, tests/core/chat/test_chat_messages.py, tests/core/chat/test_chat_loop.py]
- Update specs: chat.md Conventions ("not resent on later turns by default" → policy language; document the gate and the rebuild fix; Token Usage section line about `_message_to_request_dict`), providers.md (new adapter hook in Interfaces + convention that adapters must not duplicate history-wide strips). — files: [.vorch/specs/chat.md, .vorch/specs/providers.md]

**Dependencies:** none.
**Done when:**
- A stub adapter declaring `full_history` gets rebuilt history containing `reasoning`/`reasoning_meta` on same-model assistant entries and stripped fields on model-mismatched entries (unit test).
- A stub adapter declaring `none` never receives reasoning fields, including on the live continuation turn (unit test).
- All existing chat/provider tests pass unchanged (proves `current_run` default is byte-identical).
- A mid-run rebuild (notes/compaction path) preserves reasoning for every current-run assistant turn under `current_run` (regression test for the old latest-turn-only behavior).
- `python scripts/quality.py core/providers/ core/chat/` green.

---

## Phase 2 (Session 2): Anthropic → `full_history` — ✅ DONE (2026-06-13)

**Goal of this phase:** Anthropic sessions resumed on the same model replay prior-run thinking blocks unchanged, per current Anthropic guidance.

**Completion notes (2026-06-13):**
- `AnthropicAdapter.reasoning_replay_policy` → `full_history` (core/providers/anthropic.py); `_to_anthropic_assistant_content` needed no rendering changes — replay correctness is pinned by tests (byte-identical thinking/redacted_thinking incl. signatures on a two-run same-model history).
- Thinking-disabled guard implemented in `_build_payload`: replayed `reasoning_meta` thinking blocks are stripped only when the outgoing request **explicitly** disables thinking (`thinking: {type: disabled}`) or the catalog marks the model reasoning-unsupported; an absent thinking parameter keeps blocks (omitting is the risk per Anthropic guidance, and OpenCode Go's Anthropic route relies on continuation blocks without an explicit thinking kwarg). An assistant turn left without content blocks after stripping is dropped from the request — the wire rejects empty content arrays.
- **Live probe and live verification NOT performed** — no Anthropic credentials in this environment (empty `ANTHROPIC_API_KEY=` in `~/.vbot/.env`, no env var, no OAuth token). Deferred with full instructions in `.vorch/FLAGGED.md` (2026-06-13 entry); the conservative guard stands until probed. Plan's "live probe performed" done-when is **waived for now**, all other done-when criteria met.
- Scope guard verified: `OpenCodeGoAdapter` holds the `AnthropicAdapter` by composition and inherits the ABC's `current_run` — the override does not leak into OpenCode Go (its policy is Phase 3).
- Chat-level integration coverage added (tests/core/chat/test_chat_integration.py): cross-run replay via `full_history` fake adapter, model-switch gate strips, compaction-checkpoint tail rebuild carries tail-turn reasoning.
- Specs updated: providers/anthropic.md (Reasoning: replay policy + guard + probe-pending note; Constraints rewritten), GLOSSARY.md CoT entry (replay-policy scope instead of tool-loop-only).
- Quality gates green: `python scripts/quality.py core/providers/ core/chat/` (1070/1070).

- Override `reasoning_replay_policy` in `AnthropicAdapter` → `full_history`. Confirm `_to_anthropic_assistant_content` correctly renders replayed `reasoning_meta.content_blocks` (thinking/redacted_thinking incl. signatures, byte-unchanged) for historical entries — it is already generic, so this is mostly tests. — read: [.vorch/specs/providers/anthropic.md], files: [core/providers/anthropic.py, tests/core/providers/test_anthropic.py]
- Add a thinking-disabled guard in the adapter: when the outgoing request disables thinking (`thinking_effort: none` → `thinking: {type: disabled}`, or reasoning-unsupported model), historical thinking blocks must not be sent (strip `reasoning_meta` blocks at payload build). Verify the real API behavior with a live probe first (providers.md convention: probe before designing) — if the API tolerates them, the guard can be lighter, but document what was observed. — files: [core/providers/anthropic.py, tests/core/providers/test_anthropic.py]
- Chat-level integration coverage: cross-run replay through the loop with a fake Anthropic-policy adapter — new run on same model carries prior-run thinking; switching the agent model between runs strips it (gate); compaction-checkpoint tail rebuild carries thinking for tail turns. — files: [tests/core/chat/test_chat_integration.py]
- Live verification: one real multi-run Anthropic session (run with tool calls → run completes → new run in same session) confirming no 4xx and thinking blocks accepted; spot-check that a model switch mid-session still works. Document findings in the spec.
- Update specs: providers/anthropic.md (Reasoning + Constraints sections — replace "stale completed-turn metadata must not be sent on later turns" with the policy + gate semantics), GLOSSARY.md CoT entry (currently scoped to tool-use loops). — files: [.vorch/specs/providers/anthropic.md, .vorch/GLOSSARY.md]

**Dependencies:** Phase 1.
**Done when:**
- Adapter unit test: request payload for a two-run same-model history contains the first run's thinking blocks byte-identical to what was persisted.
- Adapter unit test: thinking-disabled request contains no thinking blocks despite history carrying them.
- Live probe performed and outcome noted in providers/anthropic.md.
- `python scripts/quality.py core/providers/anthropic.py tests/core/chat/test_chat_integration.py` green.

---

## Phase 3 (Session 3): Per-provider rollout — OpenCode Go, Copilot, remaining adapters — ✅ DONE (2026-06-13)

**Goal of this phase:** Every adapter declares a deliberate policy; OpenCode Go's divergent in-adapter strip is retired; Copilot/Responses `encrypted_content` replay is evaluated against real behavior.

**Completion notes (2026-06-13):**
- **Live probes performed against the real endpoints** (credentials were available in this environment: `OPENCODE_GO_API_KEY` + Copilot OAuth with working token re-exchange). All four cross-run replay probes (history with a *completed* prior assistant turn carrying reasoning, then a new user turn) returned 200:
  - OpenCode Go OpenAI route (`deepseek-v4-flash`): `reasoning_content` replay accepted.
  - OpenCode Go Anthropic route (`minimax-m2.5`): signed `thinking` block replay accepted.
  - Copilot `/responses` (`gpt-5-mini`): reasoning-item replay incl. `encrypted_content` accepted.
  - Copilot `/v1/messages` (`claude-sonnet-4.6`): signed `thinking` block replay accepted. (`claude-haiku-4.5` returned no thinking blocks at all under `thinking: enabled` — probe repeated with sonnet.)
- **OpenCode Go** → `full_history` for both routes; `_bound_assistant_reasoning_replay` and its helper functions deleted (send/stream/_build_payload now pass messages through; the inner `AnthropicAdapter`'s thinking-disabled guard still applies on the Anthropic route). Tests rewritten: bounded-replay pins replaced by replay-for-all-assistants pins + policy test over both routes.
- **GitHub Copilot** → policy follows the endpoint family: `full_history` for `/responses` and `/v1/messages` (probe-verified), `current_run` for the `/chat/completions` fallback (unverified; conservative-omission convention). Pinned by tests via metadata-driven endpoint selection.
- **Generic OpenAI-compatible, Mistral, MiniMax, OpenRouter** → deliberately stay on the inherited `current_run` default; one pinning test per adapter so the choice is explicit. OpenRouter documented with the billing rationale (replayed `reasoning_details` text is billed; mixed upstreams).
- Specs updated: opencode-go.md (Reasoning Replay section rewritten, probe outcomes recorded), github-copilot.md (Runtime Policy replay bullet + probe outcomes), mistral.md / minimax.md / openrouter.md (policy note each).
- Quality gates green: `python scripts/quality.py core/providers/` (730/730) and `core/chat/` (351/351).

- OpenCode Go: declare policy per route in `reasoning_replay_policy(model_id)` — the gateway expects full `reasoning_content` round-tripping on the OpenAI route and the Anthropic-routed models should mirror `AnthropicAdapter` (`full_history` for both, pending probe). Retire `_bound_assistant_reasoning_replay` or reduce it to whatever wire-specific filtering the policy cannot express; the chat layer now owns history-wide shaping. Verify against the real gateway with a probe (multi-turn with reasoning, then a follow-up run). — read: [.vorch/specs/providers/opencode-go.md], files: [core/providers/opencode_go.py, tests/core/providers/test_opencode_go.py, .vorch/specs/providers/opencode-go.md]
- GitHub Copilot (Responses wire): evaluate whether `encrypted_content` reasoning items should round-trip across runs (OpenAI Responses guidance suggests yes); probe the Copilot endpoint. Set `full_history` only if verified; otherwise keep `current_run` and document why. Same review for the Copilot Messages (Anthropic-style) wire, which can likely mirror the Anthropic decision. — read: [.vorch/specs/providers/github-copilot.md], files: [core/providers/github_copilot.py, core/providers/github_copilot_responses.py, core/providers/github_copilot_messages.py, tests/core/providers/test_github_copilot_responses.py, tests/core/providers/test_github_copilot_messages.py, .vorch/specs/providers/github-copilot.md]
- Generic `OpenAICompatibleAdapter`, Mistral, MiniMax, OpenRouter: review and explicitly settle on `current_run` (inherit default) unless provider docs/probes say otherwise; if any wire is known to reject replayed reasoning fields, set `none` there. Add a short policy note to each provider spec touched. — files: [core/providers/openai_compatible.py, core/providers/mistral.py, core/providers/minimax.py, core/providers/openrouter.py, .vorch/specs/providers/mistral.md, .vorch/specs/providers/minimax.md, .vorch/specs/providers/openrouter.md]

**Dependencies:** Phases 1–2 (the Anthropic-route decision feeds OpenCode Go/Copilot Messages).
**Done when:**
- `_bound_assistant_reasoning_replay` no longer duplicates chat-layer shaping (deleted or demonstrably wire-specific only).
- Each adapter either overrides the hook or a test asserts its effective policy, so the choice is explicit and pinned.
- Probes for OpenCode Go and Copilot performed; outcomes recorded in the provider specs.
- `python scripts/quality.py core/providers/` green.

---

## Phase 4 (Session 4): Bound the partial-thinking interruption note — ✅ DONE (2026-06-13)

**Goal of this phase:** The "Partial thinking before interruption" note stops being a permanent fixture of every future request and gets a size cap; the mechanism itself stays (replay cannot cover interrupted turns — nothing is persisted for them).

**Completion notes (2026-06-13):**
- Note is now prefix-tagged `[partial-thinking] ` (`PARTIAL_THINKING_NOTE_PREFIX` + `is_partial_thinking_note` in `core/sessions/sessions.py`, exported from `core.sessions`, mirroring `[skill-context]`).
- `_assemble_request_history` embeds it one-shot: a partial-thinking note is skipped when any assistant message follows it in the session (`index < _last_assistant_index(messages)`); otherwise it embeds as `<system-reminder>` with the prefix stripped by `_system_reminder_block`. It stays in JSONL either way (debugging), so no migration; pre-prefix old notes keep ordinary behavior.
- `_maybe_persist_partial_thinking`/`_partial_thinking_note_content` (core/chat/events.py) cap the persisted reasoning at `PARTIAL_THINKING_CAP = 2000` chars (head kept, `[… partial thinking truncated]` marker).
- Tests: one-shot embed/skip + prefix-strip in test_chat_messages.py; cap + routing in a new test_events.py; the three existing test_chat_loop.py assertions updated for the prefix. `python scripts/quality.py core/chat/` (359/359) and `core/sessions/` (55/55) green.
- chat.md notes-conventions updated with the prefix + cap + one-shot rule.

- Convert the note to a prefix-tagged kernel note (follow the existing `[skill-context]` / `[channel-message]` prefix pattern, e.g. `[partial-thinking] `) and add read-side one-shot semantics in `_assemble_request_history`: embed it as `<system-reminder>` only while no assistant message exists after it in the session; once the next run produced an assistant turn, skip it (it stays in JSONL for debugging, like other internal notes). Old plain-text notes in existing sessions keep today's behavior — acceptable, no migration. — read: [.vorch/specs/chat.md], files: [core/chat/events.py, core/chat/messages.py, tests/core/chat/test_chat_messages.py, tests/core/chat/test_streaming.py]
- Truncate the persisted partial reasoning to a fixed cap (suggest 2,000 chars, keep the head, note the truncation in the text) so a long thinking stream can't bloat the next request. — files: [core/chat/events.py, tests/core/chat/test_chat_loop.py]
- Update chat.md (notes conventions: new prefix + one-shot embedding rule). — files: [.vorch/specs/chat.md]

**Dependencies:** Phase 1 (touches the same `_assemble_request_history` code); independent of Phases 2–3.
**Done when:**
- Test: cancelled streaming run → note embedded in the next request; after that request persists an assistant message, a further rebuild no longer embeds it.
- Test: partial reasoning longer than the cap is truncated in the persisted note.
- `python scripts/quality.py core/chat/` green.

---

## Research findings (2026-06-13): cross-provider replay guidance, per-model variance, interrupted runs

Web research pass over official provider docs (state 2026-06), requested to firm up the Phase-3 `current_run` defaults for OpenRouter/Mistral/MiniMax and the interrupted-run question. Each subsection ends with the consequence for our policy. Headline: **Mistral and MiniMax both explicitly require cross-turn reasoning replay in their own docs** — the Phase-3 "no probe evidence" rationale for their `current_run` default is superseded; OpenRouter's `current_run` stands.

### OpenRouter

- Official guidance ([Reasoning Tokens docs](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)): request-side replay uses two assistant-message fields — `reasoning` (plaintext string; `reasoning_content` is an accepted alias) and `reasoning_details` (structured array). `reasoning_details` entries have three types: `reasoning.text` (raw text + optional `signature`), `reasoning.summary`, `reasoning.encrypted`. Hard rule: "the entire sequence of consecutive reasoning blocks must match the outputs generated by the model during the original request; you cannot rearrange or modify the sequence of these blocks."
- Scope per docs: preservation is framed as "useful specifically for tool calling" — i.e. in-run. Cross-run replay is neither required nor discouraged; simply undocumented.
- Per-upstream variance is real; the docs name the families **requiring** preservation: all OpenAI reasoning models (o1/o3/GPT-5+), all Anthropic reasoning models (Claude 3.7+), all Gemini reasoning models, all xAI reasoning models; open-source: Qwen3.5+, MiniMax M2+, Kimi K2 Thinking+, GLM 4.5+, Nemotron 3 Nano+, INTELLECT-3, MiMo-V2-Flash+, Trinity Large Thinking+.
- Gemini upstreams hard-fail without in-loop replay: 400 "Function call is missing a thought_signature in functionCall parts. This is required for tools to work correctly." ([gptel #1190](https://github.com/karthink/gptel/issues/1190)). OpenRouter surfaces Gemini thought signatures inside `reasoning_details` (encrypted type); they must be echoed back unchanged.
- Our `current_run` already satisfies every documented hard requirement: the in-run path keeps `reasoning_meta`, and the generic request builder round-trips `reasoning_meta.reasoning_details` onto outgoing assistant messages (`_apply_openai_reasoning_meta`, core/providers/openai_compatible.py:643, verified 2026-06-13). Gap: no pinning test asserts that an in-run tool loop round-trips `reasoning_details` through the OpenRouter adapter specifically — worth adding.
- Undocumented (probe before any change): what OpenRouter does when `reasoning_details` arrives for a non-supporting upstream (strip vs. forward vs. error), and whether replayed details bill as input tokens (docs only state generation bills as output). The billing rationale currently in openrouter.md is our inference, not documented fact.
- **Consequence:** stay `current_run`. If cross-run replay for specific upstream families (anthropic/*, google/*) ever looks attractive, the hook's `model_id` parameter supports a per-family split — but only with probes per family; the same-model gate already prevents cross-model replay.

### Mistral

- Official guidance is unambiguous and **cross-turn**, not tool-loop-only ([Reasoning docs](https://docs.mistral.ai/capabilities/reasoning/)): "always replay the full assistant message (including `ThinkChunk`) back into the message history. Dropping the reasoning trace across turns degrades model performance." Explicit warning: "Do not strip `ThinkChunk` from assistant messages before replaying them. … Stripping them increases token efficiency but significantly degrades output quality."
- Model landscape shifted (docs state 2026-06): `magistral-small-latest`/`magistral-medium-latest` are **deprecated**. Reasoning is now `reasoning_effort` (`"high"` | `"none"`) on `mistral-small-latest` and `mistral-medium-3-5` ("for agentic and code use cases, `reasoning_effort="high"` is recommended"). With `high`, `message.content` is a chunk list (`ThinkChunk` containing `TextChunk`s, then `TextChunk`); with `none`, a plain string. mistral.md's `prompt_mode`/magistral notes describe the deprecated generation; the next catalog refresh will surface the new models.
- Implementation gap if we flip: our normalizer flattens thinking chunks into visible `reasoning` and persists **no** `reasoning_meta` (core/providers/mistral.py:135–144 — Mistral messages carry neither `encrypted_content` nor `reasoning_details`, so `_extract_openai_reasoning_meta` returns None). The generic request path replays only `reasoning_meta`, so flipping Mistral to `full_history` today would change nothing on the wire. Adapter work needed: persist the raw thinking chunks in `reasoning_meta` (mirror Anthropic's `content_blocks` pattern) and render assistant `content` as a chunk list on replay.
- Open for the probe: tool use combined with reasoning replay (docs don't cover it); whether replayed ThinkChunks on a `reasoning_effort: "none"` request error or are tolerated.
- **Consequence:** Mistral wants `full_history` per its own docs — but it is not a one-line flip; it needs reasoning_meta capture + chunk-list rendering plus a probe. Proposed as follow-up work below.

### MiniMax

- Strongest cross-turn requirement of all researched providers ([interleaved-thinking post](https://www.minimax.io/news/why-is-interleaved-thinking-important-for-m2)): "preserving the reasoning process across multi-turn interactions is essential"; dropping prior reasoning costs measured quality — SWE-Bench Verified 69.4→67.2, Tau² 87→64, BrowseComp 44.0→31.4, GAIA 75.7→67.9 (retained vs. discarded).
- API mechanics ([M3 tool-use docs](https://platform.minimax.io/docs/guides/text-m3-function-call)): with `reasoning_split: true` (extra_body), thinking arrives separately as `reasoning_details`, and "the entire response_message — including the reasoning_details field — must be preserved in the message history". Without the split, thinking sits inline as `<think>…</think>` in `content` — "do not modify the content field". The Anthropic-compatible endpoint mirrors Anthropic (append complete content incl. thinking blocks). Guidance applies to M2+, M2.5, and M3 alike (OpenRouter's preservation list also names "MiniMax M2+").
- Corroborating evidence we already hold: the Phase-3 OpenCode Go Anthropic-route probe (minimax-m2.5) accepted full cross-run thinking replay — consistent with MiniMax wanting replay generally.
- Implementation note if we flip: the generic machinery already replays `reasoning_meta.reasoning_details`, but vBot only *captures* reasoning_details when `reasoning_split` is sent — and today the adapter never sets it (caller-controlled only, core/providers/minimax.py). Decision needed: default `reasoning_split: true` for M2.x/M3 so reasoning is captured separately and becomes replayable (also keeps `<think>` text out of visible content). Then flip to `full_history` + probe.
- **Consequence:** MiniMax should move to `full_history`; blocked on the `reasoning_split` default decision + a probe. Proposed as follow-up work below.

### DeepSeek (calibration point for generic OpenAI-compatible wires)

- Official ([reasoning model docs](https://api-docs.deepseek.com/guides/reasoning_model)): "If the `reasoning_content` field is included in the sequence of input messages, the API will return a 400 error." Previous-round CoT is never concatenated into context; deepseek-reasoner currently lists function calling as unsupported.
- Our generic adapter is **safe by construction** for DeepSeek-style wires: visible `reasoning` (extracted from `reasoning_content`) is never sent back — only `reasoning_meta` keys (`encrypted_content`, `reasoning_details`) are replayed, which DeepSeek doesn't emit. `current_run` cannot trigger the 400.
- The Phase-3 OpenCode Go probe (deepseek-v4-flash accepting `reasoning_content` replay) is **gateway** behavior, not DeepSeek-native behavior — the gateway evidently handles it upstream. Don't generalize from it.
- **Consequence:** generic adapter stays `current_run`; no `none` policy needed anywhere today, because the field DeepSeek rejects is never replayed by the generic path in the first place.

### OpenAI Responses ordering rule (Copilot `/responses`, OpenAI subscription connection)

- Hard rule worth pinning: reasoning items must be immediately followed by their paired item (function_call/message). A dangling reasoning item → 400 "Item 'rs_…' of type 'reasoning' was provided without its required following item" ([OpenAI community](https://community.openai.com/t/how-to-solve-badrequesterror-400-item-rs-of-type-reasoning-was-provided-without-its-required-following-item-error-in-responses-api/1151686)). Relevant for replay ordering when rebuilding history (keep reasoning items adjacent to their followers) and for interrupted runs (below).

### Interrupted runs: what to do with partial thinking

Research conclusion: **no provider supports wire-level replay of partial/interrupted thinking; vBot's note-based design is correct and Phase 4 stands as planned.**

- Anthropic: the `signature` arrives as `signature_delta` at the **end** of a thinking block — an aborted stream leaves an unsigned partial block. API rule: replayed thinking "must match the outputs generated by the model during the original request"; modified/partial blocks → 400 ([extended thinking docs](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)). Partial thinking is categorically unreplayable. (Same docs, replay economics: replayed prior thinking bills as input/cache-read tokens; on Opus 4.5+/Sonnet 4.6+ prior-turn thinking blocks are kept in context by default, on older models they're server-stripped — consistent with Phase 2's `full_history` choice and the cache-prefix argument.)
- OpenAI Responses: `encrypted_content` exists only on completed reasoning items; an aborted stream yields no replayable item, and a dangling reasoning item 400s (ordering rule above). Unreplayable.
- Text-based wires (Mistral ThinkChunk, MiniMax reasoning_details, OpenRouter reasoning.text): no cryptographic validation, so a truncated trace *would* be accepted — but all three providers' guidance says to preserve the **complete** original output, and a truncated trace misrepresents the model's reasoning state. Not worth pursuing.
- **Consequence for Phase 4:** confirmed as designed — interrupted runs persist no assistant message; partial thinking survives only as the kernel note embedded as `<system-reminder>` (never as fake assistant reasoning). Cap + one-shot is consistent with every provider's guidance. No wire-replay path for partial thinking should ever be added, on any provider.

### Follow-up work (was "candidate Phase 5") — ✅ DONE (2026-06-13)

All three items were implemented after the user prioritized provider correctness. In payoff order:
1. **MiniMax → `full_history`:** ✅ `_build_payload` defaults `reasoning_split: true` for reasoning-active models (M2.x always; M3 unless thinking disabled); `reasoning_details` is captured by the generic `_extract_openai_reasoning_meta` and replayed by `_apply_openai_reasoning_meta`; policy flipped to `full_history`. **Probe deferred (no MiniMax creds)** — recorded in `.vorch/FLAGGED.md`; behavior pinned by unit tests. Commit `feat(providers): replay minimax reasoning across runs via reasoning_split`.
2. **Mistral → `full_history`:** ✅ Live-probe-verified (`mistral-small-latest`, 2026-06-13): the wire returns reasoning as `content` chunk lists with **nested** `thinking: [TextChunk]` (the old code read it as a string and silently dropped reasoning — fixed via `_flatten_thinking`); a reconstructed `[ThinkChunk, TextChunk]` replay is accepted, **including with `reasoning_effort: "none"`** → no thinking-disabled guard needed. `_format_assistant_message` reconstructs the ThinkChunk from the persisted visible `reasoning` text (no `reasoning_meta` capture needed — simpler than originally planned), policy flipped, mistral.md refreshed incl. magistral deprecation. Commit `feat(providers): replay mistral reasoning across runs as think chunks`.
3. **OpenRouter (stays `current_run`):** ✅ Added the in-run `reasoning_details` round-trip pinning test (Gemini upstreams hard-require it); corrected openrouter.md billing claim to "inferred, not documented" and reframed `current_run` as the genuinely-correct target (cross-run undocumented). Commit `test(providers): pin openrouter in-run reasoning_details round-trip`.

---

### Open decisions

1. ~~**Partial-thinking note: bound (planned) vs. remove entirely.**~~ **Resolved 2026-06-13:** kept + bounded (Phase 4 done). Replay does not make it redundant (interrupted turns persist no assistant message), so the note stays — now prefix-tagged, size-capped, and embedded one-shot.
2. ~~**Copilot Responses / OpenCode Go target policies** are probe-dependent (Phase 3). Default if probes are inconclusive: stay on `current_run` and record why.~~ **Resolved 2026-06-13:** probes performed; OpenCode Go both routes and Copilot Responses/Messages → `full_history`, Copilot chat-completions fallback → `current_run` (unprobed, conservative).
3. ~~**Adopt the candidate Phase 5 (Mistral/MiniMax → `full_history`)?**~~ **Resolved 2026-06-13:** yes — both flipped to `full_history` (see Follow-up work above). User approved prioritizing provider correctness. `reasoning_split: true` defaulted for MiniMax M2.x/M3. Only outstanding item: the MiniMax live probe, deferred for lack of credentials (FLAGGED).

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Anthropic 4xx on replayed edge-case histories (compaction boundaries, repaired dangling tool calls, signature/ordering rules) | Med | Med | Same-model gate; thinking-disabled guard; live probe in Phase 2 before relying on it; revert is a one-line policy change per adapter |
| Token-cost growth from replayed reasoning on wires that bill it (e.g. `reasoning_content` text round-trip) | Med | Low/Med | Policy is a deliberate per-provider choice; Anthropic same-model prior-turn thinking is handled/discounted server-side; compaction still bounds total history |
| Subtle behavior drift for providers meant to stay on `current_run` | Low | Med | Phase 1 done-when requires the existing test suite to pass unchanged; default lives on the ABC, not per call site |
| Mid-run rebuild generalization regresses note/tool adjacency invariants | Low | High | Existing tool-cycle invariant tests in test_chat_messages.py; new regression test added in Phase 1 |
| OpenCode Go gateway behavior differs from its spec (round-trip expectation) | Med | Low | Probe before switching policy; spec updated with observed behavior |
