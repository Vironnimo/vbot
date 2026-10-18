# Credential RPCs

The `secrets.list|set|unset` RPCs (`server/rpc/secret_methods.py`) that manage the `credentials` section of the Secret Store by name. The value rule they share with every other surface is in `../secrets.md` -> Credential RPCs.

`secret_methods.py` serializes them with the Provider key and Settings mutations (`_settings_mutation_lock`). Logs name the credential and its effective source only.

- `secrets.list {}` -> `{state, reason, credentials: [{name, configured, source, damaged}]}`: every stored name (`entry_names`, damaged ones included). `configured` is whether the Runtime resolves a non-blank value; `source` is `process_environment`, `data_dir` or `null` (Runtime precedence, `runtime.md`).
- `secrets.set {name, value}` -> `{name, configured, source, changed}`; `secrets.unset {name}` -> `{name, configured, source, removed}`. On Windows both act on every case spelling of the name. Only a change reloads the Runtime credentials and publishes `resource_changed` for `data_store` and `providers`. A process-environment value outlives `unset` (`source` stays `process_environment`).
- `name` must match `ENV_KEY_PATTERN` and be at most 200 characters; `value` must be a non-empty single line. A refusal is `invalid_request` and never repeats the given name (it may be a pasted value); an unavailable store or failed write is `domain_error` with nothing changed. Tests: `tests/server/rpc/test_secret_methods.py`.
- Clients: the `vbot data-store secrets` commands (`cli/data-store.md`) and Settings -> Integrations -> Credentials (`webui/settings.md` -> Credentials).
