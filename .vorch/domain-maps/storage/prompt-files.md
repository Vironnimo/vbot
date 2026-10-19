# Prompt files

How Storage reads prompt fragments and persists System Prompt block layouts and overrides (`core/storage/prompt_fragments.py`, `core/storage/prompt_blocks.py`). Composition and the override cascade belong to the prompts domain (`../prompts.md`).

## Prompt fragments

Bundled fragments live in `resources/prompts/`. `PROMPT_FRAGMENT_NAMES` lists every readable fragment and `AGENT_PROMPT_FRAGMENT_NAMES` the editable block defaults. Backend-only one-shot briefs (`compaction*.md`, `handoff.md`) and the learning-Run and Librarian brief fragments (composed by `core/prompts/briefs.py`, whose `BRIEF_FRAGMENT_NAMES` must all be in `PROMPT_FRAGMENT_NAMES`) are readable for internal Runs but never editable or copied to agent scopes; the backend-only briefs are also invisible to the System Prompt UI. Nothing seeds default-scope copies anymore; a hand-created data-dir copy overrides bundled at read time, per fragment name (retired fragment names such as `reflect.md` or `reflect-skill-method.md` are no longer read).

- Default fragments read bundled resources unless a hand-created data-dir copy exists - such stale copies shadow bundled updates and should be deleted. Only a copy that does not exist falls back to the bundled resource; one that exists but cannot be read raises `StorageError`. Invalid UTF-8 in any fragment (user copy, bundled resource or Agent scope) likewise raises `StorageError` naming the fragment, never an empty value or a fallback.
- Agent-scope prompt seeding copies the current effective content of `AGENT_PROMPT_FRAGMENT_NAMES` once when custom prompts activate; missing agent-scope fragments read as `""` and assemble only under the enabled flag. A fragment that cannot be checked or read raises `StorageError`: seeding never writes over it and a read never takes it for empty.

## Block persistence

Each scope persists ordered `layout.json` plus thin text overrides under `blocks/<namespace>/<slug>.md` (default scope under `<data_dir>/prompts/`, agent scope under the agent's prompts dir). Block ids map to paths with **the colon never reaching disk** (Windows-safe); namespaces are a closed set; slugs validate with the agent-id rule before any path construction (the single-writer rule: `../storage.md` -> Prompt files).

An override file belongs to the id whose slug spells its file name exactly (`has_id_entry`): on a case-insensitive filesystem a case variant such as `notes` beside a stored `Notes.md` reads as absent, its write raises `StorageError`, and its removal leaves the file alone; where both spellings can coexist they keep separate files. A missing override reads as absent (cascade falls through); one whose presence cannot be checked raises `StorageError` instead of reading as absent or being replaced; dynamic blocks have no override path. The override cascade composes in the prompts domain from these per-scope reads.

`layout.json` is `{"format_version": 1, "entries": [...]}` (`../json-documents.md`; unknown fields survive writes). Missing layouts return `[]`; unreadable, invalid UTF-8/JSON, unversioned or other-version, or invalid-entry layouts warn and return `[]`, and `write_layout`/`prune_layout` refuse to overwrite them (`StorageError`) unless called with `reset=True` (the Prompts layout reset). Validation remains strict for entry types and unsafe scope ids; Prompts interprets an empty layout as no custom layout (`prompt_blocks.py`, `test_prompt_blocks.py`).

Block layout writes are inert-tolerant (pruning contributor-gone ids is normal, never an error), and seeding preserves existing layouts without copying text overrides.
