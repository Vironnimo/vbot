# Secrets

The Secret Store: credentials and OAuth tokens of a data directory, encrypted at rest in `<data-dir>/secrets.json`, plus the conversion of the plaintext credential files it replaced.

## Overview

`core/secrets/` owns the store document, the protection of its data key, and the one-time conversion of `.env` and `oauth/*.json`. It is a kernel module of its own rather than part of `core/storage/` because it carries its own persisted format, cryptography, per-system key protection and operator commands, and because consumers outside Storage build on it directly: the Provider `TokenStore`, `doctor` validation and the `vbot data-store secrets` commands work without a `StorageManager`. Storage keeps the credential API that every other domain uses; the database kernel never imports it (it only receives the health projection).

- Owns: `secrets.json` (format, reads, writes, reset), key creation and lookup, entry encryption, plaintext import/retirement, the health projection.
- Does not own: which names a domain stores (environment-variable names chosen by Providers, Extensions, Channels, web Tools; token ids chosen by `core/providers/token_store.py`), process-environment precedence (`runtime.md` -> credential resolution), or the WebUI.

Consumers: `StorageManager.secrets` is the one store instance per data directory and process; `load_data_dir_credentials`/`set_data_dir_credential`/`remove_data_dir_credential` use the `credentials` section (`storage.md`), and `TokenStore(storage.secrets)` uses `oauth_tokens` (`providers/connections.md`). Runtime credential resolution, `ProviderCredentialResolver`, Skill `env` requirements, Extension `set_secret` and MCP credential writes all reach the store through those Storage functions. Process environment still wins over a stored value, and names stay environment-variable names.

## Terms

### Secret Store
`<data-dir>/secrets.json` plus its data key. Not the OS keychain: on Windows only the key is wrapped by DPAPI; the entries live in the data directory.

### Data key
One random 256-bit AES key per store, identified by a key id. Created with the store, never rotated in place; `reset` starts a new store with a new key.

### Damaged entry
One entry that does not authenticate under the store's key, name and section. It reads as missing, is reported, and stays untouched until replaced or removed.

## Data Model

`secrets.json` is a Generation 1 JSON document (`SECRET_STORE_FORMAT`, written by `write_json_document` with mode 0o600, sorted keys, unknown fields preserved):

- `store_id`: random 128-bit hex id.
- `key`: `{id, protection, check, wrapped?}`. `protection` is `dpapi` or `key_file`; `check` is a sealed constant that proves a key belongs to this store; `wrapped` is the DPAPI-wrapped data key (DPAPI only).
- `credentials`: environment-variable name -> sealed value.
- `oauth_tokens`: token id (`<provider>-<connection>[--<account>]`) -> sealed token document text (the OAuth token JSON format stays owned by `token_store.py`).
- `plaintext_import` (only while a conversion is unfinished): `{imported_at, sources: {relative path: keyed fingerprint}, credentials: [names], oauth_tokens: [names], backups_pending?}`.

A sealed entry is `base64(nonce || ciphertext || tag)`, AES-256-GCM with a fresh 96-bit nonce per write; the associated data is `vbot-secret-store/1`, store id, key id, section and name, so a ciphertext copied to another name, section, store or key does not open (`_crypto.py`). Entry names match `ENTRY_NAME_PATTERN`. Values are UTF-8 strings.

The document is in `DURABLE_DOCUMENTS` (`"secrets"`, validated by `validate_secret_store_file` in `doctor config`, which also reports an unopenable key and damaged entries), in `SNAPSHOT_DOCUMENTS`, and therefore in configuration backups. All three copies hold only ciphertext; a copy restores usefully only where its key is available.

## Keys

`default_key_protections()` lists what this system can open; the first one protects a new store (`_keys.py`):

- Windows: `dpapi` first, then `key_file`. DPAPI wraps the key for the current user with entropy bound to store id and key id, never prompting (`CRYPTPROTECT_UI_FORBIDDEN`); the wrapped key lives in `secrets.json`, so a copy of the data directory opens only under the same Windows account on the same computer. `key_file` stays available to open a store created elsewhere with a key file.
- Other systems: `key_file`. The key lives outside the data directory in `$XDG_CONFIG_HOME/vbot/keys/<store id>.key` (absolute XDG only), else `~/.config/vbot/keys/`; lookup always tries the default directory too, because a service unit and a shell may disagree about `XDG_CONFIG_HOME`. `VBOT_SECRET_KEY_FILE` names the file instead: an existing file is adopted (provisioned key, vBot key-file JSON `{format_version: 1, key_id, key}`), otherwise the key is created there. Key files are 0o600 in a 0o700 directory.
- A key file is never overwritten: creating a store whose default key path already exists fails, and a missing, foreign or unreadable key makes the store unavailable.

## Interfaces

- `SecretStore(data_dir, protections=None)`: `status()`, `entries(section)`, `entry(section, name)`, `entry_names(section)` (needs no key), `set_entry`, `remove_entry`, `update(change)` (one write for a `SecretTransaction`), `reset()`, `reload()`, `import_record()`, `fingerprint(content)`. Sections: `CREDENTIALS`, `OAUTH_TOKENS`.
- Errors: `SecretStoreError` (a `StorageError`; nothing changed), `SecretStoreUnavailableError`, `SecretEntryUnavailableError`.
- Conversion (`plaintext.py`): `import_plaintext_credentials`, `retire_plaintext_credentials`, `settle_plaintext_credentials(store, mode=...)`, `ignored_plaintext_files`, `plaintext_credential_reports` (doctor warnings), `secret_store_health` (status projection with `degraded_reason`).
- Operator commands (`cli/data_store_management.py`, loopback targets only): `vbot data-store secrets status` (state, counts, damaged entries, key location, ignored plaintext files; never a value), `secrets convert --yes` (import + retire while the exact server target is stopped, then restart), `secrets reset --yes` (only for an unavailable store).
- `data-store.status` carries the projection as `secrets`; a `degraded_reason` degrades the data store (`database.md` -> Status).

