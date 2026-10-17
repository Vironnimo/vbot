# Secrets

The Secret Store: credentials and OAuth tokens of a data directory, encrypted at rest in `<data-dir>/secrets.json`, plus the conversion of the plaintext credential files it replaced.

## Overview

`core/secrets/` owns the store document, the protection of its data key, and the one-time conversion of `.env` and `oauth/*.json`. It is a kernel module of its own rather than part of `core/storage/` because it carries its own persisted format, cryptography, per-system key protection and operator commands, and because consumers outside Storage build on it directly: the Provider `TokenStore`, `doctor` validation and the `vbot data-store secrets` commands work without a `StorageManager`. Storage keeps the credential API that every other domain uses; the database kernel never imports it (it only receives the health projection).

- Owns: `secrets.json` (format, reads, writes, reset), `secrets.lock`, key creation and lookup, entry encryption, credential-name folding, plaintext import/retirement, the health projection.
- Does not own: which names a domain stores (environment-variable names chosen by Providers, Extensions, Channels, web Tools; token ids chosen by `core/providers/token_store.py`), process-environment precedence (`runtime.md` -> credential resolution), the `secrets.*` RPC handlers (`server/rpc/secret_methods.py`, contract below), or the WebUI.

Consumers: `StorageManager.secrets` is the one store instance per data directory and process; `load_data_dir_credentials`/`set_data_dir_credential`/`remove_data_dir_credential` use the `credentials` section (`storage.md`), and `TokenStore(storage.secrets)` uses `oauth_tokens` (`providers/connections.md`). Runtime credential resolution, `ProviderCredentialResolver`, Skill `env` requirements, Extension `set_secret`, MCP credential writes and the `secrets.*` RPCs all reach the store through those Storage functions. Process environment still wins over a stored value, and names stay environment-variable names.

## Terms

### Secret Store
`<data-dir>/secrets.json` plus its data key. Not the OS keychain: on Windows only the key is wrapped by DPAPI; the entries live in the data directory.

### Data key
One random 256-bit AES key per store, identified by a key id. Created with the store, never rotated in place; `reset` starts a new store with a new key.

### Damaged entry
One entry that does not authenticate under the store's key, name and section. It reads as missing, is reported, and stays untouched until replaced or removed.

### Import record
The `plaintext_import` field: which plaintext files a conversion imported and the keyed fingerprint of every value it put into the store. It decides which store values the conversion may still replace or remove (Conversion of plaintext credentials).

## Data Model

`secrets.json` is a Generation 1 JSON document (`SECRET_STORE_FORMAT`, written by `write_json_document` with mode 0o600, sorted keys, unknown fields preserved):

- `store_id`: random 128-bit hex id.
- `key`: `{id, protection, check, wrapped?}`. `protection` is `dpapi` or `key_file`; `check` is a sealed constant that proves a key belongs to this store; `wrapped` is the DPAPI-wrapped data key (DPAPI only).
- `credentials`: environment-variable name -> sealed value.
- `oauth_tokens`: token id (`<provider>-<connection>[--<account>]`) -> sealed token document text (the OAuth token JSON format stays owned by `token_store.py`).
- `plaintext_import` (only while a conversion is unfinished): `{imported_at, sources: {relative path: {fingerprint, entries: {name: value fingerprint}}}, backups_pending?}`. Fingerprints are HMACs under the data key and reveal nothing without it. The first record format (`sources: {path: fingerprint}` plus `credentials`/`oauth_tokens` name lists) is still read; its entries count as not put there by the import, so a conversion never replaces or removes them.

A sealed entry is `base64(nonce || ciphertext || tag)`, AES-256-GCM with a fresh 96-bit nonce per write; the associated data is `vbot-secret-store/1`, store id, key id, section and name, so a ciphertext copied to another name, section, store or key does not open (`_crypto.py`). Entry names match `ENTRY_NAME_PATTERN`. Values are UTF-8 strings.

`<data-dir>/secrets.lock` is the inter-process write lock (Conventions); it holds no data.

The document is in `DURABLE_DOCUMENTS` (`"secrets"`, validated by `validate_secret_store_file` in `doctor config`, which also reports an unopenable key and damaged entries), in `SNAPSHOT_DOCUMENTS`, and therefore in configuration backups. All three copies hold only ciphertext; a copy restores usefully only where its key is available. Whole-set restores keep the live store (`core.json_documents.DOCUMENTS_KEPT_BY_RESTORES`): a snapshot set restore and the update rollback neither replace nor remove `secrets.json`, and a configuration backup restores it only when it is named (`--file secrets.json`), because the store holds credentials set, rotated or converted after the copy, whose plaintext files are gone (`database.md`).

