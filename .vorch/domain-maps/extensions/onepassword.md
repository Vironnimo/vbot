# 1Password Extension

The bundled Extension `resources/extensions/onepassword/` contributes the `onepassword` Secret Source (label `1Password`): items of the vaults a 1Password service account can read, through the 1Password CLI (`op`). It registers no Tools. Default-on like every bundled Extension; without `op` its status is `unavailable`.

## Overview

- `extension.py` declares the settings and calls `api.register_secret_source` (Extension API 17). `_source.py` owns `OnePasswordSource` (the SecretSource protocol of `core/secrets/sources.py`); `_cli.py` owns one windowless `op` call with closed stdin behind the `CommandRunner` protocol that tests replace. No `__init__.py` (loaded like the other directory Extensions).
- Settings: `token` (secret, `env_key: OP_SERVICE_ACCOUNT_TOKEN`, stored in the Secret Store, read live through `api.resolve_credential`) and `cli_path` (text; a file or a folder holding `op(.exe)`), read live through `api.get_config()`.
- Program lookup without `cli_path`: PATH, then on Windows `%PROGRAMFILES%\1Password CLI\op.exe` and `%LOCALAPPDATA%\Microsoft\WinGet\Links\op.exe`, elsewhere `/usr/local/bin/op`, `/usr/bin/op`, `/opt/homebrew/bin/op`. Cached for 30 s.

## Token and states

- A saved token makes the source `available` without an unlock. Without one it is `locked` and the unlock field `token` (concealed) is required; a token given at unlock lives only in memory until `lock()`.
- Unlock verifies the token with `op vault list`; with empty input it uses the saved token. `lock()` drops the memory token and the listings and also pauses a saved token until the next unlock; this pause is in memory only and ends with a server restart or Extension Reload.
- A token that `op` refuses on `vault list` is remembered as rejected: status `locked` with the reason "1Password refused the service account token." until a different token or a new unlock succeeds.
- No account is created and no sign-in flow is used; only service accounts work. Desktop-app integration and Connect servers are not supported.

## op contract

- Every call runs `op` in a copy of the server environment without any inherited `OP_*` variable (Connect variables would take precedence over the service account), plus `OP_SERVICE_ACCOUNT_TOKEN` and `OP_CACHE=false`. The token never appears in argv. Timeout 30 s.
- Commands: `vault list --format json`, `item list --vault <id> --format json`, `item get <item> --vault <vault> --format json --reveal`. Vault and item listings are cached in memory for 60 s per token because service accounts have hourly and daily request limits; values are read per call and never cached.
- Failures are classified from `op`'s English `[ERROR] <date> <time> <message>` line: rate limit -> `unavailable`, not found -> `not_found`, refused/unauthorized -> `denied`, network -> `unavailable`, else `unavailable`. The message appends at most 160 characters of `op`'s text with the timestamp removed and anything token-shaped (`ops_...`, `eyJ...`) or equal to the token replaced by `[token]`. Logs carry the code and exit status only.

## Entries and fields

- Reference = `<vault id>/<item id>` (an `op://` prefix is tolerated); containers are vault names, with ` (<id>)` added when two vaults share a name. `entries(container)` accepts the label or the vault id.
- Category -> kind and offered fields: `LOGIN` login (username, password, url, totp, notes), `PASSWORD` token (password, notes), `API_CREDENTIAL` token (credential, username, notes), `SECURE_NOTE` text (notes), `DATABASE` login (username, password, notes), `SERVER` login (username, password, url, notes), others text (notes). Listed visible values: the LOGIN username (`additional_information`) and the primary URL.
- Resolve order: `url`/`website` -> the primary URL; `totp`/`otp` -> the value of the `OTP` field (an `otpauth://` URI; AgentSecrets derives the code); otherwise the field with the purpose `USERNAME`/`PASSWORD`/`NOTES`, then exact field id, then id and label case-insensitively. Missing or empty -> `not_found`.
- `store` is not offered (empty capabilities); a call raises `unavailable`.

## Constraints & Gotchas

- Not verified against a real `op`: the JSON key names (`additional_information`, `urls[].primary`, `fields[].purpose`) and the error wording follow 1Password's CLI documentation. Check them first when items list without usernames or errors classify wrongly.
- Tests: `tests/resources/extensions/onepassword/test_onepassword_source.py` with a fake `op` that authenticates by the environment token.
