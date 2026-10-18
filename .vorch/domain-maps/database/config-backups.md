# Configuration backups

The format, API, capture, retention and restore of configuration backups (`core/database/config_backups.py`); the Term is `../database.md` -> Configuration backup, and Coverage below is what they cover.

## Coverage

The JSON document set (`secrets.json` included) plus `CONFIGURATION_FILE_PATTERNS`: `SOUL.md`/`USER.md`/`MEMORY.md` of Workspaces in their default location `agents/<id>/workspace/`, and the global and per-Agent prompt overrides under `prompts/`. Workspaces at a custom path, other Workspace files and Skills (own history) are outside it.

## Format

`<data-dir>/config-backups/` (`manifest_version` 1): `objects/<sha[:2]>/<sha256>` holds each distinct file content once (mode 0o600), and `backups/<YYYYmmddTHHMMSSZ-hex8>.json` names every file with `{file_size, sha256, mode}` plus `damaged: true` for a JSON document that did not parse as a JSON object. A manifest is written once, unknown fields are ignored, a path outside the current file set (a newer vBot's file) is neither shown nor restored but its object stays referenced (`ConfigBackup.other_objects`), and an unreadable manifest is skipped with one WARNING and stops object garbage collection.

## Interfaces

- `capture_config_backup(data_dir, *, reason, barrier=None, now=None) -> ConfigBackup | None` (`None` when the files equal the newest backup; raises `OperationLockBusyError` when the operation lock stays busy for `CONFIG_BACKUP_LOCK_SECONDS`, 5 s: expected contention the Runtime's schedule retries quietly).
- `list_config_backups` (newest first), `read_config_backup`, `describe_config_backup` (per file `same`/`differs`/`missing` against the current file, `damaged`, `created_after`), `config_backup_summary`.
- `restore_config_backup(data_dir, backup_id, *, paths=None, check_only=False) -> ConfigRestore(backup_id, restored, unchanged, skipped, created_after, before_restore)`.
- `configuration_fingerprint` (path, size, mtime) for cheap change checks.
- `purge_config_backup_files(data_dir, patterns) -> ConfigBackupPurge(removed_entries, rewritten_backups, complete)` removes files outside the current configuration set (the plaintext `.env` and `oauth/*.json` older backups hold) from every readable manifest and deletes the objects no manifest references any more - the one rewrite of an existing manifest; `complete` is false while a manifest cannot be read, and the Secret Store retries at a later start (`secrets.md`).
- The Runtime's schedule is `core/runtime/_config_backups.ConfigBackupSchedule` (`runtime.md`); the CLI is `vbot data-store config-backup list|show|restore` (`cli/data-store.md`); `data_store_status` reports `config_backups` (`snapshots-and-recovery.md` -> Status).

## Capture, retention and restore

A capture refuses during maintenance, takes the operation lock (so it never overlaps a data snapshot or restore) and reads the files through `capture_members` with no database, so with the Runtime's barrier its JSON documents agree like a snapshot's (`snapshot-consistency.md`); it stores objects before the manifest, replaces an existing object whose content no longer matches its hash (also when nothing changed), and is skipped when nothing differs from the newest backup. Retention after each capture keeps the newest `CONFIG_BACKUP_KEEP_LATEST` (10) plus the newest backup of every hour for 48 hours, every day for 30 days and every ISO week for 52 weeks, so a burst of bad states cannot push good ones out; it then removes objects no manifest references. A damaged JSON document is backed up and marked, never restored.

A restore is offline (the CLI stops and restarts the server) and holds the operation lock continuously from planning through its before-restore backup, all file writes and guard removal; a simultaneous restore cannot resume its live guard. Captures recheck maintenance after acquiring the lock, so a restore that failed while they waited still blocks them. It verifies every needed object first (`DatabaseCorruptError` for a missing or altered copy), takes a `before restore <id>` backup of the current state, then holds the maintenance guard (`config restore <backup-id>`) while it writes each selected file atomically. An interrupted restore keeps the guard; while it stands every other restore is refused, and repeating that restore (`ConfigRestore.interrupted`) reuses the earlier before-restore backup and ends the guard even when every file already matches, since the attempt may have failed after its last write.

It never removes a file: files created after the backup stay, and a file whose folder no longer exists (a deleted or renamed Agent, Project or Channel), that is damaged, or that is a link or lies below a linked folder (symbolic link or junction) is refused when named and skipped by a full restore; a full restore (no `paths`) also skips a differing `secrets.json` (`DOCUMENTS_KEPT_BY_RESTORES`), which is restored only when named; the link rule mirrors capture, which reads only regular files reached through real directories, so a restore never writes where its before-restore backup could not look (`tests/core/database/test_config_backups.py`).

## Logging

A configuration backup logs one INFO line (backup, files, changed, reason) and a WARNING naming damaged documents; a configuration restore one INFO line with the before-restore backup (`vbot.database`, policy in `logging.md`).
