# Flagged Concerns

log of deferred concerns. Newest at the bottom. Don't reorganize.

---

## 2026-06-07 — Compaction: deferred robustness/edge cases

Found during a review of `core/compaction/`. The items below were deliberately
**not** fixed and are recorded here.

### 1. Final-response auto-compaction rebuilds request messages then throws them away

`core/chat/chat.py`: when a turn ends with a final assistant message and no tool
calls, the loop calls `_maybe_auto_compact(...)` and assigns the result to
`messages` — but the very next line is `return assistant_message`, so that
rebuilt `messages` list is never used.

**Why it's wasteful:** the turn is already over, so the only thing that needs to
happen at this point is persisting the checkpoint (`session.append(checkpoint)`),
which sets up the *next* turn. But `_maybe_auto_compact` also runs the full
`_build_request_messages(...)` rebuild — re-assembling the system prompt and
re-running attachment resolution — purely to produce a list that the caller
immediately discards. It's CPU work with no effect.

Contrast with the **mid-tool** call site, where the rebuilt `messages` *is*
needed: there the loop continues and sends another provider request, so it must
continue with the compacted (smaller) message list. There the rebuild is
correct and necessary.

**Possible cleanup:** split the two cases so the final-response path only
persists the checkpoint and skips the request rebuild.

### 2. Investigate a precise context size during a run (mid-turn)

The auto-compaction threshold check uses the provider's *real* reported
`usage.input_tokens` at the end of a turn, but mid-turn (between tool-result
cycles) the provider hasn't reported usage for the next request yet, so it falls
back to the character heuristic `estimate_messages_tokens(messages)`
(`core/utils/tokens.py`, ~4 chars/token), which under-counts tool/function
schemas and other provider-side overhead.

**To look into:** whether a precise context size is obtainable *during* a run —
a provider token-count endpoint, a real count carried over from the previous
request's reported usage, or a tokenizer-aware local measurement — so the
mid-turn threshold no longer leans on the estimate. Today the estimate stays
conservative enough to be acceptable; this is "do better if a precise source
exists," not a known bug.

---

### 1. Local embedding engines

The registry hook for local task targets is available and dependency-free (same pattern as local STT/TTS engines), but no local embedding engine is integrated. The `EmbeddingService` currently rejects local targets with `EmbeddingUnsupportedTargetError`.

### 3. Background/write-time incremental indexing

The store uses eager-on-search backfill: the first `search` after enabling `vector` embeds all missing/stale sessions, and every subsequent search diffs freshness incrementally. A background indexer or write-hook in `core/sessions/` would eliminate the latency spike of first-search backfill for large session histories.

### 5. Embedding providers other than OpenRouter

The `core/embeddings/` domain is provider-agnostic by design, but only the OpenRouter discovery path and `/api/v1/embeddings` wire are implemented. Adding e.g. direct OpenAI, Anthropic, or local embedding providers requires supplementary discovery + provider-specific `ProviderEmbeddingClient` routing.

### 6. Asymmetric `input_type` query/document embedding

The OpenRouter embeddings API supports `input_type` hints (e.g. `"query"` vs. `"document"`) for models that optimize embeddings differently per task. Currently ignored — both queries and sessions are embedded symmetrically. Fine for general-purpose models, but some specialized embedding models (e.g. Cohere) produce better results with the hint.

---

## 2026-06-08 — Embedding input truncation: remaining deferred work

Embedding input is kept under the model's token cap by a conservative character heuristic
(`_CHARS_PER_TOKEN`/`_INPUT_TOKEN_SAFETY` in `core/recall/vector_store.py`) plus a self-correcting
shrink-and-retry loop on context-length overflow (`_run_embed` in `core/recall/vector.py`). **Still
deferred:** option **(a)** a tokenizer-aware budget (e.g. `tiktoken`) to avoid the occasional wasted
first embedding request entirely, and per-session chunking so very long sessions are not represented
by their head alone. Neither is worth building yet.

## 2026-06-09 — OpenAI provider merge: cosmetic test-fixture debt

**Purely cosmetic test-fixture debt** flagged while reviewing the merge of the `openai-subscription` provider into the existing `openai` provider (now a second `subscription` connection). Intentionally not fixed in the same change.

