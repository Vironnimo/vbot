# Agent Secrets

Task-gated reference for Agent Secrets (GLOSSARY -> Agent Secret): records and lifetimes, grants and Sub-Agent pass-through, references and sinks, masking, Secret Sources, Secret requests, `/secret`, the `agent_secrets.*` RPCs and events, recent uses and Channel message removal. The domain boundary and the interfaces other code relies on are in `../secrets.md` -> Overview and Interfaces -> Agent Secrets; why each Agent-facing sentence exists: `secrets/agent-facing-text.md`.

## Terms

### Secret reference
`{{secret:NAME[.field]}}`: what an Agent writes where a value belongs and what masking shows instead of the value; outside a sink it stays literal text.

### Sink
A class of Tool arguments where vBot substitutes references (`environment`, `terminal`, `files`, `typing`, `external_tools`); each Agent Secret lists its allowed sinks.

### Secret Source
A password manager an Extension contributes (`SecretSource`) that an Agent Secret can be bound to; it supplies values, never grants. Not an Extension's own credential (`set_secret`).

### Secret request
An Agent's `secrets` `request`: a form the user answers or declines within 10 minutes.

## Source

- `core/secrets/agent_secrets.py` (`AgentSecrets`: records, grants, resolution, substitution, shell environment, masker, sources, recent uses), `_agent_secret_model.py` (record model, names, kinds, default sinks, codec), `_references.py` (reference grammar, `SecretMasker`), `_totp.py`, `sources.py` (`SecretSource`), `requests.py` (`SecretRequests`), `commands.py` (`/secret` grammar).
- Users: `core/tools/tools.py` (substitution in dispatch), `core/tools/shell.py` and `_shell_arguments.py` (environment), `core/tools/secrets.py` (the `secrets` Tool and `tool:secrets` block), `core/chat/_tool_registry.py`, `content_blocks.py`, `wire_shaping.py` and `core/compaction/compaction.py` (masking), `core/chat/_command_secret.py` and `secret_giving.py` (`/secret`, `agent_secrets.give`), `core/subagents/subagents.py` (pass-through), `core/archive/archive.py` (removal), `server/rpc/agent_secret_methods.py`, `server/app.py::_publish_secret_changes`, `core/channels/engine.py` (message removal).
- Tests: `tests/core/secrets/test_agent_secrets.py`, `tests/core/tools/test_secrets_tool.py`, `test_tools_registry.py` (sinks), `test_shell.py` and `test_shell_arguments.py`, `tests/core/chat/test_tool_dispatch.py`, `test_wire_shaping_requests.py`, `test_commands.py`, `tests/core/subagents/test_subagents.py`, `tests/server/rpc/test_agent_secret_methods.py`, `tests/core/channels/test_engine_commands.py`.

## Records

- One sealed JSON record per Secret in the `agent_secrets` section of `secrets.json`, keyed by its Secret id (`sec_` plus 12 characters): `v` (1), `name`, `kind`, `description`, `fields` (`{name: {concealed, value?, source_field?}}`), `lifetime`, `session_id?`, `expires_at?`, `agents`, `sessions`, `sinks`, `source?` (`{source, reference}`), `created_at`, `created_by` (`user`, `agent:<id>`). Metadata and values are sealed together, so entry names reveal only ids. Unknown record fields are kept on rewrite; an unreadable record is skipped with a WARNING when the records load.
- `AgentSecrets` loads every record at Runtime start (`reload`, then `sweep`) and serves reads from memory, so masking and listing do no I/O. Every write goes through `SecretStore.update` and its lock; callers run writes and source calls off the Event Loop.
- Names match `^[A-Z][A-Z0-9_]{0,63}$` and are normalized from what callers write (`api-key` -> `API_KEY`), so one name works in a reference and as a shell variable. A name may not be a data-directory credential name. It is unique among agent-wide Secrets and within one Session; a Session Secret may share an agent-wide Secret's name and then wins in its Session (`usable`).
- Kinds: `token` and `text` have one concealed `value`; `login` has `username` (visible), `password` (concealed, required), `url` (visible) and `totp` (concealed). `username`, `url` and `totp` are optional. Callers may add fields (`^[a-z][a-z0-9_]{0,31}$`, concealed unless set otherwise). A value has at most 65,536 characters and no NUL. A `totp` field holds a Base32 seed or an `otpauth://totp` URI and resolves to the current RFC 6238 code.
- Concealment is per field. A visible field's value appears in `describe`, the `secrets` Tool's lines and the prompt block; a concealed one only as its reference. Usernames are therefore visible to the Agent unless the user conceals them.
- `sinks` lists the sink classes a Secret may be used in. Defaults by kind: `token` -> `environment`, `files`; `login` -> `environment`, `terminal`, `typing`; `text` -> `environment`, `terminal`, `files`, `typing`. `external_tools` is never a default: values go to MCP servers only after the user allows it.

