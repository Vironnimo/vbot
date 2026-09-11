# Live voice companion

Read for GPT-Live transport, voice app actions, or proactive spoken Agent updates. Shared task-client authentication/retry belongs to `providers.md`; Chat and Terminal behavior remain in their existing domains.

## Ownership and setup

- `core/model_tasks/live.py` owns the fixed GPT-Live WebRTC request, runtime instructions, and two app-operation Tool definitions. It uses `ProviderTaskClient` with the existing `openai:api-key` Connection and deliberately non-idempotent creation policy. No new dependency, persisted Agent, generic Task Model binding, or Chat Model catalog entry is required.
- `server/rpc/live_methods.py` exposes credential readiness and session creation through the canonical RPC registry. Readiness proves usable credentials, not account access to GPT-Live. Only the opaque session id and SDP answer cross back to the accessor; the key remains server-side.
- The voice Model is `gpt-live-1`; managed Responses delegation uses `gpt-5.6-terra` with the same key. This backend selects app Tools; vBot's coding Agents still do the coding.
- `LiveVoice.svelte` is mounted globally in `App.svelte`, outside normal tab/recovery remounts. `lib/liveVoice.js` owns media, protocol state, action execution, bounded transient captions, and update deduplication. App navigation uses existing autosave transitions; Terminal display uses the existing `TerminalsView.svelte` controller and layout.

## Protocol and effect boundaries

- The browser creates `oai-events` before its SDP offer, gathers ICE, exchanges SDP through `live.create`, and waits for `session.started`. It never sends `session.start` for a WebRTC session.
- Delegated `response.event` streams collect function calls from `response.output_item.done`, not `response.completed.response.output`. Only successfully completed Responses execute; every function output precedes the bare `response.create` continuation. Duplicate response/call ids never repeat mutations.
- Connection generations guard asynchronous work. Stop or replacement prevents further batch steps and suppresses late results; already accepted operations remain applied. Partial launches and failed subsequent navigation preserve confirmed Terminal ids. No failed or ambiguous mutation is automatically replayed.
- End disables microphone tracks immediately, sends `session.close`, and waits up to 15 seconds for `session.closed` and final usage. Disconnect/timeout releases local resources and reports uncertainty. Cancelling startup cannot revoke a session-creation request already accepted upstream; no automatic retry follows.
- UI operations use exact Agent/Session/Terminal ids. Chat sends validate the explicit Session and use `chat.stream` with `input_origin: speech_transcription`, preserving normal Queue admission. Terminal starts accept only Codex/Claude Code, one to four instances, and an explicit server directory. This is launch-scope validation, not foreground-program detection after the CLI exits back to its shell.
- Operator Terminal reads preserve Agent bindings. Guarded input rechecks the rendered screen revision under the manager's input lock. Text is bounded, raw control characters reject, and multiline input honors bracketed paste. Existing process lifetime and lifecycle owners never change because of voice navigation.
- Read results are bounded; terminal discovery omits launch history and caps the catalog at 40 entries with a truncation flag. Large chat content is reduced to a recent 8,000-character budget. The first version has no expanded voice pagination surface.

## Agent updates and trust

- Existing app Run lifecycle events trigger announcements only while listening. Previously seen/old completions and Runs explicitly excluded from Agent activity do not announce.
- `chat.run_result` in `server/rpc/chat_methods.py` loads the exact scoped Run through `ChatSession.load_run_result`; newer replies in the same Session cannot replace its question. It reads a bounded plain-content projection on the existing Chat worker pool, without marking the Session read.
- Attributed JSON message parts enter GPT-Live's quiet `session.thinking.append` channel; a compact `session.commentary.append` update requests spoken notice. Both are ephemeral provider session context, not canonical vBot Chat history or kernel reminders. Appends are acknowledged, but acknowledgments do not prove audible speech. Agent content is quoted data under the fixed instructions.
- Finished/failed/interrupted vBot Runs can announce; Agent questions are relayed for the user to answer, summaries only on request. Terminal quietness never proves a coding task finished, and this version does not proactively classify Terminal output.

## Verification and limits

- Offline wire/auth/retry/schema tests: `tests/core/model_tasks/test_live.py`, `tests/server/rpc/test_live_methods.py`. Exact persisted Run attribution: `tests/server/rpc/test_chat_methods.py`. Operator observation/input guards: `test_terminal_manager.py` and `test_terminal_methods.py` in their existing test trees.
- Frontend lifecycle/deduplication/partial effects/action routing: `webui/src/lib/__tests__/liveVoice.test.js`; rendered entry/error flow and actual maximize/restore: component tests for LiveVoice and TerminalsView.
- Browser microphone access needs HTTPS or localhost. Remote plain-HTTP Desktop microphone transport is not implemented; no native-audio fallback or browser security override exists. Desktop wakeword Voice must be off before Live voice starts; enabling it while Live is active stops Live.
- No GPT-Live credentials/access were available during implementation. Real speech, Model action selection, OpenAI account eligibility, and provider billing are unverified. Voice duration (including idle time) and delegated backend usage are separately billed by OpenAI. Details/captions are transient; this feature is not a durable voice transcript or billing ledger.
- Official contract sources checked during implementation: OpenAI API guides `live`, `voice-webrtc?api=live`, `live-delegation`, `live-conversations`, and `voice-server-controls?api=live` at `https://developers.openai.com/api/docs/guides/`.
