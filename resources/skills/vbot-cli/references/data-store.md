# Data-store maintenance

The canonical databases live in the data directory, for example `<data-dir>/sessions.db`; `<data-dir>/data-store.json` authorizes them and lists each one's identity. Use this area only for diagnostics and current-format maintenance.

```bash
vbot data-store status
vbot data-store snapshot list
vbot data-store snapshot create --reason manual
vbot data-store snapshot verify <snapshot-id>
vbot data-store incident acknowledge <incident-id>
vbot data-store snapshot restore <snapshot-id> --yes
vbot data-store snapshot restore <snapshot-id> --database sessions --yes
```

`status` is the first check after startup or recovery. It reports one overall state (`healthy`, `snapshot_degraded`, `degraded`, `recovered_with_incident`, `unavailable` or `maintenance`), each registered database, the verified data snapshots and every unacknowledged recovery incident, using only safe operational metadata. When a loopback target is unreachable, it reports explicitly local stopped-server diagnostics. `snapshot create` is a deliberate backup of every registered database through the running server. `snapshot list`, `verify` and `restore` use the local data directory: run them on the server machine with a loopback target and the matching `--data-dir`. A remote connection failure never falls back to reading local state. A recovery incident names the restored database and remains visible until the exact incident is acknowledged; acknowledgement does not delete snapshots or quarantine evidence. `snapshot restore` is offline maintenance and requires `--yes`. It checks the snapshot first, stops and verifies the exact vBot target when necessary, restores every database in the snapshot or only those named with `--database`, verifies them, and restarts a server that was running before the restore. An interrupted restore keeps the server from starting until a restore is repeated and completes. Rehearse restore on a copy before selecting any real instance.
