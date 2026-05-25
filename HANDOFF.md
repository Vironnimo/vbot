# Handoff

## Current Architecture Candidates

The old handoff content was stale. This file now tracks the current candidates for future domain extraction work.

### 1. Compaction / Context Management

Strongest next candidate. Compaction already spans a service/strategy, chat-loop control flow, manual `/compact` command handling, persisted settings, bundled prompt fragments, Run events, and WebUI settings/timeline rendering.

Candidate shape: `core/compaction/` with a dedicated `.vorch/specs/compaction.md` spec.

Why it matters:
- Compaction has product-specific semantics, not just chat helper code.
- It must preserve Session append-only history, safe tool-cycle boundaries, summary model resolution, and visible transparency events.
- Current summary-adapter resolution exists both in the chat loop and server manual compact path.

### 2. Runs / Queue / Lifecycle

Large candidate. `ChatRunManager` owns active Runs, Run events, cancellation, replay/subscription, and the busy-session FIFO queue. Automation, subagents, server RPC, channels, and WebUI queue state all depend on it.

Candidate shape: `core/runs/` or a stricter run/queue subdomain split out of `core/chat/`.

Why it matters:
- Run lifecycle is broader than the provider-facing chat loop.
- Cancellation, SSE replay, queue draining, and UI queue projection are tightly coupled today.
- This is higher blast radius than compaction and should come after a focused smaller extraction.

### 3. Settings / Configuration

Medium-strong candidate. Settings are currently partly storage, partly server schema/validation, partly runtime side effects, and partly WebUI normalization.

Candidate shape: `core/settings/` owning schemas, defaults, validation, update services, and normalized public payloads.

Why it matters:
- `StorageManager` is carrying product settings behavior in addition to file I/O.
- Settings updates can trigger runtime side effects such as skill reloads.
- Frontend and backend duplicate some default/normalization knowledge.

### 4. Prompts / System Prompt

Medium candidate. Prompt assembly lives under Agents, prompt fragment file access under Storage, prompt RPC in Server delegates, and the editor in WebUI.

Candidate shape: `core/prompts/` owning fragment manifests, variables, read/write/reset, preview assembly, and prompt-specific storage rules.

Why it matters:
- Prompt editing is product surface area now, not just storage plumbing.
- Compaction also uses prompt fragments, so a later prompt-domain split may become useful after compaction is isolated.

### 5. Logs

Small/mechanical candidate. The subsystem already has a spec, but backend code lives under `core/utils/` even though it is a product-visible Logs subsystem.

Candidate shape: `core/logs/` moving `LogViewer` and log parsing/watching contracts out of utilities.

Why it matters:
- The boundary is already clean and read-only.
- Low urgency unless logs gain write/actions/retention settings.

## Current Priority

Start with Compaction / Context Management.
