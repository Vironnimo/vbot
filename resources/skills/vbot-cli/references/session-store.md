# Session-store maintenance

The canonical Session store is `<data-dir>/sessions.db`, authorized by `<data-dir>/session-store.json`. Use this area only for diagnostics and current-format maintenance; it never converts legacy JSONL automatically.

```bash
vbot session-store status
vbot session-store snapshot list
vbot session-store snapshot create --reason manual
vbot session-store snapshot verify <snapshot-id>
vbot session-store incident acknowledge <incident-id>
vbot session-store snapshot restore <snapshot-id> --yes
```

`status` is the first check after startup or recovery and reports only safe operational metadata. `snapshot create` is a deliberate current-format backup. A recovery incident remains visible until the exact incident is acknowledged; acknowledgement does not delete snapshots or quarantine evidence. `snapshot restore` is offline maintenance, requires `--yes`, proves the exact target server is stopped, and must be rehearsed on a copy before any real instance is selected.

For legacy data, use `python scripts/converters/session_sqlite.py inventory|dry-run|convert|verify|install|resume|export-jsonl` on copied data first. The converter is the only supported JSONL reader, and `install`/real-data conversion require a separate operator decision.
