# Memory

Pinned Memory lives in one Identity Agent's Workspace: agent-scope entries in `MEMORY.md`, user-scope entries in `USER.md`. These commands manage any agent's pinned Memory through the server RPC contract — use them for curation across agents; a Run can use its own `memory` Tool when available. CLI reads do not depend on that Tool being enabled.

```bash
vbot memory list <agent-id>
vbot memory add <agent-id> [--scope agent|user] (--content <text> | --file <path>)
vbot memory replace <agent-id> [--scope agent|user] <entry-id> (--content <text> | --file <path>)
vbot memory remove <agent-id> [--scope agent|user] <entry-id> --yes
```

- For writes, `--scope` defaults to `agent`; `list` always returns both scopes. User-scope facts are durable user preferences; agent-scope facts are stable environment/convention notes.
- `list` prints both scopes with entry ids; use those ids for `replace` and `remove`.
- Every mutation prints the affected entry and the remaining per-scope counts — that output is the verification result.
- Entries are curated durable facts, not session history or scratch notes. Keep them short and declarative; replacing an entry is preferred over accumulating overlapping ones.
- `remove` requires `--yes`.