## Keys

`default_key_protections()` lists what this system can open; the first one protects a new store (`_keys.py`):

- Windows: `dpapi` first, then `key_file`. DPAPI wraps the key for the current user with entropy bound to store id and key id, never prompting (`CRYPTPROTECT_UI_FORBIDDEN`); the wrapped key lives in `secrets.json`, so a copy of the data directory opens only under the same Windows account on the same computer. Its location reads `Windows DPAPI of the user <DOMAIN\user>, wrapped in secrets.json`, so two processes under different accounts report different locations. `key_file` stays available to open a store created elsewhere with a key file.
- Other systems: `key_file`. The key lives outside the data directory. A new key is always created in the default directory `~/.config/vbot/keys/<store id>.key`, whatever the caller's environment: an update's verification start inherits the shell that ran `vbot update`, and the service started later must find the key. A lookup searches the default directory first, then `$XDG_CONFIG_HOME/vbot/keys/` (absolute XDG only), where earlier builds created keys. `VBOT_SECRET_KEY_FILE` is exclusive when set: only that file is used; an existing file is adopted (provisioned key, vBot key-file JSON `{format_version: 1, key_id, key}`), otherwise the key is created there. Key files are 0o600 in a 0o700 directory.
- A key file is never overwritten: it is written owner-only into a staging file and published with a hard link (exclusive create where links are unsupported), so a key path that already exists fails the creation (an explicit `VBOT_SECRET_KEY_FILE` created meanwhile is adopted), and a missing, foreign or unreadable key makes the store unavailable.

## Interfaces

