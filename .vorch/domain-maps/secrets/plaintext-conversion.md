# Plaintext credential conversion

The one-time conversion of the plaintext credential files the Secret Store replaced (`core/secrets/plaintext.py`): import, retire, the import record, the per-start settle step, and what `doctor` and the data-store status report about plaintext files and quarantined stores. The store itself is `../secrets.md`.

`plaintext.py` coordinates import, retirement, startup settling and copy purges. `_plaintext_files.py` owns discovery, parsing and import-source decoding; `_plaintext_reports.py` owns file inspection diagnostics and health projections. Public conversion and reporting entrypoints remain in `plaintext.py`.

## Interfaces

`import_plaintext_credentials(store, remove_missing=True)`, `retire_plaintext_credentials`, `settle_plaintext_credentials(store, mode=...)`, `ignored_plaintext_files`, `plaintext_credential_files`, `quarantined_plaintext_files`, `quarantined_secret_stores`, `credential_file_reports` (doctor warnings), `secret_store_health` (status projection with `degraded_reason`, plus `plaintext_files`, `plaintext_copies`, `quarantined_stores`, and `plaintext_check` when the data directory cannot be listed).

## Import record

`plaintext_import` in `secrets.json` (only while a conversion is unfinished): `{imported_at, sources: {relative path: {fingerprint, entries: {name: value fingerprint}}}, backups_pending?}`. Fingerprints are HMACs under the data key and reveal nothing without it. The first record format (`sources: {path: fingerprint}` plus `credentials`/`oauth_tokens` name lists) is still read; its entries count as not put there by the import, so a conversion never replaces or removes them.

## Conversion

Before the Secret Store, credentials lay in `<data-dir>/.env` and one `<data-dir>/oauth/<token id>.json` per OAuth login. vBot no longer reads them, except through this conversion (`PLAINTEXT_CREDENTIAL_PATTERNS`):

1. Import copies every credential into the store, reads the written store back to verify every entry, and records each source with a keyed fingerprint of its content and of every value it put there. It never changes a plaintext file and is idempotent. A store value is replaced only while it is exactly what the pending import put there; a value set or removed since (WebUI, CLI, RPC) or one that differed before the import stays, and the entry is reported as a conflict. With `remove_missing` (the update's verification start), an entry a recorded file no longer holds is removed under the same rule, including when all recorded source files have disappeared. That empty reconciliation keeps `backups_pending` so normal startup still purges their plaintext backup copies; `convert` never removes. On Windows, `.env` names fold to uppercase (a later line wins, as in the process environment). `.env` parsing otherwise matches the old reader: CR/LF/CRLF end a line, Unicode line separators stay in values, comments and lines without `=` are skipped, one matching outer quote pair is removed, a later line wins, blank values are template placeholders. Entries that cannot be imported (a name that is not an environment-variable name, such as `MY-KEY`, which no interface could remove from the store; a file that is not UTF-8, a token that does not load) are reported by file and line number only, never with the line's text. A file with such a problem is imported for what it holds but is not deleted. An import into an unavailable store raises; it never replaces the store.
2. Retire first narrows the record to the files that stay, then deletes each recorded file whose content is unchanged (fingerprint) and whose entries the store holds, removes an empty `oauth/`, purges the plaintext copies from configuration backups (`purge_config_backup_files`), OAuth copies from data snapshots (`remove_retired_document_copies`) and the copies the store holds exactly from `quarantine/json-documents/` (older set restores moved OAuth files there), and finally writes the remaining record. An interrupted retire therefore never leaves a record naming a deleted file. A file that changed after the import stays and is reported; one that cannot be read or deleted now stays recorded and is retried. When a backup manifest cannot be read, `backups_pending` keeps the record so a later start retries.
3. A plaintext file that is a symbolic link or junction, or lies in an `oauth/` that is one, is imported from the file it reaches, but vBot never deletes the link or its target: it stays reported (`is a link to <target>`) until the operator deletes it.

## Settle per start mode

`settle_plaintext_credentials` runs in every Runtime start before credentials are loaded (`core/runtime/_bootstrap.py`):

- `verification` (an update's candidate start, `--verification-only`): imports into an absent store or one whose import is pending; a failure raises and fails the verification, so the update rolls back and the previous version keeps its plaintext files. The update worker runs the previous version's code, whose snapshot restore does not know `secrets.json` and never touched the plaintext files.
- `normal` (the first start after activation commits the update): retires a recorded import; a failure only warns and retries at the next start.
- Every mode then warns once about plaintext credential files outside a pending import: they are ignored, never imported automatically, and reported by `doctor` (warning) and the data-store status with a concrete remedy (import with `convert`; fix or remove the named lines first; delete the file when the store keeps other values; delete the link).

Installs that do not update through the application updater (a checkout, a dev data directory) convert with `vbot data-store secrets convert --yes`.

## Doctor and status reports

`doctor` and the status also report plaintext copies left in `quarantine/json-documents/` and, while `secrets.json` is missing, every Secret Store found in quarantine (newest first), with how to move it back. Quarantined stores come from `reset` (`quarantine/secret-store/`) and from an older vBot's set restore, which moved `secrets.json` to `quarantine/json-documents/` when its snapshot held none (`quarantined_secret_stores`).

## Gotchas

- Downgrading below the Secret Store after conversion leaves the old version without credentials; it never reads `secrets.json`. Such a version also knows `oauth/*.json` as snapshot documents: a snapshot whose OAuth copies the retire purged lists them in its manifest without the copy, so that version reports the snapshot as damaged and neither lists nor restores it. The current reader ignores those manifest entries.
- `LOG_LEVEL` in `.env` no longer selects the log level (the import warns); it comes from the process environment (`logging.md`).
