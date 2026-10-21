# KeePassXC Extension

The bundled Extension `resources/extensions/keepassxc/` contributes the `keepassxc` Secret Source (label `KeePassXC`): entries of one local KeePassXC database, read through `keepassxc-cli`. It registers no Tools and makes no network calls. Default-on like every bundled Extension; until a database is set its status is `unavailable` with the reason.

## Overview

- `extension.py` declares the settings and calls `api.register_secret_source` (Extension API 17). `_source.py` owns `KeePassXCSource` (the SecretSource protocol of `core/secrets/sources.py`); `_cli.py` owns one windowless `keepassxc-cli` process call behind the `CommandRunner` protocol that tests replace. No `__init__.py`: the loader imports `extension.py` as `vbot_ext.keepassxc` with the folder as package path, so relative imports work.
- Settings, read live on every call through `api.get_config()` (a change needs no reload): `database` (path of the `.kdbx`), `key_file` (optional), `cli_path` (optional; a file, or a folder holding `keepassxc-cli(.exe)`). Paths accept quotes, `~` and environment variables.
- Program lookup without `cli_path`: PATH (`keepassxc-cli`, then `keepassxc.cli` of the Snap package), then `%PROGRAMFILES%\KeePassXC\keepassxc-cli.exe` on Windows. The result is cached for 30 s.

## States and unlock

- `unavailable`: the program, the database file, or a set key file is missing; the reason names the setting to correct.
- `locked`: not unlocked yet, or the unlock was dropped with a reason: the `database`/`key_file` setting changed after the unlock, or the database stopped opening with the remembered master password (logged as a WARNING without paths).
- `available`: after an unlock that `keepassxc-cli ls` verified.
- Unlock field `password` (`Master password`, concealed) is required unless a key file is set (then an empty password opens with `--no-password`). A wrong password or key file raises `denied`. The password lives only in memory on the source object until `lock()`, a settings change or a failed reopen drops it.

## keepassxc-cli contract

- Every call is `keepassxc-cli <command> -q [options] [-k <key file>] [--no-password] -- <database> [args]`, one process at a time (an added entry rewrites the file), 30 s timeout. The master password goes on stdin as the first line, never into argv; this rules out passwords with line breaks, which unlock refuses.
- keepassxc-cli localizes its messages and exits with 1 for every failure (verified with 2.7.11). The source therefore never parses error text and never forwards stderr: after a failed call it runs `ls` to tell "the database no longer opens" (-> `locked`) from "this entry or attribute is missing" (-> `not_found`, after a fresh listing decides which).
- Listing: `search ""` returns every entry as `/Group/Sub/Title`; it fails (exit 1) on a database without entries, which the follow-up `ls` disambiguates. The listing is cached for 15 s per unlock. `ls -R -f` is not used because it prints a localized placeholder for empty groups.
- Reading: `show -s -a <attribute> <path>` per value; attribute names are case-insensitive in keepassxc-cli. An empty required value is `not_found`; absent/empty optional fields of an existing entry are `optional_field_missing`.

## Entries and fields

- Reference = entry path without the leading slash (`Internet/Mail`); container = the parent group path, `None` for root entries. Containers are the groups that hold entries (empty groups are not offered). Every entry offers kind `login` and the fields `username`, `password`, `url`, `notes`, `totp` (password, notes and totp concealed, no visible values listed). Only password is required; the other offered fields are marked optional.
- Field -> attribute: `username` `UserName`, `password` `Password`, `url` `URL`, `notes` `Notes`, `totp` `otp` and then the pre-2.6 `TOTP Seed`, including when `otp` exists but is empty. `totp` returns the stored `otpauth://` URI or seed; AgentSecrets derives the current code. `title` reads `Title`; any other field name reads the custom attribute of that name.
- `store` (capability `store`) creates an entry with `add`: only username, password and URL, no line breaks, a title without `/`, no existing entry at that path. The new password goes on stdin after the master password (`--password-prompt`). A timeout says the entry may exist and asks to check before retrying.

## Constraints & Gotchas

- Entry titles containing `/` cannot be addressed (paths are slash-separated); with duplicate paths keepassxc-cli reads the first. Recycle bin entries are listed like any other group.
- Logs carry the source name and command only, never paths, entry names, values or stderr. Subprocess exceptions are re-raised `from None` so captured output cannot chain into an error.
- Not supported: hardware keys (YubiKey challenge-response) and KeePassXC's browser-integration protocol. Only Windows was checked against a real `keepassxc-cli`; the Linux/macOS lookup relies on PATH.
- Tests: `tests/resources/extensions/keepassxc/test_keepassxc_source.py` with a fake runner that checks stdin and `-k` like the real CLI.
