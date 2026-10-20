# Secrets

The Secret Store: credentials and OAuth tokens of a data directory, encrypted at rest in `<data-dir>/secrets.json`, plus the conversion of the plaintext credential files it replaced; and Agent Secrets, kept in the same store.

## Overview

`core/secrets/` owns the store document, the protection of its data key, the one-time conversion of `.env` and `oauth/*.json`, and Agent Secrets. It is a kernel module of its own rather than part of `core/storage/` because it carries its own persisted format, cryptography, per-system key protection and operator commands, and because consumers outside Storage build on it directly: the Provider `TokenStore`, `doctor` validation and the `vbot data-store secrets` commands work without a `StorageManager`. Storage keeps the credential API that every other domain uses; the database kernel never imports it (it only receives the health projection).

`store.py` owns store state, key opening, transactions and process locking; `_document.py` owns the document schema, structural validation and decoding. Public store exports remain in `store.py`.

- Owns: `secrets.json` (format, reads, writes, reset), `secrets.lock`, key creation and lookup, entry encryption, credential-name folding, plaintext import/retirement, the health projection.
- Does not own: which names a domain stores (environment-variable names chosen by Providers, Extensions, Channels, web Tools; token ids chosen by `core/providers/token_store.py`), process-environment precedence (`runtime.md` -> Service properties, `provider_credentials`), the `secrets.*` RPC handlers (`server/rpc/secret_methods.py`, contract below), the operator command grammar (`cli/data-store.md`), or the WebUI.

Consumers: `StorageManager.secrets` is the one store instance per data directory and process; `load_data_dir_credentials`/`set_data_dir_credential`/`remove_data_dir_credential` use the `credentials` section (`storage.md`), `TokenStore(storage.secrets)` uses `oauth_tokens` (`providers/connections.md`), and `AgentSecrets(storage.secrets)` uses `agent_secrets`. Runtime credential resolution, `ProviderCredentialResolver`, Skill `env` requirements, Extension `set_secret`, MCP credential writes and the `secrets.*` RPCs all reach the store through those Storage functions. Process environment still wins over a stored value, and names stay environment-variable names.

Agent Secrets (`agent_secrets.py` and its helpers) decide which values exist, who may use them where, and what masking replaces. Where values go is not owned here: Tools declare sink arguments and the Tool registry substitutes (`tools.md` -> Contracts), Chat and Compaction apply the masker, Extensions contribute Secret Sources. The `secrets` Tool, `/secret` and the RPCs live with their callers (`secrets/agent-secrets.md` -> Source).

## Terms

Agent Secret is a shared term (`.vorch/GLOSSARY.md`).

### Secret Store
`<data-dir>/secrets.json` plus its data key. Not the OS keychain: on Windows only the key is wrapped by DPAPI; the entries live in the data directory.

### Data key
One random 256-bit AES key per store, identified by a key id. Created with the store, never rotated in place; `reset` starts a new store with a new key.

### Damaged entry
One entry that does not authenticate under the store's key, name and section. It reads as missing, is reported, and stays untouched until replaced or removed.

### Import record
The `plaintext_import` field: which plaintext files a conversion imported and the keyed fingerprint of every value it put into the store. It decides which store values the conversion may still replace or remove (`secrets/plaintext-conversion.md`).

### Secret reference
`{{secret:NAME[.field]}}`: what an Agent writes where a value belongs and what masking shows instead of the value; outside a sink it stays literal text.

### Sink
A class of Tool arguments where vBot substitutes references (`environment`, `terminal`, `files`, `typing`, `external_tools`); each Agent Secret lists its allowed sinks.

### Secret Source
A password manager an Extension contributes (`SecretSource`) that an Agent Secret can be bound to; it supplies values, never grants. Not an Extension's own credential (`set_secret`).

### Secret request
An Agent's `secrets` `request`: a form the user answers or declines within 10 minutes.

## Data Model

`secrets.json` is a Generation 1 JSON document (`SECRET_STORE_FORMAT`, written by `write_json_document` owner-only on every platform, with mode 0o600 on POSIX, sorted keys, unknown fields preserved):

- `store_id`: random 128-bit hex id.
- `key`: `{id, protection, check, wrapped?}`. `protection` is `dpapi` or `key_file`; `check` is a sealed constant that proves a key belongs to this store; `wrapped` is the DPAPI-wrapped data key (DPAPI only).
- `credentials`: environment-variable name -> sealed value.
- `oauth_tokens`: token id (`<provider>-<connection>[--<account>]`) -> sealed token document text (the OAuth token JSON format stays owned by `token_store.py`).
- `agent_secrets`: Secret id -> sealed Agent Secret record, metadata and values together (`secrets/agent-secrets.md` -> Records).
- `plaintext_import`: the Import record, only while a conversion is unfinished.

