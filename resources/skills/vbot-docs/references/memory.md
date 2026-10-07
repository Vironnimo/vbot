# Memory

Pinned Memory lives in one Identity Agent's Workspace: agent-scope entries in `MEMORY.md`, user-scope entries in `USER.md`. These commands manage any agent's pinned Memory through the server RPC contract — use them for curation across agents; a Run can use its own `memory` Tool when available. CLI reads do not depend on that Tool being enabled.

```bash
vbot memory list <agent-id>
vbot memory add <agent-id> [--scope agent|user] (--content <text> | --file <path>)
vbot memory replace <agent-id> [--scope agent|user] <entry-id> (--content <text> | --file <path>)
vbot memory remove <agent-id> [--scope agent|user] <entry-id> --yes
vbot memory history <agent-id> [--scope agent|user] [--limit <n>]
vbot memory show <agent-id> <revision>
vbot memory diff <agent-id> <from-revision> [<to-revision>]
vbot memory revert <agent-id> <revision>...
```

- For writes, `--scope` defaults to `agent`; `list` always returns both scopes. User-scope facts are durable user preferences; agent-scope facts are stable environment/convention notes.
- `list` prints both scopes with entry ids; use those ids for `replace` and `remove`.
- Every mutation prints the affected entry and the remaining per-scope counts — that output is the verification result.
- Entries are curated durable facts, not session history or scratch notes. Keep them short and declarative; replacing an entry is preferred over accumulating overlapping ones.
- `remove` requires `--yes`.

## History and restoring

Every change to an Agent's Memory is kept as a numbered revision: changes by the `memory` Tool (with the Session and Run that made them), changes by these commands, and edits made to `MEMORY.md` or `USER.md` any other way, which are recorded the next time Memory is used. The first revision holds the entries that existed when the history started.

- `history` lists revisions newest first with their UTC time, scope, who made the change and each changed entry: `+` added, `-` removed, `~` old text followed by `->` new text. Raise `--limit` for older revisions.
- `show` prints both scopes as they were after one revision. `diff` shows what changed from one revision to another, or to the current entries when the second revision is omitted.
- To restore removed or changed entries, `revert` the revisions that made those changes. Only their changes are taken back; everything changed since stays. Name several revisions to take them back together.
- A revert changes all named revisions or nothing. When a later revision changed the same entry again, nothing changes and the output names that later revision: revert it together with the first one, or set the entry directly with `add` or `replace`.
- A revert is itself a revision and can be reverted. The first revision cannot be reverted. The revert output lists the entries it changed and the resulting counts.
