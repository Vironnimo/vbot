# Live voice

Read for Live voice calls: the `live_voice` Task Model, provider wires, delegated reasoning, the server call registry and owner socket, spoken app operations, or proactive Run announcements. Shared task-client authentication/retry belongs to `providers.md`; Chat and Terminal behavior remain in their existing domains.

## Ownership

The server owns everything except media. The provider control channel, delegated reasoning, Tool execution, and Run announcements run on the server; the accessor owns only microphone/speaker media (WebRTC directly to the provider), the UI requests the server sends it, and the displayed call state. Barge-in and turn-taking happen in the provider's voice model on the media path; vBot never pauses the conversation for app work.

- `core/model_tasks/live.py` is the public surface: `LiveVoiceService` (`status()`, `start_call`), the `LiveCallHost` / `LiveCall` / `LiveRuntime` protocols, `LiveRunNotice`, and `LiveStartRejected` with `LIVE_START_REJECTION_CODES` (`not_configured`, `invalid_offer`, `access_denied`, `rate_limited`, `outcome_unknown`, `provider_error`, `control_failed`). Runtime constructs it as `runtime.live_voice`.
- Internal core files: `_live_wire.py` (provider-neutral `LiveWire` protocol and normalized events), `_live_openai.py` (OpenAI wire), `_live_brain.py` (delegation Tool loop), `_live_call.py` (`LiveCallSession` lifecycle), `_live_tools.py` (voice and delegation instructions, the two app Tool definitions), `_live_options.py` (backend Model candidates for option schemas).
- A new provider adds a `LiveWire` implementation and a branch in `LiveVoiceService._resolve_target`; `LiveCallSession` and `LiveBrain` stay provider-neutral. Only provider `openai` is accepted today; any other binding rejects as `not_configured`.
- `server/live.py` (`LiveCallRegistry`, `app.state.live_calls`) owns call ownership, the owner socket `/ws/live/{call_id}`, UI requests, and the per-call Tool lock. `server/_live_tools.py` (`LiveToolExecutor`) runs app operations; `server/_live_feed.py` (`LiveRunFeed`) announces Runs. `server/rpc/live_methods.py` exposes `live.status`, `live.start`, `live.stop`, `live.ui_result`. The lifespan ends Live calls (bounded to 10 s) before the Runtime closes.
- WebUI: `lib/api/live.js` (RPC wrappers, owner socket, close-code outcomes), `lib/liveVoice.js` (controller: media, owner socket, UI requests, bounded captions), `components/LiveVoice.svelte` (mounted through AppShell's `sidebarFooter` in `App.svelte`, outside tab remounts). `App.svelte`'s `liveUiActions` implement UI requests through existing autosave navigation and the `TerminalsView.svelte` controller.

## Terms

### Live call

One realtime voice conversation: a provider media session plus its control channel, owned by the server. At most one per server; starting another replaces it.

### Voice model / backend model

The voice model (the binding target) talks with the user. The backend model (option `backend_model`) handles delegated requests by calling the app Tools. vBot's coding Agents still do coding work.

### Delegation

A request the voice model hands to vBot during the conversation. It returns one spoken-style answer to the voice model. Several run at once.

### Owner

The accessor that started the call. It attaches the owner socket and answers UI requests; only it holds media.

## Binding and options

- Task type `live_voice` is explicit-only (`MODEL_TASK_ORDER`, no modality derivation): voice Models declare it in `task_types`. Catalog entries live in `resources/models/openai.overrides.json`: `gpt-live-1` (`api-key` Connection) and `gpt-live-1-codex` (`subscription` Connection). They show in the Chat picker only under "Show all models", like other task-only Models.
- Options come from `task_options.live_voice.parameters`: `voice` (enum with a declared default) and `backend_model` (spec type `model`, default `gpt-5.6-terra`). `live_backend_candidates` offers tool-capable chat Models of the same Provider that the binding's Connection allows, so the Settings UI and save validation agree. `options_with_defaults` fills both before start; a stale or disallowed `backend_model` rejects start as `not_configured`.
- `live.status` returns `{configured, usable, target}`; `usable` proves credentials only, not account access to the voice Model. The accessor shows the control when `settings.model_tasks.live_voice.target` is set. Settings -> Voice -> "Live voice" edits the binding through the shared Task Model editor.

## Call lifecycle

- `live.start {sdp}`: the offer must start with `v=0`, contain `m=audio`, and be <= 64 KiB. The server resolves the binding, creates the provider call (non-idempotent task-client policy; never replayed), joins the control channel immediately (it does not replay earlier events), and returns `{call_id, media: {type: "webrtc", sdp}}`. Expected failures return `{error: <code>}` instead of an RPC error.
- The accessor gathers ICE non-trickle (5 s cap), attaches the owner socket, then applies the answer. Updates: `state` (`connecting` -> `live` on provider session start -> `closing` -> `closed` | `failed`), `caption` (`user` | `assistant`, `final`, text <= 1000 chars), `activity` (`busy`, label `working` while delegations run), exactly one `closed {reason, usage}`. The server may add a non-fatal `error` frame (`notification_failed`) and idle `heartbeat` frames (25 s).
- `live.stop` closes gracefully in the background: the session asks the provider to close and waits up to 15 s for a confirmed close with usage. Abort (owner lost, replacement, shutdown) sends a best-effort close within 1 s. A call that is not `live` within 45 s aborts with `start_timeout`.
- Closed reason priority: abort reason > confirmed provider reason > `closed` (after stop) > `connection_lost`. Phase `failed` only for `connection_lost` and `start_timeout`. A replaced call gets `closed` with reason `replaced`.
- Registry limits: owner must attach within 15 s and may reattach within 10 s after a socket loss, else the call ends; UI requests time out after 20 s; up to 200 updates buffer before attach; owner queue 1000. Close codes: 1000 after `closed`, 1008 unknown call, 1013 owner fell behind, 4000 replaced by a newer owner socket (do not reconnect).

## Delegation and trust

- `LiveBrain` runs a Tool loop over `runtime.get_adapter(ConnectionRef).send(...)` with the backend Model on the voice binding's Connection, `thinking_effort="low"`, at most 8 steps, and the last 10 request/answer pairs of the call as history. Each input carries the delegated request, the recent conversation (12 turns, <= 4000 chars), and the last 5 vBot updates.
- Up to 4 delegations run concurrently, each bounded to 240 s. Tool executions of one call are serialized by the registry. When the provider sends no request text, the session waits for user quiet (0.4 s, max 2 s) so the latest speech is included.
- Retryable Provider errors retry only the model send (0.5 s, 1.5 s), never a Tool execution. Unknown Tools and non-object arguments do not execute. Failures and timeouts answer with a spoken note naming mutating actions already performed as possibly completed; nothing is retried.
- Voice and delegation instructions (`VOICE_INSTRUCTIONS`, `DELEGATION_INSTRUCTIONS` in `_live_tools.py`) are runtime Agent-facing text. Chat messages, Terminal contents, and vBot updates are quoted data, not instructions. Provider appends are ephemeral provider context, never vBot Session history or kernel reminders; acknowledgments do not prove audible speech.

## App operations

- `vbot_app`: `context`, `open` (UI requests), `sessions` (`session.list`, 30), `read` (`chat.history`, 20 messages reduced to the recent 8,000 chars), `send` (`chat.stream` with `input_origin: speech_transcription`, normal Queue admission, text <= 16,000 chars). Exact Agent/Session ids; an unseen Session address is validated through `chat.history` first.
- `vbot_terminal`: list, start, read, input, show, maximize, restore, reorder, group create/show/rename/delete, close. Operations dispatch the registered RPC handlers in-process, so validation and `/ws` publication match other accessors.
- Terminal start accepts only Codex/Claude Code, a positive count (default one, no Live ceiling; TerminalManager capacity is authoritative), and an explicit server directory; without a `group_id` it reuses an editable group named after the program or creates one. A failed batch returns the requested count, confirmed Terminal summaries, and the error, never replaying accepted launches. This validates launch scope, not the foreground program after the CLI exits to its shell.
- Operator reads preserve Agent bindings; screens return the last 8,000 chars. Input rechecks the rendered screen revision (`expected_screen_revision`), rejects raw control characters, honors bracketed paste for multiline text, and supports named keys. `close` stops then forgets the exact Terminal like the UI close action; failures report confirmed or uncertain stop/removal state and never replay. A completed mutation stays reported when the following layout refresh fails (`layout_error`).
- UI requests (`context`, `open` for `chat` | `terminals`, `terminal_view` ops `context` / `refresh` / `show` / `maximize` / `restore` / `show_group`) go to the owner, which answers through `live.ui_result` with exactly one of `result` or `error`. After stop or replacement, multi-step operations stop before further effects (`voice_stopped`) and report confirmed partial results.

## Run announcements

- `LiveRunFeed` follows the server event bus from the call's start and announces completed/failed/interrupted Runs once (cancelled Runs stay silent). It skips Runs that finished before the call and Runs excluded from Agent activity. It loads each excerpt through `chat.run_result` for the exact scoped Run, so newer replies in the same Session never replace it (see `chat.md`). After a bus lag it resubscribes; Runs that left retention stay silent.
- The call dedups announcements, ignores them once closing, keeps the last 5 as brain context, and speaks them only while `live`. Text: `vBot update: {"run", "agent", "session_id", "result_excerpt" (<= 600 chars), "excerpt_truncated"}`. Finished Runs are announced briefly; Agent questions are relayed, summaries only on request. Terminal quietness never proves a coding task finished.

## OpenAI wire

- The Connection mode picks the dialect: `codex_responses` (ChatGPT subscription) -> codex, otherwise public API.
- Codex: `POST <codex base>/codex/realtime/calls?intent=quicksilver&architecture=avas` with `{sdp, session}` (model, instructions, voice, `delegation: {type: "client"}`); headers add `OpenAI-Alpha: quicksilver=v2`, `originator: vbot`, the Account id, and fresh session/thread ids. The SDP answer is the body; the call id comes from `Location`. Control: `wss://api.openai.com/v1/live/<call_id>` with the same headers. Delegations carry request text; results use `delegation.context.append` and announcements `session.context.append` (channel `speakable`, <= 500 bytes per append).
- Public: `POST /live/sessions` returns `{session: {id}, transport: {sdp}}`; the page's data channel is locked to no events. Control: `<base>/live/sessions/<id>/attach`. Delegations carry no text; results and announcements use `session.commentary.append` (<= 1500 bytes per append). Transcripts come from `session.input_transcript.delta` / `session.output_transcript.delta`.
- Call ids must match `^[A-Za-z0-9_-]{1,128}$`. Audio frames on the control channel are dropped unparsed. Logs carry the call id and a public target label without Account id; tokens, SDP, and transcripts never enter logs.

## Verification and limits

- Tests: `tests/core/model_tasks/test_live.py`, `test__live_openai.py`, `test__live_brain.py`, `test__live_tools.py`, `test__live_options.py`; `tests/server/test_live.py`, `test__live_tools.py`, `test__live_feed.py`, `tests/server/rpc/test_live_methods.py`; Run attribution in `tests/server/rpc/test_chat_methods.py`; Terminal guards in the Terminal manager/method tests. WebUI: `lib/__tests__/liveVoice.test.js`, `api.test.live.test.js`, `components/__tests__/LiveVoice.test.js`.
- The codex dialect's call creation, control join, and event shapes were probed against a ChatGPT subscription; the public dialect follows the official docs only. A full spoken call with delegation is not yet verified end to end.
- Browser microphone access needs HTTPS or localhost. Remote plain-HTTP Desktop microphone access, wakeword/hotkey start, and non-OpenAI providers are not implemented. Desktop wakeword Voice must be off before Live starts; enabling it while Live is active stops Live.
- Voice duration (including idle time) and backend usage bill separately at the provider. Captions are transient controller state, not a durable transcript or billing ledger.
- Official public-API sources: OpenAI guides `live`, `voice-webrtc?api=live`, `live-delegation`, `live-conversations`, and `voice-server-controls?api=live` at `https://developers.openai.com/api/docs/guides/`.
