# Slash Command Integration Plan

## Goal

Make built-in slash commands consistent across command catalog, dispatcher,
server chat entrypoints, WebUI command handling, Telegram/channel routing, tests,
and specs.

Requested command set for this pass:

- `/compact`
- `/new`
- `/help`
- `/retry`
- Existing `/stop` and `/status` must keep working.

No argument syntax yet. `/help compact`, `/queue clear`, and similar subcommands
are explicitly out of scope for this pass.

## Current Problems

- `/compact` is listed in `CommandDispatcher.BUILT_IN_COMMANDS`, but it is not
  routed by `CommandDispatcher.dispatch()`. Server delegates special-case it,
  so WebUI works while channels do not.
- `/new` creates and persists a new current Session, but the WebUI treats it as
  generic `command_handled` text and stays on the old Session.
- `/new` is not meaningful for Telegram's deterministic channel Session IDs.
  It should not claim that the channel conversation moved when it did not.
- Specs still say the current built-ins are only `/stop` and `/compact`.
- The existing command result shape only models "handled reply text". `/retry`
  needs to start a Run and return the normal Run/SSE payload on server accessors.

## Design

- Keep the command grammar exact-match only for now: a full pure-text message
  such as `/status`, `/retry`, or `/help`. Unknown slash text continues through
  normal chat/skill handling.
- Make `CommandDispatcher` the single recognition and catalog source for all
  built-ins.
- Add a command action result for commands whose execution belongs to an
  accessor/service layer instead of the dispatcher itself.
  - `compact` action: compact the target Session.
  - `new_session` action: create/switch Session where the accessor can honor it.
  - `retry_last_turn` action: retry the latest user turn.
- Keep `CommandHandled` for immediate text replies such as `/stop`, `/status`,
  and `/help`.
- Extend command-handled server responses only when needed with optional
  structured data, so existing command response tests remain stable for ordinary
  replies.
- Move manual compaction execution behind a shared core service method so server
  and channels do not diverge.
- WebUI should switch to the new Session when the server returns a handled
  `/new` response with a Session ID.
- Telegram/channel behavior for `/new`: reply that starting a new channel
  Session is not available yet, instead of creating an unrelated normal Session.

## Implementation Checklist

- [x] Update `core/chat/commands.py`:
  - [x] Add `/help` and `/retry` to `BUILT_IN_COMMANDS`.
  - [x] Register `/compact`, `/new`, `/retry`, `/help` in the dispatcher table.
  - [x] Add a command action result type for `compact`, `new_session`, and
        `retry_last_turn`.
  - [x] Keep `/stop` and `/status` as direct `CommandHandled` replies.
  - [x] Add `/help` reply text that lists current built-ins and briefly explains
        `/skill-name` vs `$skill-name` without adding argument support.
- [x] Add shared command action execution support:
  - [x] Add `TriggerService.retry_run()` for channel retries.
  - [x] Add `TriggerService.compact_session()` or equivalent shared helper for
        manual compaction from server and channels.
  - [x] Preserve current compaction rules: unavailable service reply, no active
        Run, same summary-model resolution, adapter close discipline, append one
        `compaction_checkpoint` on success.
- [x] Update `server/delegates.py`:
  - [x] Remove server-only `/compact` pre-special-casing from `chat.send` and
        `chat.stream`.
  - [x] Route all pure-text commands through `CommandDispatcher`.
  - [x] Map command actions to server behavior:
        `compact` -> handled reply, `new_session` -> handled reply plus data,
        `retry_last_turn` -> normal Run/SSE response.
  - [x] Keep unknown slash text flowing into normal Run/skill activation.
- [x] Update Telegram/channel command handling:
  - [x] Handle action results in both eager plain-text dispatch and queued
        processing.
  - [x] `/compact` should use shared compaction action.
  - [x] `/retry` should retry and relay the resulting Run.
  - [x] `/new` should return a truthful unavailable reply for channel sessions.
- [x] Update WebUI:
  - [x] Recognize `/new` command-handled response data and switch the current
        Agent/session state to the returned Session ID.
  - [x] Keep `/compact` history reload behavior.
  - [x] `/retry` should follow the normal Run response path and subscribe to SSE.
- [ ] Update tests:
  - [x] Core command tests for catalog, help, and action results.
  - [x] Server delegate/RPC tests for `/compact`, `/new`, `/retry`, and command
        catalog ordering.
  - [x] Telegram tests for action handling, including channel `/new` reply.
  - [x] WebUI tests for `/new` session switching and `/retry` Run subscription.
- [x] Update specs:
  - [x] `.vorch/specs/chat.md`
  - [x] `.vorch/specs/server.md`
  - [x] `.vorch/specs/webui.md`
  - [x] `.vorch/specs/channels.md`
  - [x] `.vorch/specs/automation.md` if `TriggerService` grows command helpers.
- [ ] Verification:
  - [x] Focused backend quality for changed Python modules/tests.
  - [x] Focused frontend quality for changed Svelte/tests.
  - [x] Full backend quality gate.
  - [x] Full frontend quality gate if WebUI files changed.
  - [x] Commit one logical unit.

## Verification Log

- Focused backend: `python scripts/quality.py core/chat/commands.py core/automation/automation.py core/channels/telegram.py server/delegates.py tests/core/chat/test_commands.py tests/core/channels/test_telegram.py tests/server/test_rpc.py tests/server/test_delegates.py` passed.
- Focused frontend: `python scripts/quality-frontend.py webui/src/components/ChatView.svelte webui/src/components/__tests__/ChatView.test.js` passed.
- Full backend: `python scripts/quality.py` passed with 2213 tests after the
  smoke-fix regression test.
- Full frontend: `python scripts/quality-frontend.py` passed with 375 tests and build.
- Live smoke: `/help` and `/new` worked in WebUI. `/compact` exposed a no-model
  error escaping as an RPC error; fixed by keeping provider/model resolution
  failures inside the command reply path. Retest confirmed `/compact` now shows
  inline command feedback instead of a send failure.
- Commit: `feat(chat): integrate slash commands`.

## Notes / Risks

- `/retry` cannot be represented as `command_handled`; it must produce the same
  shape as `chat.retry_last_turn` on server accessors.
- Channel `/retry` should use the non-streaming chat loop through
  `TriggerService` and relay final output through the existing channel event
  relay.
- Channel `/new` needs a larger route-mapping design to truly rotate channel
  Sessions. This pass should avoid lying to users and document the limitation.
- Command specs must be updated in the same commit because stale specs were one
  of the causes of the current drift.