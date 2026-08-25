# Module Audit

Corpus-wide deep-module audit (diagnose-only pass, then living remediation checklist). Run against the whole repo following `.vorch/workflows/deep-module-audit-workflow.md`: three explorer passes (oversized/shallow modules, duplicate logic, parameter clusters) plus orchestrator confirmation of every major claim against source. Severity: 🔴 actively hurting, fix soon · 🟡 real, schedule it · 🔵 minor/opportunistic.

## Maintenance rule

**When you work from this file, you keep this file updated in the same change.** Tick the checkbox of every item you finish, add a one-line outcome note (with commit reference where useful), and re-scope or re-prioritize items whose reality diverged from the original finding. An unticked box whose work is already done is a defect in this file, not a convenience. New findings discovered during remediation get added under their section; rejected findings get struck through with a one-line reason instead of being deleted.

## Findings

### A. Split candidates — complexity that deserves its own owner

- [ ] **A1 🔴 code — Split `core/chat/chat.py` (4297 lines, 4× soft limit).** `ChatLoop` bundles four responsibilities beyond its legitimate core (run lifecycle, queueing, request-state building):
  - [ ] **A1a Compaction orchestration (~1000 lines)** — `_execute_manual_compaction`, `_maybe_auto_compact_state`, `_prepare/_commit_prompt_context_after_compaction`, `_load_compaction_settings`, `_emit_compaction_completed`, `_project_post_compaction_request`, `_load_compaction_snapshot`. `core/compaction` exists as the owning domain module but only holds policy/triggers/strategies; the trigger/execution coordination lives in the wrong house. Extract a compaction-run coordinator.
  - [ ] **A1b Pinned prompt-context assembly (~340 lines, ~2358–2698)** — `_pinned_skill_catalog/_epoch_text/_working_project_context/_soul_context/_memory_files`; belongs near `core/prompts`.
  - [ ] **A1c Wire-request/stream runner (~420 lines, ~4138–4560)** — `_send_assistant_request` → `_consume_stream_attempt`, `_finalize_interrupted_partial`, chunk-timeout resolution; extract a chat-internal request runner.
  - Note: `_send_until_final` (~460 lines) IS the agentic loop and stays — legitimately cohesive.
- [ ] **A2 🔴 code — Split `core/runtime/runtime.py` (2751 lines).** God-object hub: DI container + lifecycle are legitimate; embedded business logic of three domains is not:
  - [ ] **A2a Skill resolution/scanning/sharing (~500 lines, ~1249–1760)** → service owned by `core/skills`, composed by Runtime.
  - [ ] **A2b Recall backend construction/reload/session-eviction (~1766–2062)** → owner `core/recall`.
  - [ ] **A2c Provider adapter factory + connection token getters + local catalog refresh (~2576+)** → integration service in `core/providers`.
- [ ] **A3 🟡 code — Split `core/chat/messages.py` (2171 lines) into its two halves.** Canonical persisted message model + per-role validators stay; wire-request shaping (request-history assembly, reasoning-replay projection, dangling-tool-call repair, notes→synthetic-user-message encoding, response parsing, ~1042–1850) moves to its own chat-domain module.
- [ ] **A4 🟡 code — Extract `/status` report building (~550 lines) from `core/chat/commands.py`.** `StatusModelDetails`, all `resolve_status_*`, `build_status_reply/text`, formatters (~1616–2170) are pure presentation with no dispatcher coupling → own status-report module in the chat domain.
- [ ] **A5 🟡 code — Extract operator-surface persistence from `core/tools/terminal_manager.py` (2237 lines).** Groups CRUD + launch history (~685–940) plus parse/persist helpers (~2162–2380), ~450 lines → operator-store module owned by the tools/terminal domain. Core PTY lifecycle stays. Second smaller candidate: attention detection/delivery (~1632–1750 + ~2236–2290).

### B. Merge candidates — one concept with several homes

- [ ] **B1 🟡 code — Provider HTTP plumbing copy-pasted 5–7×.** `_connect_stream`/`_post_json` closures in `openai.py`, `github_copilot.py`, `anthropic_compatible.py`, `openai_compatible.py`, `ollama.py` (+ same shape in `openrouter.py`, `opencode_go.py`) — identical down to verbatim comments. Extract shared `connect_streaming_with_retry(...)` (+ JSON-post equivalent) into `core/providers/_http_shared.py`; adapters keep header-builder and detail-formatting hooks.
- [ ] **B2 🟡 code — Frontend autosave debounce wiring copied in 11 components.** Identical timer/effect/cleanup block in 9 settings panels + `SkillDirectoryEditor.svelte` + `SystemPromptView.svelte`; `webui/src/lib/autosave.js` already owns save coordination. Add `createDebouncedAutosave({ participant, debounceMs })` there; panels shrink to config.
- [ ] **B3 🟡 code — Hand-rolled atomic temp-writes beside `core/utils/atomic.py`.** Private "write temp → os.replace" implementations in `cli/install_state.py`, `cli/autostart_management.py` (`_write_unit_file`), `cli/update_management.py`, `core/utils/server_control.py` — none with atomic.py's fsync guarantees. Route all through `core/utils/atomic.py` (add optional permissions/mode parameter for the 0o600 control-record case).
- [ ] **B4 🔵 code — Skill support-file path validation implemented twice.** Same normalize/reject rule in `core/tools/skill.py` (`_normalized_skill_file_path`) and `core/skills/authoring.py`; export one `normalize_skill_file_path()` from `core/skills`, tool maps error type.
- [ ] **B5 🔵 code — `_log_background_task_result` byte-identical 3× inside `core/tools`.** `bash.py`, `process_manager.py`, `terminal_manager.py` → one shared helper exported from `process_manager.py`.
- [ ] **B6 🔵 code — Frontend micro-helper family scattered.** `isPlainObject` ×11 with two incompatible semantics (`typeof`+Array check vs `toString === '[object Object]'`), `asText` ×5 (one deviating), `asOptionalText` ×3 → one tiny shared values module in `webui/src/lib`; pick one `isPlainObject` semantic deliberately.