- `SecretStore(data_dir, protections=None)`: `status()`, `entries(section)`, `entry(section, name)`, `entry_names(section)` (needs no key), `set_entry`, `remove_entry`, `update(change)` (one write for a `SecretTransaction`), `reset()`, `reload()`, `import_record()`, `fingerprint(content)`. Sections: `CREDENTIALS`, `OAUTH_TOKENS`. `SecretStoreStatus` names the key location found, every location this process searches (`key_locations`) and where it would create a new key (`new_key_location`); never a value.
- Credential names (`store.py`): `CREDENTIAL_NAME_PATTERN` (an environment-variable name of at most 200 characters; Storage's `ENV_KEY_PATTERN`, the RPCs, the CLI and the import accept only such names, while `ENTRY_NAME_PATTERN` also admits OAuth token ids), `CREDENTIAL_NAMES_IGNORE_CASE` (Windows), `credential_name_key(name)` (uppercase on Windows, the name elsewhere), `fold_credential_names(entries)` (one entry per credential: the all-uppercase spelling wins, otherwise the first spelling in sorted order). Storage, the Runtime fallback and the import use this one rule.
- Errors: `SecretStoreError` (a `StorageError`; nothing changed), `SecretStoreUnavailableError`, `SecretEntryUnavailableError`.
- Conversion (`plaintext.py`): `import_plaintext_credentials(store, remove_missing=True)`, `retire_plaintext_credentials`, `settle_plaintext_credentials(store, mode=...)`, `ignored_plaintext_files`, `plaintext_credential_files`, `quarantined_plaintext_files`, `quarantined_secret_stores`, `credential_file_reports` (doctor warnings), `secret_store_health` (status projection with `degraded_reason`, plus `plaintext_files`, `plaintext_copies`, `quarantined_stores`, and `plaintext_check` when the data directory cannot be listed).
- `data-store.status` carries the projection as `secrets`; a `degraded_reason` degrades the data store (`database.md` -> Status).

### Credential RPCs

`server/rpc/secret_methods.py` manages the `credentials` section by name; serialized with the Provider key and Settings mutations (`_settings_mutation_lock`). A value enters only through `secrets.set` and never appears in a result, an error or a log; logs name the credential and its effective source only.

- `secrets.list {}` -> `{state, reason, credentials: [{name, configured, source, damaged}]}`: every stored name (`entry_names`, damaged ones included). `configured` is whether the Runtime resolves a non-blank value; `source` is `process_environment`, `data_dir` or `null` (Runtime precedence, `runtime.md`).
- `secrets.set {name, value}` -> `{name, configured, source, changed}`; `secrets.unset {name}` -> `{name, configured, source, removed}`. On Windows both act on every case spelling of the name. Only a change reloads the Runtime credentials and publishes `resource_changed` for `data_store` and `providers`. A process-environment value outlives `unset` (`source` stays `process_environment`).
- `name` must match `ENV_KEY_PATTERN` and be at most 200 characters; `value` must be a non-empty single line. A refusal is `invalid_request` and never repeats the given name (it may be a pasted value); an unavailable store or failed write is `domain_error` with nothing changed. Tests: `tests/server/rpc/test_secret_methods.py`.
- Clients: the operator commands below and Settings -> Integrations -> Credentials (`webui/settings.md` -> Credentials).

### Operator commands

`cli/data_store_management.py`; `cli.md` owns the command grammar:

- `secrets list`, `secrets set <NAME>`, `secrets unset <NAME>` use the RPCs above. `set` reads the value from a hidden prompt or, when stdin is piped, from stdin, never from the command line. When a loopback target is stopped and `live_server_ports` finds no server on the data directory, they read and write the Secret Store directly (`"local": true`); a local `list` reports `source` as `data_dir` or `null` only, since the server's environment is unknown.
- `secrets status` (loopback only): the health projection (state, counts, damaged entries, key locations, ignored plaintext files, quarantined copies and stores); never a value.
- `secrets convert --yes`: import without removing anything (`remove_missing=False`) and retire, while the exact server target is stopped, then restart.
- `secrets reset --yes`: only for a store the CLI process cannot open. While a server runs on the data directory, it asks that server through `data_store.status` and proceeds only when the server reports the store `unavailable` too and would find the new key (equal `new_key_location`); an unreachable target while `live_server_ports` lists a server refuses. Every refusal names where the CLI and the server search for the key and would create a new one.

## States

- `absent`: no document. Reads are empty; the first write creates the store and its key. A write never creates a missing data directory, and the data-directory check runs before a key is created.
- `available`: the key opens the key check. Damaged entries are listed in `status().damaged` and logged once.
- `unavailable`: the document fails to load or validate, its protection is not available on this system, or the key is missing or wrong. Reads are empty, `entry` raises, every write raises `SecretStoreUnavailableError`, and the files stay byte-identical. One WARN names the reason and `vbot data-store secrets reset --yes`; the server keeps starting, credentials report as not configured, `doctor` reports an error on `$.key`, and the data-store status degrades.
- A read that fails with an I/O error (a scanner or backup tool holding `secrets.json`, a key file that cannot be read now) is retried after 50 ms and 200 ms; when it keeps failing, the store reads as unavailable for that access only (`_View.transient`, never cached), logs its own WARN, and the next access tries again. Only format, validation and key failures are cached until the document changes.
- `reset()` refuses a store this process opens and a transient failure, then moves the document to `<data-dir>/quarantine/secret-store/<UTC stamp>-<hex>/secrets.json` (never deleted; it opens again with its key, for example with `SecretStore(<that folder>)`) and creates a new empty store. Nothing else ever replaces a store.

## Conversion of plaintext credentials

Before the Secret Store, credentials lay in `<data-dir>/.env` and one `<data-dir>/oauth/<token id>.json` per OAuth login. vBot no longer reads them, except through this conversion (`PLAINTEXT_CREDENTIAL_PATTERNS`):

1. Import copies every credential into the store, reads the written store back to verify every entry, and records each source with a keyed fingerprint of its content and of every value it put there. It never changes a plaintext file and is idempotent. A store value is replaced only while it is exactly what the pending import put there; a value set or removed since (WebUI, CLI, RPC) or one that differed before the import stays, and the entry is reported as a conflict. With `remove_missing` (the update's verification start), an entry a recorded file no longer holds is removed under the same rule; `convert` never removes. On Windows, `.env` names fold to uppercase (a later line wins, as in the process environment). `.env` parsing otherwise matches the old reader: CR/LF/CRLF end a line, Unicode line separators stay in values, comments and lines without `=` are skipped, one matching outer quote pair is removed, a later line wins, blank values are template placeholders. Entries that cannot be imported (a name that is not an environment-variable name, such as `MY-KEY`, which no interface could remove from the store; a file that is not UTF-8, a token that does not load) are reported by file and line number only, never with the line's text. A file with such a problem is imported for what it holds but is not deleted. An import into an unavailable store raises; it never replaces the store.
2. Retire first narrows the record to the files that stay, then deletes each recorded file whose content is unchanged (fingerprint) and whose entries the store holds, removes an empty `oauth/`, purges the plaintext copies from configuration backups (`purge_config_backup_files`), OAuth copies from data snapshots (`remove_retired_document_copies`) and the copies the store holds exactly from `quarantine/json-documents/` (older set restores moved OAuth files there), and finally writes the remaining record. An interrupted retire therefore never leaves a record naming a deleted file. A file that changed after the import stays and is reported; one that cannot be read or deleted now stays recorded and is retried. When a backup manifest cannot be read, `backups_pending` keeps the record so a later start retries.
3. A plaintext file that is a symbolic link or junction, or lies in an `oauth/` that is one, is imported from the file it reaches, but vBot never deletes the link or its target: it stays reported (`is a link to <target>`) until the operator deletes it.

`settle_plaintext_credentials` runs in every Runtime start before credentials are loaded (`core/runtime/_bootstrap.py`):

- `verification` (an update's candidate start, `--verification-only`): imports into an absent store or one whose import is pending; a failure raises and fails the verification, so the update rolls back and the previous version keeps its plaintext files. The update worker runs the previous version's code, whose snapshot restore does not know `secrets.json` and never touched the plaintext files.
- `normal` (the first start after activation commits the update): retires a recorded import; a failure only warns and retries at the next start.
- Every mode then warns once about plaintext credential files outside a pending import: they are ignored, never imported automatically, and reported by `doctor` (warning) and the data-store status with a concrete remedy (import with `convert`; fix or remove the named lines first; delete the file when the store keeps other values; delete the link).

`doctor` and the status also report plaintext copies left in `quarantine/json-documents/` and, while `secrets.json` is missing, every Secret Store found in quarantine (newest first), with how to move it back. Installs that do not update through the application updater (a checkout, a dev data directory) convert with `vbot data-store secrets convert --yes`.

## Conventions

- Never log, return or print a value; logs and status name entries, sections, files, line numbers and reasons only.
- One `SecretStore` instance per data directory and process: its `RLock` serializes reads and writes. Every write holds `secrets.lock` (an OS-owned `msvcrt`/`fcntl` lock released when a process ends; re-entrant within one instance; 30 s timeout raises `SecretStoreError` with nothing changed) from its re-read to its write, so the server, the CLI and an update's verification start never lose each other's changes.
- The write lock needs no file content: Windows locks byte 0 even beyond EOF. Never initialize that byte before acquiring the lock; another first writer may already hold it (`test_concurrent_writers_keep_every_entry`).
- Enter writes through `update` (it holds `document_change` for the snapshot freeze before the store lock).
- Tests use `KeyFileProtection(directories=...)` or the isolated test home (`tests/conftest.py` clears `XDG_CONFIG_HOME` and `VBOT_SECRET_KEY_FILE`); on Windows the default store uses real DPAPI. Case-folding tests monkeypatch `core.secrets.store.CREDENTIAL_NAMES_IGNORE_CASE`. Tests: `tests/core/secrets/`, `tests/core/storage/test_storage_credentials.py`, `tests/core/providers/test_token_store.py`, `tests/cli/test_data_store.py`, `tests/server/rpc/test_secret_methods.py`.

## Constraints & Gotchas

- An unavailable state from a format or key failure is cached until `secrets.json` changes: restoring a lost key file takes effect after a server restart (Runtime snapshots credentials at start anyway).
- Key lookup depends on the process: its account (DPAPI, `HOME`) and `VBOT_SECRET_KEY_FILE`. A CLI started with another account or variable can fail to open a store the server opens; `secrets reset` therefore defers to the running server, and the store is never destroyed.
- Restoring a snapshot or configuration backup never rolls `secrets.json` back unless the operator names it in a configuration backup restore; a future store format change needs its own rollback story. Restoring a named copy from before a `reset` brings back a document whose key may be gone.
- An older vBot's set restore moved `secrets.json` to `quarantine/json-documents/` when its snapshot held none; the status and `doctor` report such a store while the live one is missing.
- Downgrading below the Secret Store after conversion leaves the old version without credentials; it never reads `secrets.json`. Such a version also knows `oauth/*.json` as snapshot documents: a snapshot whose OAuth copies the retire purged lists them in its manifest without the copy, so that version reports the snapshot as damaged and neither lists nor restores it. The current reader ignores those manifest entries.
- `LOG_LEVEL` in `.env` no longer selects the log level (the import warns); it comes from the process environment (`logging.md`).