## States

- `absent`: no document. Reads are empty; the first write creates the store and its key. A write never creates a missing data directory, and the data-directory check runs before a key is created.
- `available`: the key opens the key check. Damaged entries are listed in `status().damaged` and logged once.
- `unavailable`: the document fails to load or validate, its protection is not available on this system, or the key is missing or wrong. Reads are empty, `entry` raises, every write raises `SecretStoreUnavailableError`, and the files stay byte-identical. One WARN names the reason and `vbot data-store secrets reset --yes`; the server keeps starting, credentials report as not configured, `doctor` reports an error on `$.key`, and the data-store status degrades.
- `reset()` moves the document to `<data-dir>/quarantine/secret-store/<UTC stamp>-<hex>/secrets.json` (never deleted; it opens again with its key, for example with `SecretStore(<that folder>)`), then creates a new empty store. Nothing else ever replaces a store.

## Conversion of plaintext credentials

Before the Secret Store, credentials lay in `<data-dir>/.env` and one `<data-dir>/oauth/<token id>.json` per OAuth login. vBot no longer reads them, except through this conversion (`PLAINTEXT_CREDENTIAL_PATTERNS`):

1. Import copies every credential into the store, reads the written store back to verify every entry, and records each fully imported source with a keyed HMAC fingerprint. It never changes a plaintext file and is idempotent; while the import is pending, a repeat mirrors the files (entries the files no longer hold are removed again). `.env` parsing matches the old reader: CR/LF/CRLF end a line, Unicode line separators stay in values, comments and lines without `=` are skipped, one matching outer quote pair is removed, a later line wins, blank values are template placeholders. A file with entries that cannot be imported (an invalid name, not UTF-8, a token that does not load) is left out of `sources` and therefore never deleted. An import into an unavailable store raises; it never replaces the store.
2. Retire deletes each recorded file whose content is unchanged (fingerprint) and whose entries the store still holds, removes an empty `oauth/`, purges the plaintext copies from configuration backups (`purge_config_backup_files`) and OAuth copies from data snapshots (`remove_retired_document_copies`), and clears the record. A file that changed after the import stays and is reported. When a backup manifest cannot be read, `backups_pending` keeps the record so a later start retries.

`settle_plaintext_credentials` runs in every Runtime start before credentials are loaded (`core/runtime/_bootstrap.py`):

- `verification` (an update's candidate start, `--verification-only`): imports into an absent store or one whose import is pending; a failure raises and fails the verification, so the update rolls back and the previous version keeps its plaintext files. The update worker runs the previous version's code, whose snapshot restore does not know `secrets.json` and never touched the plaintext files.
- `normal` (the first start after activation commits the update): retires a recorded import; a failure only warns and retries at the next start.
- Every mode then warns once about plaintext credential files outside a pending import: they are ignored, never imported automatically, and reported by `doctor` (warning) and the data-store status until `vbot data-store secrets convert --yes` imports them or the user deletes them.

Installs that do not update through the application updater (a checkout, a dev data directory) convert with `vbot data-store secrets convert --yes`.

## Conventions

- Never log, return or print a value; logs and status name entries, sections, files and reasons only.
- One `SecretStore` instance per data directory and process: its `RLock` serializes reads and writes, and every write re-reads the document first. Separate instances of one process are not serialized against each other; CLI commands that write stop the server first.
- Enter writes through `update` (it holds `document_change` for the snapshot freeze before the store lock).
- Tests use `KeyFileProtection(directories=...)` or the isolated test home (`tests/conftest.py` clears `XDG_CONFIG_HOME` and `VBOT_SECRET_KEY_FILE`); on Windows the default store uses real DPAPI. Tests: `tests/core/secrets/`, `tests/core/storage/test_storage_credentials.py`, `tests/core/providers/test_token_store.py`, `tests/cli/test_data_store.py`.

## Constraints & Gotchas

- The unavailable state is cached until `secrets.json` changes: restoring a lost key file takes effect after a server restart (Runtime snapshots credentials at start anyway).
- The CLI process opens the store with its own environment. A shell whose `XDG_CONFIG_HOME` or `VBOT_SECRET_KEY_FILE` differs from the server's can create a key the server does not find; the store then reports unavailable (never destroyed) and `secrets status` shows the key location it expects.
- Restoring a data snapshot or configuration backup from before a `reset` brings back a document whose key may be gone; restoring a snapshot from before the conversion moves `secrets.json` aside like any document missing from the snapshot.
- Downgrading below the Secret Store after conversion leaves the old version without credentials; it never reads `secrets.json`.
- The quarantine and JSON-document recovery batches of earlier operations may still hold plaintext copies; the purge covers configuration backups and data snapshots only.
- `LOG_LEVEL` in `.env` no longer selects the log level (the import warns); it comes from the process environment (`logging.md`).
