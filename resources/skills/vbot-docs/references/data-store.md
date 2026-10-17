# Data-store maintenance

The canonical databases live in the data directory, for example `<data-dir>/sessions.db`; `<data-dir>/data-store.json` authorizes them and lists each one's identity. Use this area for named credentials (Credentials below), diagnostics and current-format maintenance.

```bash
vbot data-store status
vbot data-store snapshot list
vbot data-store snapshot create --reason manual
vbot data-store snapshot verify <snapshot-id>
vbot data-store incident acknowledge <incident-id>
vbot data-store snapshot restore <snapshot-id> --yes
vbot data-store snapshot restore <snapshot-id> --database sessions --yes
vbot data-store snapshot restore <snapshot-id> --documents --yes
vbot data-store snapshot restore <snapshot-id> --all --yes
vbot data-store config-backup list
vbot data-store config-backup show <backup-id>
vbot data-store config-backup restore <backup-id> (--file <path>... | --all) --yes
vbot data-store unregister ext.<extension-id>.<name> --yes
```

`status` is the first check after startup or recovery. It reports one overall state (`healthy`, `snapshot_degraded`, `degraded`, `recovered_with_incident`, `unavailable` or `maintenance`), each registered database, the registered data snapshots and every unacknowledged recovery incident, using only safe operational metadata. For Session search, ordinary `status` reports availability and the last coverage check made in the current server process, with its time; without a recorded check, coverage is unknown. To check the search indexes against stored entries now, run `vbot data-store status --verify-search`; it can take longer on large histories. When a loopback target is unreachable, status reports explicitly local stopped-server diagnostics; `--verify-search` also works there without changing the databases. `snapshot create` is a deliberate backup through the running server of every registered database and of the JSON documents (settings, Agent, Project, Channel, prompt layout, job, Calendar, Skill policy, Terminal and MCP connection files, and the encrypted Secret Store `secrets.json`). `snapshot list`, `verify`, `restore` and every `config-backup` command use the local data directory: run them on the server machine with a loopback target and the matching `--data-dir`. A remote connection failure never falls back to reading local state. A recovery incident names the restored database and remains visible until the exact incident is acknowledged; acknowledgement does not delete snapshots or quarantine evidence. A registered database whose file is missing makes `snapshot create` and updates fail; the message and `status` name each such database and how to resolve it. `unregister` releases the database of an Extension that was removed: the database files move to the quarantine folder named in the result and the database is no longer registered, so snapshots and updates can proceed. It accepts only Extension databases (`ext.<extension-id>.<name>`), is refused while an Extension has the database open, and requires `--yes`. Earlier data snapshots keep their copy, and restoring that database from one registers it again. Run it only when the user asks, because the Extension's data then remains only in quarantine and in earlier snapshots. `snapshot restore` is offline maintenance and requires `--yes`. It checks the snapshot first and stops and verifies the exact vBot target when necessary. Without a selector it restores every database in the snapshot; `--database` restores only the named ones; `--documents` restores the JSON documents as one set, alone or together with `--database`; `--all` restores the complete snapshot (every database and the JSON documents), moves databases created after the snapshot aside, and cannot be combined with another selector. Restored JSON documents become exactly the snapshot's: documents created after the snapshot are removed, and every replaced or removed document is kept in the quarantine folder named in the result. `secrets.json` is the exception: a snapshot restore keeps the current one, so credentials saved since the snapshot stay. The command verifies the restored databases and restarts a server that was running before the restore. An interrupted restore keeps the server from starting until a restore is repeated and completes. Rehearse restore on a copy before selecting any real instance.

`config-backup list` shows the automatic backups of the configuration files (settings, Agent, Project, Channel and job files, the Secret Store, `SOUL.md`, `USER.md`, `MEMORY.md` and prompt overrides), newest first, with the files each one changed; `show <backup-id>` compares each file with the current one. `config-backup restore` requires `--yes` and either `--file <path>` (repeatable, as `show` names it) or `--all`. It stops and restarts the target like `snapshot restore`, first backs up the state it replaces as `before restore <backup-id>`, and never deletes a file. `--all` keeps the current `secrets.json`; `--file secrets.json` restores the Secret Store and drops the credentials saved since that backup, so name it only when the user asks for exactly that. Restore a backup only when the user asks.

## Credentials

Credentials live encrypted in the data directory's Secret Store, `secrets.json`; a variable of the same name in the server's process environment takes precedence. vBot ignores a `.env` file and `oauth/` token files in the data directory until `secrets convert` imports them. Set Provider keys with `provider key set`, Channel tokens with `channel token set` and Extension secrets with `extensions set <name> <field> --stdin`; set any other named credential, such as a token a Skill requires, here:

```bash
vbot data-store secrets list
vbot data-store secrets set <NAME>
vbot data-store secrets unset <NAME>
```

`list` shows each stored name, whether it is configured and whether the server takes its value from `process_environment` or `data_dir`, never a value. `set` reads the value from stdin, or from a hidden prompt in a terminal: pipe in a value the user gave you, and never put it in an argument or a file. When the user has not given you the value, give them `vbot data-store secrets set <NAME>` to run in their terminal instead of asking for it in the conversation. `unset` removes the stored value; a process-environment variable of that name still applies. With a running server these apply at once; with a stopped local server they change the store directly for the next start.

```bash
vbot data-store secrets status
vbot data-store secrets convert --yes
vbot data-store secrets reset --yes
```

These read the local data directory: run them on the server machine with a loopback target and the matching `--data-dir`. `status` shows the store's state, damaged entries, where the key is searched and the ignored plaintext credential files with what to do about each, never a value. `convert` stops a running server, imports `.env` and `oauth/*.json` into the store, deletes the files it fully imported and starts the server again. `reset` is destructive: for a store whose key is lost for good, it moves the store to quarantine and starts an empty one, so every credential and OAuth login must be entered again. Run `reset` only when the user asks.
