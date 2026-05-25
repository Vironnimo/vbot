# Compaction Domain Extraction Plan

## Goal

Extract compaction/context-management code into a dedicated domain while preserving behavior.

## Scope

In:
- Move compaction service, settings type, errors, and strategy code out of `core/chat/` into `core/compaction/`.
- Keep public behavior of automatic compaction, manual `/compact`, compaction settings, and `compaction_completed` events unchanged.
- Add/update domain documentation in `.vorch/specs/` and the project spec index.
- Update imports and tests to the new domain path.

Out:
- No behavior changes to compaction heuristics, summary prompt shape, or token thresholds.
- No SQLite/session storage work.
- No run/queue refactor.
- No Settings-domain refactor beyond imports/docs needed for compaction.

## Hidden Constraints

- Sessions are append-only JSONL; compaction appends `compaction_checkpoint` messages and must not rewrite history.
- Compaction must not split open tool cycles; current chat-loop invocation points are safe boundaries.
- Manual `/compact` and automatic compaction both support `summary_model` fallback to the active model when invalid/unavailable.
- `compaction_completed` is a visible Run event consumed by WebUI timeline rendering.
- `compaction.md` is allowed for backend loading but is not part of the normal prompt editor surface.

## Risks

- Import cycles: current compaction imports `ChatMessage` from `core.chat.chat`; moving it may expose existing coupling.
- Manual and automatic compaction have similar summary-adapter resolution but not identical call sites.
- Tests may still import from the old `core.chat.compaction` path.

## Done When

- [x] Handoff file is replaced with current domain candidates.
- [x] `core/compaction/` exists and exports the compaction API.
- [x] Chat loop and server manual compact path import from `core.compaction`.
- [x] Tests import the new path and pass.
- [x] `.vorch/specs/compaction.md` exists and `.vorch/PROJECT.md` references it.
- [x] Relevant backend quality gates pass.
- [x] Changes are committed.
