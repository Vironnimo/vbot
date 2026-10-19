# Providers, Models, Task Models

## Inspect

```bash
vbot provider list
vbot provider status <provider-id> [--connection <provider:connection-id>]
vbot provider usage [--connection <provider:connection-id>]...
vbot provider history list [--since <iso>] [--until <iso>]
vbot provider history clear --yes
```

`provider list` groups Connections by Provider and shows a readable state alongside exact ids, Account names, and local endpoint reachability. Use `vbot provider list --details` for all enabled/usable fields and credential sources, or `provider status <provider-id>` for one Provider. Configured means enabled with credentials; this listing does not test upstream access. Use it when selecting a Connection or diagnosing missing Models.

`provider usage` probes live upstream subscription limits for every supported usable Connection, or only the selected ones. It reports the plan, percentage used and remaining, reset timestamps, and a per-Provider error without hiding successful siblings. `provider history list [--since <iso>] [--until <iso>]` reads vBot's own recorded limit observations (durable samples, not live polls); `provider history clear --yes` deletes all of them. These are live Provider state; use `statistics usage` for persisted Session token totals.

## Custom Providers

```bash
vbot provider custom list
vbot provider custom save <provider-id> --name <display-name> --base-url <http(s)-url> [--adapter openai_compatible] [--auth api_key|none] [--api-key-stdin] [--models-endpoint /models] [--model <model-id>]... [--wire-file <path> | --clear-wire]
vbot provider custom delete <provider-id>
```

- A Custom Provider speaks the OpenAI-compatible API through one Connection, `default`.
- `custom save` creates the Provider or replaces its endpoint, authentication, discovery path and Model list, and applies at once. Each `--model` adds a chat Model with conservative capabilities; set richer modality and capability facts in the WebUI. An existing Provider keeps its sampling defaults, its request rules (`--wire-file <path>` replaces them with a JSON block, `--clear-wire` removes them) and the stored capabilities of every Model id that stays in the list.
- `--api-key-stdin` reads the key from UTF-8 stdin into the Secret Store; without it, the stored key stays.
- `custom delete` removes generated data-dir credentials but does not rewrite Agent/default/task Model references; tell the user those references become unavailable.

## Enable / disable a connection

```bash
vbot provider enable <provider-id> [--connection <provider:connection-id>]
vbot provider disable <provider-id> [--connection <provider:connection-id>]
```

- A disabled connection is completely passive: never probed, offers no models, and chat against it fails with a clear "disabled" error.
- Keyed connections start enabled; keyless local connections such as `ollama:local` start disabled: enable one when the user wants to use that local service.
- `--connection` is required only when the provider has more than one connection (the error lists the candidates). A Connection id as `provider list` shows it can replace both: `vbot provider enable ollama:local`. `provider status`, `provider key` and the OAuth commands accept it the same way.
- Enabling a local connection probes it immediately and reports the result. "Endpoint not reachable" is not a failure: the enable sticks, and the models appear automatically once the service runs (tell the user to start it, e.g. `ollama serve`).

## API-key credentials

```bash
vbot provider key set <provider-id> --stdin [--connection <provider:connection-id>] [--account <account-id>] [--refresh-models]
vbot provider key unset <provider-id> [--connection <provider:connection-id>] [--account <account-id>]
```

- Pipe the key to `--stdin`, never as an argument. `key set` saves it in the server's Secret Store, applies it at once and prints only the Connection and credential name.
- `--connection` is required only when the provider has more than one API-key connection.
- Add `--refresh-models` to discover Models after saving the key. If discovery fails, the command exits nonzero and reports both the saved key and failed refresh. Correct the connection, then retry `model refresh`; do not resend the key just to retry discovery.
- `key unset` removes only the stored key; a credential set in the server's process environment is out of its reach and stays usable.

## OAuth device flow

`vbot provider connect openai` starts the Subscription sign-in. When `--connection` is omitted, `connect`, `connection status`, and `disconnect` select the Provider's only OAuth Connection. Multiple candidates require an explicit `--connection`; API-key Connections are never selected for OAuth. The response names the exact Connection and Account for subsequent checks.

OAuth/subscription connections use the device flow instead of `key set` (`key set` rejects OAuth connections, `connect` rejects API-key connections):

```bash
vbot provider connect <provider-id> [--connection <provider:connection-id>] [--account <account-id>]
vbot provider connection status <provider-id> [--connection <provider:connection-id>] [--account <account-id>]
vbot provider disconnect <provider-id> [--connection <provider:connection-id>] [--account <account-id>]
```

`connect` prints a user code, a verification URL, and the expiry; the server polls in the background. Relay the code and URL to the user, then check `connection status` until it reports `connected=yes`. If the flow is no longer pending and is not connected, inspect the state and begin a new flow when sign-in is still wanted; do not poll indefinitely.

## Accounts: several credentials per Connection

- A connection holds named credential slots; the default slot is `default`. Account ids are 1-32 lowercase letters, digits or underscores and start with a letter or digit.
- `--account <account-id>` works on all five credential commands. Named API-key accounts persist under the derived env key `<BASE>__<ACCOUNT>` (e.g. `OPENAI_API_KEY__WORK`).
- A model pins an account with the suffix `<provider>/<model>::<connection>:<account>`.

