# Providers, Models, Task Models

## Inspect

```bash
vbot provider list
vbot provider status <provider-id> [--connection <provider:connection-id>]
vbot provider usage [--connection <provider:connection-id>]...
```

`provider list` shows every connection with its enabled/usable state, accounts, credential source, and — for local endpoints — reachability. Run it before model or agent configuration work.

`provider usage` probes live upstream subscription limits for every supported usable Connection, or only the Connection ids selected by repeated `--connection` flags. It reports the plan, percentage used and remaining, reset timestamps, and a per-Provider error without hiding successful siblings. This is live Provider state; use `statistics usage` for persisted Session token/cost totals.

## Enable / disable a connection

```bash
vbot provider enable <provider-id> [--connection <provider:connection-id>]
vbot provider disable <provider-id> [--connection <provider:connection-id>]
```

- A disabled connection is completely passive: never probed, offers no models, and chat against it fails with a clear "disabled" error.
- Keyed connections start enabled; **keyless local connections (e.g. `ollama:local`) start disabled** — enable one when the user wants to use that local service.
- `--connection` is required only when the provider has more than one connection (the error lists the candidates).
- Enabling a local connection probes it immediately and reports the result. "Endpoint not reachable" is not a failure — the enable sticks, and the models appear automatically once the service runs (tell the user to start it, e.g. `ollama serve`).

## API-key credentials

```bash
vbot provider set-key <provider-id> <api-key> [--connection <provider:connection-id>] [--account <account-id>] [--refresh-models]
vbot provider unset-key <provider-id> [--connection <provider:connection-id>] [--account <account-id>]
```

- `set-key` writes the key to the target data-dir `.env` via server RPC, reloads provider credentials live (no restart), and prints only the connection and env-key name. Never echo the key back.
- `--connection` is required only when the provider has more than one API-key connection.
- Add `--refresh-models` when the user wants the provider's models usable right away.
- `unset-key` removes only data-dir `.env` keys; a credential set in the process environment is out of its reach and stays usable.

## OAuth device flow

OAuth/subscription connections use the device flow instead of `set-key` (`set-key` rejects OAuth connections, `connect` rejects API-key connections):

```bash
vbot provider connect <provider-id> --connection <provider:connection-id> [--account <account-id>]
vbot provider connect-status <provider-id> --connection <provider:connection-id> [--account <account-id>]
vbot provider disconnect <provider-id> --connection <provider:connection-id> [--account <account-id>]
```

`connect` prints a user code, a verification URL, and the expiry; the server polls in the background. Relay the code and URL to the user, then check `connect-status` until it reports `connected=yes`.

## Accounts — multiple credentials per connection

- A connection holds named credential slots; the default slot is `default`. Account ids are 1-32 characters of lowercase letters, digits, or underscores.
- `--account <account-id>` works on all five credential commands. Named API-key accounts persist under the derived env key `<BASE>__<ACCOUNT>` (e.g. `OPENAI_API_KEY__WORK`).
- A model pins an account with the suffix `<provider>/<model>::<connection>:<account>`.

## Models

```bash
vbot model list [--provider <provider-id>] [--capability <name>]... [--task <task-type>]... [--input-modality <name>]... [--output-modality <name>]... [--min-context-window <tokens>]
vbot model refresh [<provider-id>]
```

- `model list` returns only Models served by at least one enabled, credentialed Connection. Rows include the exact id accepted by `agent create --model` / `agent update --model`, effective context window, useful capabilities/task types, and `reachable: no` when a local service is currently down. For an Agent's primary Model, use `vbot model list --task chat`; repeat filter flags to require every listed value.
- `refresh` fetches provider model catalogs from the network (needs a credential for provider catalogs); omitting the provider id refreshes all refreshable Providers. It publishes a complete Model DB, including its Override files, under the target data directory and never writes the installed checkout.

## Task models

Bind a specialized task to a model target. Task types: `image_generation`, `image_understanding`, `speech_to_text`, `text_embedding`, `text_to_speech`, `video_generation`.

```bash
vbot task-model list
vbot task-model targets <task-type>
vbot task-model options <task-type> <target-id>
vbot task-model set <task-type> <target-id> [--options '<json-object>']
vbot task-model clear <task-type>
```

- Read target ids from `targets` (`<provider>/<model>::<connection>` or `local/<id>`) instead of constructing them by hand. `targets` lists connection-level ids — append a trailing `:<account-id>` yourself to pin a credential account.
- Check `options` for the valid keys before passing `--options`; it must be one JSON object as a single shell argument.
- `set` changes only the given task type; other bindings stay untouched.

```bash
vbot task-model set text_to_speech openai/gpt-4o-mini-tts::api-key --options '{"voice": "alloy"}'
vbot task-model set text_embedding openai/text-embedding-3-small::api-key
```