### 1. `OPENAI_DATA` fixture in `tests/core/providers/test_providers.py` still describes the pre-merge shape

`OPENAI_DATA` (line 30) and `OPENROUTER_DATA` (line 60) are pre-merge-era test fixtures used by generic parser tests (validation of `_parse_config`, OAuth device flow parsing, etc.). `OPENAI_DATA` still uses `adapter: "openai_compatible"` and the old `oauth` placeholder connector with `credential_key: "OPENAI_OAUTH_TOKEN"`. The new `resources/providers/openai.json` no longer has either; they were removed in Phase 5.

**Why it still passes:** these fixtures test the *generic* parser, not the real provider config. They construct arbitrary valid config dicts in memory and assert that the parser turns them into the right dataclass. The test at `test_connection_without_mode_or_models_endpoint_remains_none` (line ~860) calls `config.get_connection("oauth")` and `config.get_connection("api-key")` against this fixture — so the test still works as long as the fixture has both connections.

**Why deferred:** cleaning it up means rewriting the fixture, then chasing every test that depends on its specific shape (the `oauth` / `api-key` connection ids, the `OPENAI_OAUTH_TOKEN` env var, etc.). Pure test refactor, no production behavior. Out of scope for the merge, and the merge is a feature change, not a test-hygiene drive. Do it in a focused follow-up PR.

## 2026-06-11 — Linux readiness: remaining unverified pieces

A Linux-readiness audit found the process layer already platform-branched (POSIX kill via
`os.killpg`, `start_new_session`, bash-tool runs real `bash` off-Windows). Still open:

- **sqlite-vec on the actual Pi is unverified.** `core/recall/vector_store.py` hard-imports
  `sqlite_vec` and loads it as a native SQLite extension; needs an aarch64 wheel for the Pi's
  Python and a `sqlite3` built with extension loading. Verify on first Pi deploy (64-bit
  Raspberry Pi OS required) before enabling `recall.backend: vector`.

## 2026-06-15 — Model DB Phase 7 (docs): consciously deferred rebuild items

3. **No second model-DB read-root for custom user providers.** `ModelRegistry.load`
   reads exactly one `<resources_dir>/models` tree and caches by that resolved path.
   A user-defined provider whose model files live outside the bundled `resources/`
   (e.g. under the data dir) has no read root and no invalidation hook. Adding a
   second root + cache-invalidation across both is deferred until custom providers
   are a feature.

4. **Anthropic is an untested stub.** `resources/models/anthropic.json` exists but
   there is no Anthropic normalizer registered in `discovery._DISCOVERY_ADAPTER_MAP`,
   so Anthropic catalogs cannot be refreshed through the pipeline. Anthropic was
   deliberately skipped in the Phase-3 regeneration (no normalizer, no key). Building
   the normalizer is deferred.

## 2026-06-15 - Model DB live-test follow-ups (opencode-go minimax-m3)

Residual cleanup/scope noted while the user live-tested the refreshed DB in the worktree.

1. **opencode-go override mixes two enrichment styles.** 16 models carry explicit
   hand `context_window`/`max_output_tokens` (the gateway's own limits, which can
   deviate from the lab), while `minimax-m3` / `qwen3.7-max` carry a `canonical`
   pointer and inherit those fields from the canonical base (the gateway matches the
   lab for them). Both are correct, but the split is inconsistent. A future cleanup
   could convert every opencode-go model whose gateway limits equal the lab's to a
   pointer (DRY, single source) and keep explicit numbers only where the gateway
   genuinely deviates. Deferred: it is an audit of all ~18 entries against canonical,
   not a behavior fix.

2. **Native `on_off`/`budget` reasoning wiring still pending (see Phase-7 item 1
   above).** The wire still sends a generic effort (or Anthropic adaptive
   thinking), not the model's native toggle/budget parameter. Wiring those request
   shapes remains deferred until a reachable model needs the native shape.


## 2026-06-16 - Provider usage probe: blind Copilot/MiniMax fetchers

The live provider usage probe (`core/providers/usage.py`, Statistics → Limits subtab)
ships with one live-verified fetcher and two blind ones.