## Models

```bash
vbot model list [--query <text>]... [--provider <provider-id>] [--capability <name>]... [--task <task-type>]... [--input-modality <name>]... [--output-modality <name>]... [--min-context-window <tokens>]
vbot model show <provider>/<model-id>
vbot model refresh [<provider-id>]
```

- `model list` returns only Models served by at least one usable Connection (enabled and authenticated when required). Rows include the exact id accepted by `agent create --model` / `agent update --model`, effective context window, useful capabilities/task types, and `reachable: no` when a local service is currently down. For an Agent's primary Model, use `vbot model list --task chat`; repeat filter flags to require every listed value.
- `--query` finds the Model the user names: every word must occur in the Model id, name or family, ignoring case, across all Providers (`vbot model list --task chat --query claude --query opus`). Add `--provider` when the user names the Provider. Prefer it to an unfiltered list, which can hold hundreds of Models.
- `model show` returns the complete public Model record, including modalities, task types, supported parameters and voices, typed task options, reasoning controls, context/output limits, connection restrictions, usable Connections, family, and metadata. Use it instead of web search when the question is about the Model data currently loaded by vBot.
- `refresh` fetches the Model lists of one Provider, or of every Provider without an id, through their usable Connections.
- `vbot model forget-wire-facts <provider>[/<model-id>] [--connection <connection-id>]` forgets the parameters and limits vBot learned from a Provider's refusals; run it when such a refusal was a one-off or the Provider has fixed it. Learned facts otherwise expire after 30 days.

## Task models

Bind a specialized task to a model target. Task types: `decision`, `image_generation`, `image_understanding`, `live_voice`, `music_generation`, `speech_to_text`, `text_embedding`, `text_to_speech`, `video_generation`.

```bash
vbot task-model list
vbot task-model status <task-type>
vbot task-model target list <task-type>
vbot task-model option list <task-type> [<target-id>]
vbot task-model set <task-type> <target-id> [--option <name> <value>]...
vbot task-model set <task-type> <target-id> [--options '<json-object>' | --options-stdin]
vbot task-model option set <task-type> <name> [<value> | --stdin]
vbot task-model option unset <task-type> <name>
vbot task-model clear <task-type>
```

- Read target ids from `target list` (`<provider>/<model>::<connection>` or `local/<id>`) instead of constructing them by hand. `target list` lists Connection-level ids; append a trailing `:<account-id>` yourself to pin a credential account.
- Run `option list <task-type> <target-id>` before the first binding. After a binding exists, omit the target to inspect its `fields`, `configured_options`, and default-merged `effective_options`.
- Prefer repeated `--option <name> <value>` over `--options` for ordinary setup. Each value is read as the `type` its field has in `option list`: `select` and `text` fields keep the text as written (`--option duration 8` stores the choice `"8"`), `number` fields take a number, and `boolean` fields take `true` or `false`. Use `--options` only when replacing the complete option object is intentional. On shells that alter JSON quoting, pipe the object to `--options-stdin` instead.
- Use `option set` and `option unset` for later changes. They preserve every sibling option and reject unknown names, invalid select values, wrong types, and out-of-range numbers before persistence. For object-valued options such as `extra_options`, pipe JSON to `option set ... --stdin` so shell quoting cannot corrupt it.
- A Model's `supported_voices` and the `voice` choices returned by `task-model option list` are exact ids. Never shorten them, invent a friendly name, or reuse a voice from a different Model.
- `set` changes only the given task type; other bindings stay untouched. Changing its target without options starts from the new target's defaults and never carries options from the old Model.

```bash
vbot model show openrouter/microsoft/mai-voice-2
vbot task-model target list text_to_speech
vbot task-model option list text_to_speech openrouter/microsoft/mai-voice-2::api-key
vbot task-model set text_to_speech openrouter/microsoft/mai-voice-2::api-key --option voice en-us-harper:mai-voice-2
vbot task-model option set text_to_speech speed 1.1
vbot task-model option list text_to_speech
vbot task-model set text_embedding openai/text-embedding-3-small::api-key
# PowerShell: '{"some_provider_field":true}' | vbot task-model option set text_to_speech extra_options --stdin
```

### Image, video, and music settings

The fields depend on the bound Model and Connection: `option list` returns only the settings that this Model accepts on this Connection, with their exact names and choices. Read the names from `option list` instead of assuming them.

- Image generation: for example the image count `n`, `quality`, and `size` or `aspect_ratio` and `resolution`. The `generate_image` Tool can override `aspect_ratio`, `resolution`, and `background` per call; the stored value applies when a call omits them. Image count and quality are set only here.
- Video generation: `duration` in seconds, `aspect_ratio`, `resolution` or `size`, `generate_audio`, and `seed`, as the Model offers them. The `generate_video` Tool can override `duration`, `aspect_ratio`, `resolution`, and `generate_audio` per call.
- Music generation: `temperature`, `top_p`, and `seed`, as the Model offers them. An unset field uses the Provider's default.

```bash
vbot task-model option list image_generation
vbot task-model option set image_generation quality high
vbot task-model option set image_generation n 2
vbot task-model option set video_generation duration 8
vbot task-model option set video_generation generate_audio false
vbot task-model option unset video_generation resolution
```