### C. Types waiting to be born — parameter clusters

- [ ] **C1 🔴 code — Birth `SessionAddress(project_id, agent_id, session_id)`.** Triple travels through ~90 signatures + 100+ call sites (41 in sessions.py, 20 in chat.py, 11 in runs.py, 10 in statistics.py, 8 each in server/rpc/chat_methods.py and automation.py). Smoking gun: `runs.py` defines `SessionKey = tuple[str | None, str, str]`, `automation.py` duplicates it as `CompletionSessionKey` and unpacks positionally (`key[1], key[2]`) at ~10 sites; commands.py re-declares the three fields in three dataclasses. Home: frozen dataclass in `core/sessions/`; fold `SessionKey`/`CompletionSessionKey` into it; `SessionManager` methods take the address as one parameter. Do this BEFORE the chat.py split so moved code carries the new shape.
- [ ] **C2 🟡 code — Birth request-build inputs bag.** 13 kwargs (`replay_policy` … `session_messages_override`) assembled field-by-field from `_RunExecutionContext` + ModelTarget at 5 sites in chat.py (`:1435, :2018, :2087, :2911, :3851`). Either pass the context itself or a `RequestBuildInputs` frozen dataclass built once. Natural side effect of the A1 split — do together with it.
- [ ] **C3 🟡 code — Birth `ConnectionRef(provider_id, connection_id)`.** Pair travels through 20+ signatures across ≥6 files (runtime interfaces, providers credentials/usage/task_client, model_tasks/image.py, chat.py chunk-timeout/base-url resolution). Value type in `core/providers/`.
- [ ] **C4 🟡 code — Birth run-admission bundle.** `ChatRunManager.start()`/`enqueue()` take identical 8-kwarg sets and immediately re-fuse three into the session key. Follows almost for free from C1: accept `SessionAddress` + small `RunAdmission(working_project_id, run_kind, contributes_to_agent_activity, work_id)` in `core/runs/runs.py`.

### D. Documentation drift

- [ ] **D1 🔵 documentation drift — PROJECT.md module list incomplete.** `core/debug/` and `core/projects/` exist as code packages with indexed domain maps but are missing from the declared "Core modules" enumeration. Add both.
- [ ] **D2 🔵 convention deviation — main-file convention exceptions.** `core/debug` facades via `__init__.py` (13-line facade); `core/utils` has no real main file (callers import leaf paths). Either give both real main-file facades or record the exception in PROJECT.md.

## Checked and cleared (audited, legitimate — do not "fix")

- `cli/parser.py` (2077 lines): pure argparse construction, one builder per command area, zero logic — legitimately large; splitting adds indirection without depth.
- No folder-module's main file is a re-export shell; all 24 mains carry real implementation.
- `core/recall/recall.py` (184 lines): genuine deep seam (Protocols, contracts, backend registry), not a shallow relay.
- `model_tasks/task_execution.py` (48 lines): hides a real shared abstraction used by all task services.
- Provider adapters and `server/rpc/*_methods.py` groups: the declared dispatch/adapter pattern, not sprawl.
- Declared code-module set matches the actual directory tree exactly — no shadow modules.
- Retry/backoff math lives exactly once (`core/utils/retry.py`) and is consumed by providers, discovery, web_fetch; event fan-out lives once (`core/event_stream.py`); core-domain config persistence goes through `core/utils/atomic.py`.
- Sessions move/archive blocks share shape but differ materially in semantics (refuse-existing vs replace-prior, metadata stripping) — internal to one file, not worth coupling.
- Adapter `stream(messages, *, model_id, **kwargs)` params: open-ended by design, no fixed bundle.
- `data_dir` + path pairs: constructor-injected DI everywhere; don't travel through signatures.
- Attachment id/path/mime: store methods take only `attachment_id`; rest lives on `AttachmentRecord`.
- Token-store account triple `(provider_id, local_connection_id, account_id)`: real but confined to one class, ≤2 hops — trivially local.
- Frontend lib stores pass factory-built state bags, not loose id triples.

## Deferred candidates (need separate follow-up work after acceptance)

- Scalar-coercion helper family scattered across ~25 homes (`_positive_int` ×5 with drifting semantics, `_non_negative_int` ×2 identical, `_required_string` ×4, `_optional_string` ×6, …). Consolidate opportunistically when a file is next touched — NOT as a sweep (churn worse than the disease). Candidate owner: small scalars module in `core/utils` or fold into `core/tools/arguments.py`.
- Possible bug: frontend builds its session key from `(agentId, sessionId)` and omits the project anchor the backend key includes — probably harmless (UI context is per-agent), needs targeted investigation.
- Runtime god-object refactor (A2) is the largest single item; schedule as its own planned piece of work.
