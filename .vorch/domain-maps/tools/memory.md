# Memory Tool

Model-facing editing of pinned Memory entries in `USER.md` and `MEMORY.md` (storage contract: `memory.md`).

## Interfaces

- Tool name `memory`; `register_memory_tool(registry, memory_service)` in `core/tools/memory.py`, bound to the Runtime's `MemoryService`. Activation follows the Agent's Memory mode (`activation="memory_mode"`, Identity Agents only).
- Model-facing schema: one flat object with required `action` (`list`, `add`, `replace`, `remove`) and optional `scope` (`user`, `agent`), `content`, `old_text`; no `additionalProperties` keyword or defaults.
- Entries are addressed by text: `old_text` is a unique part of an entry's current text. Matching (`core/memory/memory.py::match_memory_entries`) ignores whitespace runs and a copied leading `- ` bullet; an exact whole entry wins, then exact containment; only if neither exists are case, typographic quotes/dashes and Unicode compatibility forms ignored the same way. Loose whole matches of different entries are ambiguous. The backend rematches under its file lock (`replace_matching`/`remove_matching`), so a concurrent change cannot redirect a write.
- `scope` is required for `add` (never guessed). `list` without scope shows both scopes; `replace`/`remove` without scope use the one scope whose entries match `old_text` and fail when both or neither match.
- `entry_id` is an unadvertised parameter: positional ids copied from older results are refused with the entry currently at that position and the corrected `old_text` call, unless `old_text` names the same entry. The RPC surface still uses positional ids (`memory.md`).
- `parallel_safe=False`: same-turn memory calls apply in written order, so "free space, then add" works in one message and text matching never races a sibling change.

## Argument Repair

The Tool-owned normalizer (`normalize_call_arguments`) repairs action/scope formatting and wrappers (`request`, action-named objects, `operation`), field aliases (`target` -> scope; `new_text`/`new_content`/`new_string`/`text` -> content; `old_string`/`old_content`/`old`/`match`/`find` -> old_text; `id`/`index` -> entry_id), action synonyms (create/save/append... -> add, update/edit/modify/change -> replace, delete/forget/drop/erase -> remove, read/show/view/get -> list) and scope values (`memory`/`MEMORY.md` -> agent, `profile`/`USER.md` -> user). Conflicting spellings fail before any write. `remove` with only `content` treats it as the entry text.

## Result Contract

- Every success carries a `content` line (declared result schema). `list` renders exactly the Memory block format (`# Agent Memory (used/limit chars used)` / `# User Profile ...` plus bullets or `No entries yet.`) and a `count` that drives the Tool row's presentation-only result count. Mutations answer with one line: scope, usage after the change and, for replace/remove, the previous entry text (preview <= 200 chars). No mutation echoes the entry list.
- Duplicate adds and no-op replaces succeed with "nothing changed".
- Failures change nothing and are never marked retryable: `memory_no_match` shows the searched scope(s) in block format; `memory_ambiguous` lists the matching entries (per scope when both match); `memory_full` states the resulting total, the characters to free and the current entries; `invalid_arguments` refusals (missing scope for add, replace without content, remove with content, add with old_text/entry_id, entry_id) name the corrected call as JSON. Other Memory validation or I/O failures return `memory_error`.
- Thrash guard: the 4th consecutive failed mutation in a Run returns a terminal `memory_error` ("stop retrying", `retryable: false`, `attempts_made`); argument refusals do not count, a success resets the streak (`memory.md` -> Constraints).

## Constraints & Gotchas

- The whole file is Tool-managed bullets; there is no freeform zone to preserve (`memory.md` -> Storage contract).
- Writing policy (what deserves Memory) lives in the `memory:guidance` block; the Tool description only states mechanics and the two scopes.
- Tests: `tests/core/tools/test_memory.py` (production executor), `tests/core/memory/test_memory.py`; probe cases in `scripts/tool_lab/cases/knowledge.json`.
