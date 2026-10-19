# Memory

Pinned Memory lives in one Identity Agent's Workspace: agent-scope entries in `MEMORY.md`, user-scope entries in `USER.md`. These commands manage any Agent's Memory; for your own, the `memory` Tool does the same when you have it.

```bash
vbot memory list <agent-id> [--scope agent|user]
vbot memory add <agent-id> [--scope agent|user] (--content <text> | --file <path>)
vbot memory replace <agent-id> [--scope agent|user] <entry-id> (--content <text> | --file <path>)
vbot memory remove <agent-id> [--scope agent|user] <entry-id> --yes
vbot memory history <agent-id> [--scope agent|user] [--limit <n>]
vbot memory show <agent-id> <revision>
vbot memory diff <agent-id> <from-revision> [<to-revision>]
vbot memory revert <agent-id> <revision>...
```

- For writes, `--scope` defaults to `agent`; `list` shows both scopes unless `--scope` names one. User-scope entries hold durable facts about the user, agent-scope entries stable facts about the environment and conventions.
- `list` prints entry ids; use them for `replace` and `remove`. `remove` requires `--yes`.
- Every change prints the affected entry and the remaining count per scope; that output confirms it.
- Write short, declarative facts, not Session history or scratch notes. Replace an entry instead of adding one that overlaps it.

## History and restoring

Every change to an Agent's Memory is kept as a numbered revision: changes by the `memory` Tool (with the Session and Run that made them), changes by these commands, and edits made to `MEMORY.md` or `USER.md` any other way, which are recorded the next time Memory is used. The first revision holds the entries that existed when the history started.

- `history` lists revisions newest first with their UTC time, scope, who made the change and each changed entry: `+` added, `-` removed, `~` old text followed by `->` new text. Raise `--limit` for older revisions.
- `show` prints both scopes as they were after one revision. `diff` shows what changed from one revision to another, or to the current entries when the second revision is omitted.
- To restore removed or changed entries, `revert` the revisions that made those changes. Only their changes are taken back; everything changed since stays. Name several revisions to take them back together.
- A revert changes all named revisions or nothing. When a later revision changed the same entry again, nothing changes and the output names that later revision: revert it together with the first one, or set the entry directly with `add` or `replace`.
- A revert is itself a revision and can be reverted. The first revision cannot be reverted. The revert output lists the entries it changed and the resulting counts.