## Lifetimes and grants

- `permanent` and `until` Secrets are agent-wide: `agents` names the Agents, by Agent id, that may use them, and `until` adds `expires_at`. An expiry given for a `permanent` Secret makes it `until`; clearing it makes it `permanent` again. An expired Secret is hidden at once and removed by `sweep` (Runtime start and every `agent_secrets.list`).
- `session` and `once` Secrets belong to one Session (`session_id`) and to the Agent that got them there (`agents`; records without one serve any Agent of that Session), so a Session that `/agent` moves to another Agent leaves them behind. A grant change is refused until the user makes the Secret permanent (which clears its Session). A `once` Secret is deleted when the call that uses it runs, through a sink argument or `env_keys`: the dispatcher and the shell substitute with `consume=False` and call `consume` once every reference of the call resolved, so a call refused for another argument keeps it. Consumption validates every resolved record revision under one lock and removes all one-time records in one store transaction: a concurrent use, revocation, expiry or replacement refuses the whole call before its handler runs. One call naming it twice resolves it once.
- `sessions` holds the Sessions a Secret was passed into: by pass-through, and for a `permanent` or `until` Secret created with a Session (an answered request, `/secret`, `generate`) that Session.
- `granted_to(agent_id, session_id, passed_only=...)`: the Session is in `sessions`, or, for `session` and `once`, it is the Secret's Session and its Agent, or, for agent-wide Secrets and only outside a Sub-Agent Session, the Agent id is in `agents`.
- A Sub-Agent Session gets only explicit pass-through (user decision, 2026-10-21), also when the Sub-Agent runs its Parent's own Agent: it uses the Secrets bound to it or passed into it, never the grants to its Agent. Every use takes `passed_only=` from `ToolContext.subagent_session` (the Run's `subagent_session`, carried by `ToolDispatchContext` and `ToolExecutionConfig`): `usable`, `lookup`, `passable`, `pass_to_session`, `resolve`, `substitute`, the shell environment and `SecretRequests.request`; `give_secret` checks `is_subagent_session` on a worker. There a Secret that exists but was not passed fails `not_granted`, telling the Agent to ask its Parent Agent in its report. Because of the Session in `sessions`, a Sub-Agent can use what it requested, was given or generated.
- Secrets travel through the `subagent` `run` argument `secrets` (names of the caller's usable Secrets, a list or one comma-separated string). The coordinator checks them with `passable` before any Session work (`secret_unavailable`, nothing started), then `pass_to_session` adds the new Session to each record's `sessions` and a note (`SUBAGENT_SECRETS_NOTE_TEMPLATE`) lists them in that Session.
- An archive (deleting a Session, Agent or Project) calls `remove_session` for each archived Session: its own `session` and `once` Secrets are deleted and grants passed to it are withdrawn, each as a change for the listeners. A restore does not bring them back.
- Observed: `agents` holds `SessionAddress.agent_id` without its Project, so a grant to an Agent id also reaches a Project Agent of the same id.

## References and sinks

- Grammar (`REFERENCE_PATTERN`): `{{secret:NAME}}` or `{{secret:NAME.field}}`, spaces inside the braces tolerated, name and field case-insensitive. Without a field it means the only field, else `password`, else `value`. The same text is the mask, so masked text written back through a sink restores the value.
- A Tool declares sinks with `secret_sinks` (root argument -> sink class) in `ToolRegistry.register`, `api.register_tool` or a live Extension catalog declaration (API 17). After argument normalization and validation, dispatch replaces references in those arguments, nested strings included, on the handler's copy only, on a Tool worker because a source may block. The persisted call, its events and its display keep the references. A reference in any other argument stays literal text.
- A reference that cannot be used here (`unknown_secret`, `not_granted`, `unknown_field`, `sink_not_allowed`, `source_failed`; `SecretUseError`) fails the call as `secret_unavailable` with that message, and nothing runs. Each used field is recorded on the display as `secrets_used` (`NAME.field (sink)`, never a value).

| Sink class | Declared by |
|---|---|
| `environment` | the shell Tool itself, without a declaration (below) |
| `terminal` | `terminal` `text` and `data` |
| `files` | `write` `content`, `edit` `edits` (`old_string` too, so an edit around a masked line still matches), `apply_patch` `patch` |
| `typing` | Computer Use `computer` `text` and `computer_batch` `actions` |
| `external_tools` | the MCP connection Tool's `arguments` and every root parameter of a remote MCP Tool |

Shell environment (`core/tools/_shell_arguments.py`, `shell.py`):

- `env_keys` accepts the caller's Secret names and field variables (`environment_names`): a Secret name sets every field, as `NAME` for a one-field Secret and `NAME_<FIELD>` otherwise; one field's variable (`GITHUB_PASSWORD`) sets that field. A granted credential keeps precedence over a Secret name.
- An `env` value that is exactly one reference becomes that value.
- A reference in `command`, or inside an `env` value with other text, refuses the call before it runs (`invalid_arguments`, naming both ways): command lines reach process listings and shell history.
- Resolution runs on a worker after the call is parsed; a refusal is `secret_unavailable`. A granted credential a command received through `env_keys` is remembered for masking.

## Masking

- `AgentSecrets.masker` is one `SecretMasker`, rebuilt after any change, over every known value: each stored concealed field value of every Secret (other Agents' Secrets included; visible fields such as a username or URL stay readable), values a Secret Source delivered in this process, and system credential values: the data-directory credentials (Runtime start and every credential reload, `publish_credentials`) and credentials a shell command received, masked as `{{secret:<ENV_NAME>}}`. Retired built-in concealed values remain in the process masking set after consumption, replacement, deletion, expiry or reload; ending a grant does not expose output still in flight. Like resolved source values, this retention is in memory only. A reference to such a name fails `not_granted`: it is a vBot credential.
- Each value is also matched as its CRLF form, each line of at least 20 characters of a multi-line value, its JSON-escaped and URL-encoded forms and its standard and URL-safe Base64 fragments at the three byte offsets. Values and variants shorter than 8 characters (`MIN_MASKED_LENGTH`) are not masked; `/secret` warns about such a value.
- Masking points:
  - every Tool result and its display payload, before persistence, events and the Model (`core/chat/_tool_registry.py`), failure results of hook or bookkeeping exceptions included;
  - user input before it persists: typed text, audio transcriptions and `@`-mention snapshots (`content_blocks.masked_content`) of new and steered input, an ExternalRun's user text (Live calls), and a temporary Agent's initial input (`ChatLoop.mask_user_input`);
  - every Chat Model request: `model_facing_request` masks `content`, `reasoning`, Tool result content blocks and Tool Call arguments of every message, System messages included; media and `reasoning_meta` stay unchanged;
  - every Compaction Model request (`CompactionService(secret_masker=...)`, the same projection).
- Persisted history is never rewritten: a value that entered history before vBot knew it stays there, and every later request masks it.

## Secret Sources

- `SecretSource` (`core/secrets/sources.py`): `name` (lowercase letters, digits and hyphens, starting with a letter, at most 40), `label`, `unlock_fields`, `capabilities`, `status()` (`available`, `locked` or `unavailable`, with a reason), `containers()`, `entries(container)` (never a concealed value), `resolve(reference, field)`, `unlock(inputs)`, `lock()` and `store(entry)`. Methods run on worker threads; values are fetched per call and never cached on disk. A source never decides who may use a value: grants, lifetimes, sinks and masking stay with `AgentSecrets`.
- An Extension declares one with `api.register_secret_source(source)` (API 17, shape checked at registration). Runtime offers the sources of the loaded Extensions after each Extension load, reload and disable (`Runtime._refresh_secret_sources` -> `replace_sources`); the first declarer of a name wins and later ones are diagnosed on their Extension record. A source that goes away is locked first; a source not offered reads `unavailable` (`its Extension is not enabled`).
- A bound Secret (`agent_secrets.create` with `source`, `source_reference` and `source_fields`, mapping vBot field names to the source's) stores no values: each use resolves the field from the source, and a delivered value is remembered for masking in this process. Its values change only in the source: `update` refuses values, a `secrets` request for its name is refused (`secret_from_source`), and `/secret` or `give` never replaces them.
- Only single entries are bound. Binding a whole container (group, vault) is not implemented, although `containers()` exists and `source_entries` lists them. vBot never calls `store` yet: `generate` stores into vBot's own store. No bundled Extension declares a source yet (KeePassXC and 1Password are the intended ones).

## Secret requests and generate

- `secrets` `request` asks the user: on a Channel reply surface it answers at once, requesting nothing, with the `/secret` command the user should send (`secret_command_example`). It waits when the Run is attended: reply surface `webui` or `tui`, or no reply surface and `RunKind` `user` or `live`. Otherwise it fails with `no_user`.
- `SecretRequests.request` holds the pending request in memory and waits up to `REQUEST_TIMEOUT_SECONDS` (600) for `stored`, `declined` or `expired`; a restart ends waiting calls. A second request for the same name in the same Session replaces the first, which ends `declined`. A request for a name the Agent already has replaces that Secret's values and keeps its kind.
- The user answers through `agent_secrets.answer` (values, optionally another lifetime or an expiry: the Agent only suggests `session`, `permanent` or `once`), `agent_secrets.decline`, or `/secret` or `agent_secrets.give` with the same name in that Session. The pending request's `describe()` gives the form (fields with `concealed` and `optional`). An answered new Secret is bound to the Session, or granted to the requesting Agent and passed into the Session. The value never passes through the Agent, the Run or the Session history.
- `generate` stores a random password (8 to 128 characters, default 24, letters and digits plus `symbols`, `""` for none, each group at least once) as a `permanent` login granted to the calling Agent and passed into the calling Session (actor `agent:<id>`), with an optional `username`. It refuses a name the Agent already has (`secret_exists`).

## How the Agent learns its Secrets

- The `tool:secrets` System Prompt block (`## Secrets`) lists the agent-wide Secrets granted to the Agent as a `BlockCatalog` (title `Secrets`, one entry per Secret name, frame the block head), so a change within a prompt epoch reaches the Model per Secret (`chat/request-building.md` -> Dynamic prompt blocks per prompt epoch). It renders empty in a Sub-Agent Session.
- Session Secrets arrive as notes: `give_secret`'s note for a new Session Secret or a replaced value, and the pass-through note. An answered request is told by its Tool result. `secrets` `list` shows every usable Secret, with a hint when its source is not available. In a Sub-Agent Session, which has no System Prompt block, a `permanent` or `until` Secret given with `/secret` gets the Session Secret note too.

## `/secret`

- Spec: optional argument, `notice`, `immediate` (a Run may be waiting in a request it answers), `create_session`, `group_conversations=False`, `secret_argument=True` (the argument can carry a value).
- Grammar (`core/secrets/commands.py`): `/secret NAME [options] VALUE`. Options before the value: `--permanent`, `--for <n>m|h|d|w` (`until`), `--once`, `--kind token|login|text`, `--user NAME` (implies `login`), `--description TEXT` or a quoted text; `--` ends the options. Everything after the options is the value, spaces included. The lifetime defaults to `session`; the kind to `text` for a multi-line value, else `token`. A bare `/secret` returns the usage, outside Channels with the hint that `/secret` alone opens a form.
- It starts no Run and leaves no user message. `give_secret`, shared with `agent_secrets.give`, answers a pending request of that name in the Session; else it replaces the values of the Agent's usable store Secret of that name, whatever its lifetime; else it creates a Secret bound to the Session, or granted to the Agent and passed into the Session. Feedback names the Secret, for a new one with the reference the Agent sees.

## Channels

For a Command with `secret_argument` and a non-empty argument, the engine calls `ConversationTransport.delete_message` before it executes or refuses the Command, so the group refusal of `/secret` removes the message too. Telegram deletes in private chats and in groups where the bot is admin; Discord only in servers where the bot has Manage Messages, never in direct messages; Slack, Mattermost and WhatsApp cannot. A failed deletion counts as not removed (WARNING with the error type only). A removed message is no longer the reply target, and the reply appends:

- removed in a group: `vBot removed your message; change the value if someone may have read it.`
- not removed in a direct chat: `<Platform> keeps your message with the value in this chat, and vBot cannot remove it there: delete it yourself.`
- not removed in a group: `Everyone in this chat can read the value you sent: delete the message and change the value.`

## RPCs and events

- `agent_secrets.list|create|update|delete|reveal|answer|give|decline|source_unlock|source_lock|source_entries` (`server/rpc/agent_secret_methods.py`), distinct from the credential RPCs `secrets.*` (`secrets/credential-rpcs.md`).
- Values enter through `create`, `update`, `answer` and `give`. Only `reveal` returns a concealed value: a stored field as stored (a `totp` field's seed, not a code), a bound field from its source; it logs the Secret and field name at INFO. Other results describe Secrets as `describe()` does plus `recent_uses`; `source_entries` returns only non-concealed field values. Refusals are `invalid_request` and name fields, never values.
- `list` returns `{secrets, sources, requests, kinds, lifetimes, sinks}` and sweeps expired Secrets first. `create` defaults the lifetime to `permanent`, `give` to `session`.
- `list`, `create`, `update`, `delete`, `reveal` and the three source RPCs run in order on one worker under `_agent_secrets_mutation_lock`; `answer`, `give` and `decline` do not, so an answer never waits behind a slow source call.
- Unscoped `resource_changed(kind="agent_secrets")` follows every `AgentSecrets` create, update, delete, expiry, pass-through and use of a `once` Secret, and every request that appears or ends (listeners wired by `_publish_secret_changes`); `source_unlock` and `source_lock` publish it themselves, because a source's lock state has no listener.

## Recent uses

`recent_uses(secret_id)` keeps the last 20 uses per Secret, substitutions and shell environment alike (field, sink, Agent id, Session id, time; never a value), in memory since process start; RPC results carry them as `recent_uses`. Each use also logs at DEBUG.