1. **OpenAI `openai:subscription` is live-verified** (2026-06-16, HTTP 200): the real
   `/wham/usage` body matches openclaw's shape and the parser yields correct 5h +
   weekly windows. No follow-up.

2. **GitHub Copilot `github-copilot:oauth` is blind.** `copilot_internal/user` parsing
   is implemented from openclaw's field names but not live-verified (no Copilot login in
   this environment). Pinned by unit tests; degrades to an "unavailable" snapshot on
   shape mismatch. Live-verify when a Copilot login exists.

3. **MiniMax `minimax:api-key` is blind.** `token_plan/remains` parsing — especially the
   remaining-vs-total count keys (MiniMax misnames "usage" as "remaining") — is an
   assumption pinned only by unit tests. Live-verify when a MiniMax Token Plan key is
   available; the percent could be inverted if the count semantics differ.


## 2026-06-17 — Native reasoning wiring landed; provider assumptions unverified

The unified reasoning-intent layer is built and all five adapters render through it
(`resolve_reasoning_intent` in `core/providers/reasoning.py`). This **resolves** the
previously-flagged "no wire support for `budget`/`on_off`" items (2026-06-15 Phase-7 #1
and 2026-06-15 live-test #2): budget Claudes now send native `thinking_budget`, `on_off`
models toggle natively, and `/status` reports the rendered budget/state. Remaining
unverified pieces (no credentials in this environment — the Phase-0 gate could not probe):

1. **Anthropic `budget_max` numbers are hand-seeded, not live-verified.**
   `resources/models/anthropic.overrides.json` seeds `control: budget` +
   `budget_max` for `claude-opus-4-20250219` (32000) and `claude-sonnet-4-20250219`
   (64000), using each model's max output as the budget ceiling. The feed leaves
   `budget_max` `None` for every Claude, so these are conservative estimates pinned by
   tests. Live-verify the accepted thinking budget (and whether these older Claudes
   accept `budget_tokens` vs the newer adaptive `output_config.effort`) when Anthropic
   credentials exist; correct the override numbers if the API disagrees.

2. **OpenRouter `on_off` off-shape is assumed.** A `none` selection on an `on_off`
   OpenRouter model renders `reasoning: {enabled: false}` (a documented OpenRouter
   param); the exact disable shape (`enabled:false` vs `exclude` vs omit) was not
   probed. Effort-spelled-off wires keep the byte-identical `{effort: "none"}`.
   Live-verify when an OpenRouter key + a reachable `on_off` model exist.


## 2026-06-18 — Projects (Plan 1, kern+cli): consciously deferred edges

1. **`channel` tool still resolves a path against `workspace`, not `effective_cwd`.**
   The workspace→cwd switch covered `read`/`write`/`edit`/`search`/`grep`/`glob`/`bash`
   (and `memory` deliberately stays on workspace). The `channel` tool
   (`core/tools/channel.py`, the path-resolving line) was not switched. Low impact:
   channels-on-project is deferred, so a channel session never carries a project cwd and
   `effective_cwd` would fall back to `workspace` anyway. Switch it to `effective_cwd`
   when channels learn projects, for consistency.

## 2026-06-21 — WebUI long-lived tab: unbounded background-session run-event accumulation

Found while diagnosing a long-lived-tab performance report (stream stutter that clears the
instant the tab is hidden/minimized/reloaded). The B10 fix (`b3edb5f`) bounded the dedup
key set and prunes the **displayed** session's `runEvents` on `loadHistory`, but two growth
vectors stay open for **background** sessions (channels, cron, sub-agents) the user never
opens:

1. **`chatState.sessions` is never pruned.** `ensureSessionState` is the only writer; no
   code deletes session entries. Every `(agent, session)` that ever receives a `/ws` run
   event keeps a permanent entry for the tab's lifetime.

2. **Per-session `runEvents` has no cap and an O(n) dedup.** `appendRunEvent` linear-scans
   the whole `runEvents` array per non-streaming event, and background sessions never reach
   the `loadHistory` prune path — so under sustained background activity the arrays grow
   unbounded and per-event cost climbs (O(n²) overall).

**Not the cause of the reported incident** (the tab was idle — Telegram connected but
unused, no sub-agents — so no run events were flowing; removing the always-on `.pulse-dot`
ripple animation in `5c640f5` addressed that case). This is latent: it only bites under
heavy background workloads. Deferred because the fix has subtle edges — pruning a background
session can drop the live sub-agent `running`/queue projection a still-rendered row relies
on, so it is **not** a pure "longer load time" change. Fix carefully when background-heavy
use makes it matter: cap per-session `runEvents`, and LRU-evict non-displayed session
states, releasing their status-verification guards like the existing
`subAgentGuardKeysForEvictedStatuses` path already does for evicted status keys.

## 2026-06-22 — `/agent` session move: deferred scope (v1)

The new `/agent` command moves a running session (full verbatim history, same session id) to another agent. Consciously out of scope for v1, recorded here:

1. **No channel support.** `/agent` is WebUI-only; channel-bound sessions are explicitly *refused* (they carry `source_channel_id`, which a move would orphan). When channels can retarget their own session pointer, lift the refusal in `_handle_move_session_command` (`server/rpc/chat_methods.py`) and let channel sessions move.

2. **No CLI support, and no argument autocomplete/picker.** The target address is free-typed (like `/handoff`); there is no `/agent ` autocomplete. Deferred with the rest of the CLI accessor work.

3. **No live cross-window update, no undo.** A second window parked on the source agent sees the move only on its next load (no WS broadcast in v1 — consistent with the rest of the system).

4. **Recall reconcile hook intentionally NOT built — the plan's sanctioned fallback.** The plan defaulted to a targeted source-scope delete of the moved session's derived-index rows, with an explicit "fall back if the hook turns out heavier than a nudge." Verified against the source: it is heavier **and** unnecessary. Both derived backends already scope search results to the source agent's *active* candidate summaries (`SqliteFtsRecallBackend`: query `session_id IN (active)` + `_cleanup_missing_sessions`; `VectorRecallBackend`: `(agent_id, project_id)` + active-summary filter + `_ensure_fresh_index` drop). A moved session can never surface under the old agent, and stale rows self-heal on the next source-scope search. A `drop_session`/reconcile method would have to be added to the `RecallBackend` Protocol and all four implementations plus a runtime hook and orchestrator wiring — a real interface widening for zero correctness gain. So the fallback was taken (see `recall.md` → Cross-Domain Rules). Only residue: dead index rows linger until the next source search lazily clears them (harmless; indexes are disposable).

## 2026-06-22 — Anthropic provider: prior flagged items RESOLVED, one niche edge left

An Anthropic API key was added, unblocking the deferred Anthropic work. Built and live-verified (2026-06-22) against the real `/v1/models` + `/v1/messages`. Resolves earlier entries:

- **RESOLVED — "Anthropic is an untested stub" (2026-06-15 Phase-7 #4).** Anthropic now has a discovery normalizer (`AnthropicAdapter.normalize_catalog_entry` + `discovery_headers`/`discovery_params`) and is registered in `discovery._DISCOVERY_ADAPTER_MAP`, so a Model-DB refresh fetches it like every other provider. The hand-written 2-model stub (which pointed at the already-retired May-2025 `claude-opus-4-20250219` / `claude-sonnet-4-20250219` — invalid ids) was deleted; `resources/models/anthropic.json` is now generated (9 real models). The live `/models` endpoint is rich (context, output, modalities, structured-output, reasoning control + effort ladder), so the normalizer maps it directly and the canonical join supplies reasoning at load.
- **RESOLVED — "Anthropic `budget_max` hand-seeded, not live-verified" (2026-06-17 #1).** The guessed `budget_max` numbers (and the stub overrides) were removed. Reasoning control is now per-model from the live caps / canonical layer: `adaptive` → `levels`, native `enabled` → `budget`. Live-verified the split: `enabled` budget 400s on Opus 4.7+/Fable, accepted on 4.6 and earlier; `adaptive` 400s on the budget-only models. Budget Claudes carry no `budget_max` (feed publishes none) → fallback ladder.
- **FIXED (newly found) — provider-default `temperature` broke modern models.** The provider config defaulted `temperature: 0.7`; the adaptive-only models (Opus 4.7+, Fable 5) reject `temperature` with a 400 whenever thinking is not active — so every flagship Claude was unusable without an explicit effort. Removed the default and made `_build_payload` drop sampling params for models whose discovery-derived `metadata.anthropic.supports_temperature` is `False`.
- **Edge handled — Opus 4.5.** Exposes an effort ladder but not adaptive thinking, so canonical's `levels` would render adaptive and 400. Pinned to `control: budget` in `anthropic.overrides.json` (live-verified). The override file now carries only this one durable correction.

**Still open (niche, now unblocked):** API tolerance for replayed opaque thinking blocks under *explicitly disabled* thinking was not re-probed this round (separate from the catalog/reasoning/sampling work). The conservative strip-guard stands; credentials now exist to verify it if it ever bites (see `providers/anthropic.md`).

---

## 2026-06-23 — Observed channel messages: grouped reminder lacks channel identity

When the chat layer groups passively observed channel messages into the single "Messages in the channel since your last turn:" reminder (`core/chat/messages.py` → `_channel_messages_reminder_block`; see `chat.md` → observed channel context), it works only from the per-message notes, which carry the **sender** but not the **channel**. The header is therefore generic.

**What would be better:** carry the channel name and channel id alongside each observed message (the listening side in `core/channels/` already has both when it writes the note) so the grouped reminder can name the source — e.g. "Messages in <channel> since your last turn:" — giving the agent richer context about where the messages came from. Deferred deliberately (user decision 2026-06-23); the generic header is the current behavior.

## 2026-06-24 — Install bootstrap + release CI: unverified end-to-end

The one-shot bootstrap scripts (`scripts/bootstrap.sh`, `scripts/bootstrap.ps1`), the new `-SkipWebuiBuild` path in `install.ps1`, and the release workflow (`.github/workflows/release.yml`) are **syntax-validated only** — they cannot be exercised in this environment (no fresh OS, no winget/apt run, no GitHub Actions run). The `vbot update` *logic* is unit-tested with injected runners/HTTP, but the real git/pip/npm subprocesses and the prebuilt-asset download are not exercised. Verify on first real use: (1) publish a GitHub Release so the workflow builds and attaches `webui-dist.tar.gz`; (2) run the release-track one-liner on a clean Windows host and a clean Raspberry Pi; (3) run `vbot update` on both a release-track and a dev-track install. Until a Release with the asset exists, only the `--dev` bootstrap track and dev-track `vbot update` work — the release track fails fast ("could not determine the latest release" / "no webui-dist.tar.gz asset").

**Update (2026-06-24):** the release CI is now verified end-to-end — publishing `v0.1.1` triggered the workflow, which built and attached `webui-dist.tar.gz` (~240 KB), and the `releases/latest` API serves the tag + asset URL the bootstrap and `vbot update` consume. Still unverified: the bootstrap scripts executing on a fresh Windows/Pi host (winget/apt install, clone, unpack) and a real `vbot update` run with live git/pip/npm subprocesses.

## 2026-06-24 — `vbot autostart` command: real-OS enable path unverified

`vbot autostart enable|disable|status` (`cli/autostart_management.py`) is unit-tested with an injected runner (argv construction, OS branch behavior, idempotent disable, start-now), but the **real** OS calls are not exercised here. Windows `schtasks /Create` is denied without an elevated terminal in this environment (verified: both `schtasks` and PowerShell `Register-ScheduledTask` return access-denied when non-elevated — a Windows reality that also affects the old installer), and there is no systemd to exercise the Linux path. So the success path — a task/unit actually created and the server actually started — needs verification on a real **elevated** Windows host and on a real Linux/Pi. The installers now call `vbot autostart enable` by default (opt out with `-NoAutostart` / `--no-autostart`), so that same real-OS check covers them; on Windows the installer must run elevated for autostart to take, and reports a non-fatal warning otherwise.

## 2026-06-24 — Windows bootstrap now uses a venv + PATH shim (unverified on real Windows)

`bootstrap.ps1` now mirrors the Linux bootstrap: it creates `~/vbot/.venv`, installs into it (runs `install.ps1 -SkipPathUpdate` with the venv first on PATH), and exposes only `vbot` via a `~/vbot/bin/vbot.cmd` shim added to the user PATH — so the venv's `python`/`pip` do not shadow the user's. The dev track now also installs the `dev` extras on both OSes (parity with `vbot update`). Not exercised here (no fresh Windows): verify on a real Windows host that the venv is used, that a new terminal resolves `vbot` via the shim, and that the autostart task points at the venv `vbot.exe`.

## 2026-06-24 — Install/update/autostart review: code fixes landed, real-OS verification still open

A fresh-reviewer pass over the install/update/autostart machinery (the leads in `stuff/HANDOFF-install-update-autostart.md`) found and **fixed in code** four issues, all unit-tested with injected runners/HTTP:

1. **WebUI unpack crashed on Pythons without PEP-706 filters.** `_download_webui` passed `filter="data"` unconditionally and caught only `(tarfile.TarError, OSError)`, so on a CPython lacking the extraction filter (stock Raspberry Pi OS Bookworm may ship 3.11.2; the param/backport is 3.11.4+/3.12+) the unknown-keyword `TypeError` escaped as an uncaught traceback. Now feature-detected via `hasattr(tarfile, "data_filter")` with a same-tree-guard fallback (`_extract_within`), and the except also catches `ValueError`.
2. **Managed restart fought systemd.** `vbot update` and `vbot server restart` killed the unit's process out-of-band (psutil) and spawned an unmanaged detached replacement, racing the unit's `Restart=` directive (forced-kill case) or silently desyncing systemd's view (clean-exit case). Restart is now systemd-aware: `cli/server_management.py` `restart_server` / `restart_via_systemd_if_managed` / `is_systemd_managed` delegate to `systemctl --user restart <unit>` when a `<service-name>.service` user unit exists and is active. `--service-name` (default `vbot`) is plumbed through `update` and `server restart`. **Still needs a real Pi to confirm** the unit detection and `systemctl --user restart` round-trip.
3. **Release track re-downloaded the WebUI every run.** `_advance_release` downloaded+extracted the asset before the up-to-date check, so an already-current release re-fetched `webui-dist.tar.gz` on every `vbot update`. Now it computes the post-checkout HEAD and skips the download when the tag is unchanged and `webui/dist/index.html` is present.
4. **No subprocess timeouts / interactive git prompt.** `_default_runner` ran git/pip/npm with no `timeout` and could hang forever on a stuck network or a credential prompt. Now every command runs under `_COMMAND_TIMEOUT_SECONDS` (returns rc 124 on timeout) with `GIT_TERMINAL_PROMPT=0`.

Doc/UX hardening in the same change: `bootstrap.sh` usage no longer references the removed `--enable-autostart`/`--start-server` flags; both bootstraps point a re-run at `vbot update` and add a path-traversal guard before `tar -xzf` (mirroring `update`'s data filter); the uninstallers hint to pass a custom `--service-name`/`-TaskName` when the default-named unit/task is absent.

**Refuted/limited leads (verified sound, not changed):** the shallow-clone update path is correct (release uses an explicit `tag` refspec; dev uses a pure `--ff-only`), the Windows venv shim/PATH design is sound, `.github/` keeps only `workflows/` tracked, and the bundled skill + `cli.md` matched behavior. The intentional decisions (clean-break flags, `curl|bash`, `--discard` hard reset, venv-only `vbot`, agent-facing English) were left untouched.

## 2026-06-24 — Uninstall was not bootstrap/venv-aware: FIXED, real-OS removal still unverified

Found while preparing the WSL live test: `uninstall.{sh,ps1}` ran `pip uninstall` against the **active/system** interpreter and only ever removed the autostart unit/task. For a one-line **bootstrap** install (self-contained `~/vbot` with its own `.venv`, a `~/.local/bin/vbot` symlink / `~/vbot/bin` shim, and the autostart entry) that pip uninstall was a no-op and nothing else was removed — so `vbot` kept running and resolving after a "successful" uninstall. The released v0.1.2 uninstaller has this gap.

**Fixed (this change, shipped in v0.1.3):** the bootstrap now drops a `.vbot-bootstrap` marker at the install root, and the uninstallers branch on it — marker present → remove the whole tree (venv + source), the launcher (Linux symlink if it resolves into the tree; Windows shim dir + its user-PATH entry), and the autostart entry, after stopping the server (systemd `disable --now`, plus a best-effort `vbot server stop` so the Windows venv unlinks). No marker → unchanged manual pip-uninstall behavior, so a developer checkout that merely has a `.venv` is never deleted. The data dir (`~/.vbot`) is never touched in either mode. Hard guards refuse to remove `/` or `$HOME`.

**Still unverified end-to-end (needs real hardware, same thread as the entries above):** the bootstrap-mode removal cannot be exercised here — confirm on a real Linux/Pi (the systemd stop + `rm -rf` of the live tree + symlink removal) and a real Windows host (`Unregister-ScheduledTask` needs an elevated terminal — non-elevated leaves the task and warns; the venv unlock after `server stop`; the `~/vbot/bin` PATH entry removal). The WSL plan (`stuff/HANDOFF-wsl-test.md`) now expects a clean uninstall instead of the old gap.

**Still unverified end-to-end (needs real hardware, unchanged from the entries above):** a fresh-OS bootstrap on Windows + Raspberry Pi, a real `vbot update` on both tracks with live git/pip/npm, the systemd restart on a Pi, and `schtasks /TR` task creation/run on an elevated Windows host (the multi-layer quoting around space-containing paths is the open risk there — the action-string construction is unchanged).

## 2026-06-24 — WSL live test ran for real: restart-timeout bug fixed; no downgrade path (open)

The Linux live test (`stuff/HANDOFF-wsl-test.md`) was finally executed on real systemd (WSL2, Ubuntu 24.04, `systemctl --user` working) — closing the "never run for real" gaps for the **release-track `vbot update`** and the **bootstrap uninstall**. Passed: install of a pinned older release (`--version v0.1.2`), `vbot` on PATH in a fresh login shell, systemd-routed `server restart`, the idempotent second `update` (no WebUI re-download, ~2s), and the v0.1.3 wholesale **uninstall** (tree + venv + launcher + unit + running server removed, `~/.vbot` preserved). Two things surfaced.

**Fixed in this change — `vbot update`'s systemd restart could falsely report "systemctl unavailable" + exit 1.** `_run_systemctl` (`cli/server_management.py`) capped *every* systemctl call at 10s, but a `restart` blocks on the unit's stop+start and the installed unit ships `TimeoutStopSec=10`; when the server's graceful shutdown approached that 10s wall, the `systemctl --user restart` subprocess hit its own identical 10s timeout, raised `TimeoutExpired`, and was collapsed into the misleading "systemctl unavailable" (exit 1) — even though systemd then completed the restart. Host-independent (any Pi with a slow drain hits it), which is why `server restart` passed (fast drain) but `update`'s restart failed (slow drain) minutes apart. Fix: split the cap into `_SYSTEMCTL_PROBE_TIMEOUT_SECONDS` (10s) and `_SYSTEMCTL_RESTART_TIMEOUT_SECONDS` (30s, selected per verb), and report a timeout distinctly (rc 124, "systemctl timed out after Ns") instead of "unavailable". Unit-tested in `tests/cli/test_server_management.py`. **Underlying trigger not addressed:** the server sometimes takes the full 10s to drain on SIGTERM ("Waiting for background tasks to complete" → SIGKILL) even with no providers/channels configured — worth a look at shutdown responsiveness, but the restart is now robust regardless of drain time.

**OPEN — there is no downgrade / rollback path.** If a user is on the latest release and it is buggy, there is no supported command to go back to an older release. `vbot update` is **forward-only**: it always resolves `releases/latest` (`cli/update_management.py` `_advance_release` → `_fetch_latest_release`) and exposes no `--version`/`--tag` (`cli/parser.py` `_add_update_parsers` — only `--discard`/`--stash`/`--no-restart`/`--service-name`). The bootstrap's `--version <tag>` installs a specific old release, but `clone_repo` (`scripts/bootstrap.sh:117`) **refuses if `~/vbot` already exists** ("run 'vbot update'… otherwise remove it"), so it cannot downgrade in place. The only routes today are manual: delete `~/vbot` and re-bootstrap with `--version vX.Y.Z`, or hand-run `git fetch tag` + `checkout --force` + `pip install -e` + re-fetch the old WebUI + restart. Worth deciding: add `vbot update --version <tag>` (and/or let the bootstrap reinstall over an existing tree) so a bad release has a first-class rollback. Found during the WSL live test.

**WSL caveat for future runs:** `loginctl enable-linger` returns success but linger stays `no` in WSL2 (each `wsl.exe` call is a transient, non-PAM session), so the systemd **user manager** is torn down/respawned between separate `wsl` invocations, repeatedly stop/starting the enabled unit and intermittently leaving an orphan holding the port. Environmental (a real Pi with a login session + linger is stable), but it makes systemd-dependent assertions flaky across discrete `wsl` calls — probe health with a short retry rather than trusting a single hit.

## 2026-06-28 — Desktop accessor: deferred caveats

Recorded while documenting the Desktop accessor launch work (Phases 1–6 shipped, Windows live-tested).

1. **Linux desktop shortcut + client mode not real-hardware-tested.** `scripts/install.sh --desktop` (writes `~/.local/share/applications/vbot-desktop.desktop` → `vbot desktop`) and `--desktop-client` (server-less `.[cli,desktop]` install), plus the matching `.desktop` removal in `scripts/uninstall.sh`, are built and syntax-correct but were not run on a real Linux desktop session. The Windows equivalents (`-Desktop`/`-DesktopClient`, the Start-menu `.lnk`) were live-tested. Verify on a real Linux host with a graphical session that the menu entry appears and launches, the client-mode install has no server stack, and the uninstaller removes the entry.

2. **Shell connection screen is English-only (i18n deferred).** The native in-window connection screen (`desktop/connection.py` → `build_connection_html`, including the four probe-failure error copies) hard-codes English strings, not the i18n system. This deliberately mirrors the prior English-only Desktop fallback page. Route it through an i18n mechanism when the Desktop shell gains localization; until then it is the one user-facing Desktop surface outside the WebUI that is not translatable.
## 2026-06-30 — Agent-authorable skills: deferred caveats

Recorded while implementing the skill-authoring plan (M1–M4 shipped, backend + WebUI, full gates green).

1. **Config (project) agents share a per-agent skill home keyed by bare id.** `skill_manage` and the agent-aware `skills_for` key an agent's private skill home on `context.agent_id` → `<data_dir>/agents/<id>/skills/`. For an *identity* agent this id is its unique store id (correct). For a *config/project* agent the run's `agent_id` is the bare scanned id (e.g. `builder`), so two different projects' `builder` agents would share `<data_dir>/agents/builder/skills/` — a cross-project leak if a config agent ever authors a private skill. The write-scope boundary is identity-agent-centric by design (the plan defers project-scope authoring), and config agents naturally write project skills via the file tools instead, so this is an edge, not a live bug. If config-agent private skills become a real use case, key the home on a project-qualified id (or block `skill_manage` for config agents).

2. **`/learn` brief is a constant, not `resources/prompts/learn.md`.** The plan listed `resources/prompts/learn.md` as an option; I chose a `LEARN_INSTRUCTION` constant in `server/rpc/chat_methods.py` (consistent with `HANDOFF_INSTRUCTION`, no RPC-time file I/O). If learn-prompt customization (per-agent override, user editing) is ever wanted, promote it to an editable resource/prompt fragment.

3. **Command autocomplete is not rooted-aware for skills.** `catalog_methods._command_skill_suggestions` passes the address's raw `project_id` (None for a rooted identity agent) to `skills_for`, so a rooted identity agent's `/`–`$` autocomplete omits its home project's skills, while the run-time catalog and the `skill` tool (which thread the rooted `skill_project_id`) include them. Autocomplete is a best-effort hint surface, so this is a minor inconsistency, not a correctness bug; align it by resolving the rooted project in the autocomplete path if it matters.