A sealed entry is `base64(nonce || ciphertext || tag)`, AES-256-GCM with a fresh 96-bit nonce per write; the associated data is `vbot-secret-store/1`, store id, key id, section and name, so a ciphertext copied to another name, section, store or key does not open (`_crypto.py`). Entry names match `ENTRY_NAME_PATTERN`. Values are UTF-8 strings.

`<data-dir>/secrets.lock` is the inter-process write lock (Conventions); it holds no data.

The document is in `DURABLE_DOCUMENTS` (`"secrets"`, validated by `validate_secret_store_file` in `doctor config`, which also reports an unopenable key and damaged entries), in `SNAPSHOT_DOCUMENTS`, and therefore in configuration backups. All three copies hold only ciphertext; a copy restores usefully only where its key is available. Whole-set restores keep the live store (`database.md` -> JSON document set): a configuration backup restores it only when it is named (`--file secrets.json`), because the store holds credentials set, rotated or converted after the copy, whose plaintext files are gone.

## Keys

`default_key_protections()` lists what this system can open; the first one protects a new store (`_keys.py`):

- Windows: `dpapi` first, then `key_file`. DPAPI wraps the key for the current user with entropy bound to store id and key id, never prompting (`CRYPTPROTECT_UI_FORBIDDEN`); the wrapped key lives in `secrets.json`, so a copy of the data directory opens only under the same Windows account on the same computer. Its location reads `Windows DPAPI of the user <DOMAIN\user>, wrapped in secrets.json`, so two processes under different accounts report different locations. `key_file` stays available to open a store created elsewhere with a key file.
- Other systems: `key_file`. The key lives outside the data directory. A new key is always created in the default directory `~/.config/vbot/keys/<store id>.key`, whatever the caller's environment: an update's verification start inherits the shell that ran `vbot update`, and the service started later must find the key. A lookup searches the default directory first, then `$XDG_CONFIG_HOME/vbot/keys/` (absolute XDG only), where earlier builds created keys. `VBOT_SECRET_KEY_FILE` is exclusive when set: only that file is used; an existing file is adopted (provisioned key, vBot key-file JSON `{format_version: 1, key_id, key}`), otherwise the key is created there. Key files are 0o600 in a 0o700 directory.
- A key file is never overwritten: it is written owner-only into a staging file and published with a hard link (exclusive create where links are unsupported), so a key path that already exists fails the creation (an explicit `VBOT_SECRET_KEY_FILE` created meanwhile is adopted), and a missing, foreign or unreadable key makes the store unavailable.

## Interfaces

