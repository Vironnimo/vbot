# Import external Sessions and Memory

Use this reference when the user asks to bring saved work from Codex, Claude Code, OpenCode, Hermes or OpenClaw into vBot. For looking up their history without importing it, load `external-history`.

The source paths belong to the vBot server. Commands work from any directory; use the exact source id and path from the listing, and an Agent id from `vbot agent list` or the Project's Team.

## Continue a Session

Replace `<source>` with `codex`, `claude-code`, `opencode`, `hermes` or `openclaw`, and the remaining placeholders with the selected source and destination:

```bash
vbot external list <source>
vbot external import-session <source> <source-session-id> --path "<source-path>" --agent <agent>
```

For an Identity Agent, `--working-project <project-id>` selects the repository the continued Session works in; omission uses its default. A Project Agent is addressed as `agent@project` and works in that Project.

Import preserves the complete available conversation and source records. Historical Tools become labeled text, with their names, arguments and results; vBot supplies current Tools and inserts an environment-change System Reminder at the first continuation. Imported history is marked in the Timeline, and long histories use normal Compaction. Attachments remain source references or serialized source blocks; the import does not copy referenced files or running processes.

The returned Session id is the destination. Repeating the same source snapshot for the same Agent and Working Project returns that Session. A changed source snapshot creates a separate Session. Import starts no Run; to send a user-requested continuation, use `vbot chat --agent <agent> --session <imported-session-id> "<message>"`, replacing the message with the user's requested work. To undo the import, archive its destination with `vbot session delete <agent> <imported-session-id>`.

## Import Memory facts

```bash
vbot external memories <source>
vbot external import-memory <source> --path "<memory-file>" --agent <agent> --scope user --entries-file "<reviewed-facts-file>"
```

Write the reviewed facts into the entries file, separated by blank lines, as UTF-8 or UTF-16 with a BOM. `--scope agent` stores Agent facts; `--project <project-id> --scope project` selects shared Project Memory instead of `--agent`. Omitting `--entries-file` uses the source paragraphs unchanged.

The import retains the complete original document in the destination's `memory-sources` directory and adds the selected facts in one Memory revision. Existing entries stay; identical facts are skipped. If the selection exceeds the Memory budget, nothing is added to pinned Memory: select fewer facts without discarding the original document. Memory history and revert use the usual commands in the Memory reference.
