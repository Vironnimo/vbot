# Settings Domain Extraction Plan

## Goal

Extract Settings update-schema validation into a dedicated domain while preserving behavior.

## Scope

In:
- Create `core/settings/` for public Settings schema parsing and validation.
- Move `settings.update` section parsing for Appearance, Skills, Sub-Agents, Compaction, and Defaults out of `server/delegates.py`.
- Keep existing RPC request/response shapes and error codes unchanged.
- Add focused unit tests for the new Settings parser and keep existing RPC/storage tests passing.
- Add/update `.vorch/specs/settings.md`, the project spec index, and neighboring docs.

Out:
- No WebUI behavior changes.
- No change to raw `settings.json` file format.
- No move of prompt fragment storage, `.env` loading, or atomic file I/O.
- No migration of all `StorageManager` settings helpers in this pass.
- No new persisted settings sections.

## Hidden Constraints

- `settings.update` maps malformed public payloads to RPC `invalid_request`, while storage-level validation failures still map through the normal domain-error path.
- Updating Skills must reload the runtime skill registry immediately.
- Sub-Agent settings are currently stored as top-level integer keys in `settings.json`.
- Defaults updates are sparse; `null` removes individual Agent default keys, and `thinking_effort: ""` means explicit provider default.
- Compaction update payloads require all four fields and validate `threshold` in `(0, 1]`.
- Storage helpers and server RPC tests currently rely on exact validation message fragments.

## Risks

- Moving parser code can accidentally change RPC error codes if parser exceptions are mapped incorrectly.
- Settings constants are duplicated between server parsing and storage normalization today; this pass should reduce server coupling without widening the Storage refactor.
- WebUI auto-save depends on existing settings response payloads and should not observe a contract change.

## Done When

- [x] `core/settings/` exists and exports Settings update parsing.
- [x] `server/delegates.py` no longer owns `settings.update` parser internals.
- [x] Existing Settings RPC/storage behavior is covered by passing tests.
- [x] `.vorch/specs/settings.md` exists and `.vorch/PROJECT.md` references it.
- [x] Relevant backend/frontend quality gates pass.
- [x] WebUI smoke test includes Settings UI and a real agent Run with tool calls.
- [x] Changes are committed.