- `SecretStore(data_dir, protections=None)` with sections `CREDENTIALS` and `OAUTH_TOKENS`: `update(change)` makes one write for a `SecretTransaction`; `entry_names(section)` needs no key; `reset()` is the only replacement of a store (States). `status()` returns a `SecretStoreStatus` naming the key location found, every location this process searches (`key_locations`) and where it would create a new key (`new_key_location`); never a value.
- Credential names (`store.py`): `CREDENTIAL_NAME_PATTERN` (an environment-variable name of at most 200 characters; Storage's `ENV_KEY_PATTERN`, the RPCs, the CLI and the import accept only such names, while `ENTRY_NAME_PATTERN` also admits OAuth token ids), `CREDENTIAL_NAMES_IGNORE_CASE` (Windows), `credential_name_key(name)` (uppercase on Windows, the name elsewhere), `fold_credential_names(entries)` (one entry per credential: the all-uppercase spelling wins, otherwise the first spelling in sorted order). Storage, the Runtime fallback and the import use this one rule.
- Errors: `SecretStoreError` (a `StorageError`; nothing changed), `SecretStoreUnavailableError`, `SecretEntryUnavailableError`.
- Health: `secret_store_health` (`plaintext.py`) is the status projection; `data-store.status` carries it as `secrets`, and its `degraded_reason` degrades the data store (`database/snapshots-and-recovery.md` -> Status).
- Plaintext files: vBot never reads `.env` or `oauth/*.json` except through the conversion, which `settle_plaintext_credentials` runs in every Runtime start before credentials load; other plaintext files stay ignored (`secrets/plaintext-conversion.md` -> Settle per start mode).

### Credential RPCs

`secrets.list|set|unset` manage the `credentials` section by name. A value enters only through `secrets.set` and never appears in a result, an error or a log (contract: `secrets/credential-rpcs.md`). The `vbot data-store secrets` commands use them, or the store directly for a stopped loopback target (`cli/data-store.md`).

### Agent Secrets

- `Runtime.agent_secrets` (`AgentSecrets` over `StorageManager.secrets`) and `Runtime.secret_requests`: reads come from memory; writes and Secret Source calls block, so run them off the Event Loop.
- Use is checked per Agent id and Session; a Sub-Agent Session uses only what was bound to or passed into it (`passed_only`). A value leaves only through `substitute` (sink arguments), `environment` (shell `env_keys`) and `agent_secrets.reveal`, the one RPC that returns a concealed value; all else is `describe()` or masked text.
- The `agent_secrets.*` RPCs are distinct from `secrets.*`; changes publish `resource_changed` kind `agent_secrets`. Extension API 17 adds `api.register_secret_source` and `secret_sinks`.

## States

- `absent`: no document. Reads are empty; the first write creates the store and its key. A write never creates a missing data directory, and the data-directory check runs before a key is created.
- `available`: the key opens the key check. Damaged entries are listed in `status().damaged` and logged once.
- `unavailable`: the document fails to load or validate, its protection is not available on this system, or the key is missing or wrong. Reads are empty, `entry` raises, every write raises `SecretStoreUnavailableError`, and the files stay byte-identical. One WARN names the reason and `vbot data-store secrets reset --yes`; the server keeps starting, credentials report as not configured, `doctor` reports an error on `$.key`, and the data-store status degrades.
- A read that fails with an I/O error (a scanner or backup tool holding `secrets.json`, a key file that cannot be read now) is retried after 50 ms and 200 ms; when it keeps failing, the store reads as unavailable for that access only (`_View.transient`, never cached), logs its own WARN, and the next access tries again. Format and validation failures can be cached until the document changes; key-file-backed views are rechecked on every access. Loading a new document version also repairs inherited file permissions from earlier releases.
- `reset()` refuses a store this process opens and a transient failure, then moves the document to `<data-dir>/quarantine/secret-store/<UTC stamp>-<hex>/secrets.json` (never deleted; it opens again with its key, for example with `SecretStore(<that folder>)`) and creates a new empty store. Nothing else ever replaces a store.

## Conventions

- Never log, return or print a value; logs and status name entries, sections, files, line numbers and reasons only.
- One `SecretStore` instance per data directory and process: its `RLock` serializes reads and writes. Every write holds `secrets.lock` (an OS-owned `msvcrt`/`fcntl` lock released when a process ends; re-entrant within one instance; 30 s timeout raises `SecretStoreError` with nothing changed) from its re-read to its write, so the server, the CLI and an update's verification start never lose each other's changes.
- The write lock needs no file content: Windows locks byte 0 even beyond EOF. Never initialize that byte before acquiring the lock; another first writer may already hold it (`test_concurrent_writers_keep_every_entry`).
- Enter writes through `update` (it holds `document_change` for the snapshot freeze before the store lock).
- Tests use `KeyFileProtection(directories=...)` or the isolated test home (`tests/conftest.py` clears `XDG_CONFIG_HOME` and `VBOT_SECRET_KEY_FILE`); on Windows the default store uses real DPAPI. Case-folding tests monkeypatch `core.secrets.store.CREDENTIAL_NAMES_IGNORE_CASE`. Tests: `tests/core/secrets/`, `tests/core/storage/test_storage_credentials.py`, `tests/core/providers/test_token_store.py`, `tests/cli/test_data_store.py`, `tests/server/rpc/test_secret_methods.py`.

## Constraints & Gotchas

- Key-file-backed stores re-read the external key before each access, including writes: removal or replacement makes the store unavailable immediately, and restoring the original key restores access without restarting. Document-backed protections can cache a view until `secrets.json` changes (Runtime still snapshots credentials at start).
- Key lookup depends on the process: its account (DPAPI, `HOME`) and `VBOT_SECRET_KEY_FILE`. A CLI started with another account or variable can fail to open a store the server opens; `secrets reset` therefore defers to the running server, and the store is never destroyed.
- Because restores keep the live store, a future store format change needs its own rollback story. Restoring a named copy from before a `reset` brings back a document whose key may be gone.

## Agent-facing text

This domain owns the `secrets` Tool definition, results and errors and the `## Secrets` System Prompt block with its Secret lines (`core/tools/secrets.py`), the note `/secret` and its form add to a Session (`core/chat/secret_giving.py`) and the substitution refusals (`SecretUseError` in `core/secrets/agent_secrets.py`). Before you change, add or delete a sentence of them, read `secrets/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add. Shell and Sub-Agent Secret texts: `tools/shell/agent-facing-text.md`, `subagents/agent-facing-text.md`.

## References

Read these only when your task matches - not by default.

- Changing the `.env`/`oauth` conversion (import, retire, `settle_plaintext_credentials`), the Import record, or the plaintext and quarantined-store findings of `doctor` and the data-store status -> `secrets/plaintext-conversion.md`
- Changing the `secrets.*` RPCs or their clients -> `secrets/credential-rpcs.md`
- Changing Agent Secrets (records, grants, Sub-Agent pass-through, sinks, masking, Secret Sources, requests, `/secret`, Channel message removal, `agent_secrets.*` RPCs) -> `secrets/agent-secrets.md`
- Changing the bundled KeePassXC or 1Password Secret Source -> `extensions/keepassxc.md`, `extensions/onepassword.md`
