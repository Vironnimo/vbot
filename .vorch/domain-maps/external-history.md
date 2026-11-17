# External history

`core/external_history/` reads other coding agents' saved conversations and Memory documents for vBot import and read-only lookup.

## Overview

The source readers interpret Codex, Claude Code, OpenCode, Hermes and OpenClaw formats without launching or resuming those applications. The importer and the bundled `external-history` Skill use the same RPC reads. Source discovery and interpretation belong here; canonical conversation persistence remains in Sessions, Memory mutations and source-document retention in Memory, and provider request construction in Chat. There is no foreign Tool execution adapter.

User decisions (2026-11-12): import the full available conversation for continuation, retain foreign Tool names/arguments/results as historical data, identify imported entries in the Timeline, and add one System Reminder at the first vBot continuation. The Skill is for vBot Agents reading external history independently of import. Memory is imported separately into a selected Agent/User/Project scope.

## Interfaces

- `ExternalHistory` exposes source discovery, Session listing, paged reads/search, complete Session reads, paged Memory reads and complete single-document reads. Paths are on the server. Directory discovery visits known history/Memory roots and direct export files; it does not recursively inspect plugin caches or arbitrary repositories. Custom directories and exports can be selected explicitly.
- `server/rpc/external_history_methods.py` runs readers on a bounded worker pool. It delegates Session creation to the existing admission/resolution path, with initial messages and import metadata in one Session-store transaction. The same source id and content digest within the same live owner/Working Project reuses a direct import destination. An inherited import in a fork is ineligible; a changed snapshot creates another Session.
- `memory.import` follows the existing serialized Memory owner boundary. It adds reviewed facts as one revision, deduplicates against existing entries and refuses an over-budget selection without truncation. Before committing facts it saves the original UTF-8 bytes (including a BOM and source line endings) under the owner's `memory-sources/<source>/<content-hash>.md`; failure to save the copy returns a Memory error and leaves the facts unchanged. A later Memory-file write failure can leave the recoverable copy. The original source stays at its location and the copy is outside pinned prompt files.
- The CLI area is `external`; `sources`, `list`, `show` and `memories` are reads, `import-session` and `import-memory` are explicit mutations. The Chat header opens the WebUI importer for the selected Agent. Session import opens the destination but starts no Run.

## Data model and context boundary

An imported entry becomes a canonical `user` or `assistant` ChatMessage with `external_history` provenance (source, source Session id, ordinal, original role/kind and raw record). Foreign tool activity and system/developer messages are labeled ordinary historical text, with no native ToolCall, executable call id, Tool grant, System role, Usage or reasoning replay payload. An imported Assistant entry can have no vBot Model identity. The nullable `entry_text.external_history_json` column persists the original record and survives normal lineage/copy operations.

Chat strips provenance from provider requests; only the readable history projection enters Context. The Timeline emits `external_history` items and omits the raw payload from that display projection. The UI marks their source and collapses historical activity. Attachments are retained as source references or serialized content blocks; referenced files and processes are not copied or resumed.

`core/chat/imported_history.py` determines whether the imported lineage already contains the transition note. RunExecution persists it under the Session write lock before the first Model request, including when Accessor admission already persisted the user's input. Repeated Runs retain the same note; forks before continuation need it, forks after continuation inherit it. Normal Compaction can summarize this historical transition; the original records remain in canonical history.

## Source formats and limits

JSONL readers cover Codex rollout response items (event-message fallback only when response items are absent), Claude conversation envelopes and OpenClaw transcript events. JSON exports cover role/content conversations and both OpenCode wrapped info/parts and typed messages. SQLite readers open with `mode=ro`, `query_only` and a read transaction: Hermes sessions/messages with active/compacted display selection and generation order; OpenCode session/message/part and session_v2/session_message/session_part; OpenClaw session_windows/transcript_events with its active-event projection.

Unrecognized schemas, malformed transcripts, Hermes cross-Session compression lineage or compacted rows without display order, binary Hermes message content, OpenCode database forks and missing OpenClaw active-path events or payloads require a complete native export instead of a silent partial import. File reads cap at 256 MiB. The lookup API pages content in bounded Unicode character slices as well as entry pages, preserving the continuation cursor inside large Tool Results. Directory lists report unreadable files as warnings alongside readable results. A Session id looked up by directory must match exactly one readable candidate; it carries any scan warnings. Linear JSONL logs retain file order. OpenClaw JSONL with branch navigation, side appends or inherited paths requires a selected-path export or the SQLite active-event index. Hermes reset/fork children do not inherit parent turns; withdrawn rows and model-only carriers are excluded, archived Tool Results survive shortened copies. Import digests exclude derived timestamps; genuine source timestamps remain in raw records.

Source-format evidence: [Claude Sessions](https://code.claude.com/docs/en/sessions), [Codex app server](https://learn.chatgpt.com/docs/app-server), [OpenCode source](https://github.com/anomalyco/opencode), [Hermes source](https://github.com/NousResearch/hermes-agent), [OpenClaw source](https://github.com/openclaw/openclaw). These are evolving external formats, not vBot's persisted-format compatibility contract.

SQLite projections name the columns discovered in the source snapshot, including unknown and generated visible columns, so raw provenance keeps source fields without depending on column positions.

## Agent-facing text

This domain owns the bundled reading Skill, CLI import/lookup help and output, historical-entry labels and the import-transition System Reminder. Before changing these texts, read `external-history/agent-facing-text.md` and update the affected sentence reasons.

## References

- Changing import/lookup guidance or the environment-transition reminder -> `external-history/agent-facing-text.md`
- Changing persisted imported messages, lineage or creation atomicity -> `sessions.md`
- Changing provider projection or System Reminder delivery -> `chat.md`, `model-communication.md`
- Changing selected facts, budgets, revisions or retained Memory source documents -> `memory.md`
- Changing CLI output or the importer UI -> `cli.md`, `webui.md`

Contract tests live in `tests/core/external_history/`, the Session lifecycle and Chat request/Timeline suites, `tests/server/rpc/test_external_history_methods.py`, `tests/cli/test_cli_external.py` and the WebUI `ExternalHistoryDialog` tests.
