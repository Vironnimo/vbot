# Skills

`skill list` shows the effective loadable catalog and invalid-Skill diagnostics. Mutation commands write only vBot-owned editable scopes: `global` (`<data-dir>/skills`) or `agent:<identity-agent-id>` (that Identity Agent's private Skill home). Project Skills remain repository-owned and bundled Skills remain read-only.

```bash
vbot skill list
vbot skill read --scope global|agent:<id>
vbot skill create <name> --scope <scope> (--content <skill-md> | --file <path>) [--source <label>]
vbot skill update <name> --scope <scope> (--content <skill-md> | --file <path>) [--source <label>]
vbot skill delete <name> --scope <scope> --yes
vbot skill write-file <name> <relative-path> --scope <scope> (--content <text> | --file <path>)
vbot skill remove-file <name> <relative-path> --scope <scope> --yes
```

- `read` returns each editable Skill's complete `SKILL.md`, not the layered effective catalog.
- `create` and `update` validate the full `SKILL.md` through the shared Skill authoring service and apply the change live. Prefer `--file` for multiline content.
- `write-file` and `remove-file` manage supporting files such as `references/schema.md`; paths are relative to the named Skill and traversal is rejected server-side.
- `delete` and `remove-file` require `--yes`. Their operations are destructive within the editable scope, though deleting an Identity Agent later archives its complete private Skill home with the Agent.
- Mutation output includes the normalized Skill name, operation, scope, and validation warnings. Run `skill read --scope ...` to verify content and `skill list` to verify effective availability.
