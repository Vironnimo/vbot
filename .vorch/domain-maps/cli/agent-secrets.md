# Agent Secret Commands

Supplementary to `cli.md`. Covers `vbot agent-secrets` (grammar `cli/_parser_agent_secrets.py`, dispatch `cli/_dispatch_agents.py`, operations `cli/agent_secret_management.py`) over the `agent_secrets.*` RPCs (`server/rpc/agent_secret_methods.py`; Secrets, lifetimes and sinks: `secrets.md`). It is not `vbot data-store secrets`, which holds the credentials vBot itself uses (`cli/data-store.md`); the help of both names the other. Alias `agent-secret`; there is no `secrets` alias, so the two cannot be confused.

## Commands

- `list`, `add <NAME>`, `update <secret>`, `delete <secret>`, `reveal <secret> [--field]`, `answer|decline <request-id>` and `source list|unlock|lock|entries <source>`.
- Every command except `list` and `source list` first reads `agent_secrets.list` and resolves its target there, before any value is read: a Secret by id or by name, normalized like the store (`normalize_secret_name`, imported lazily so help and parsing do not load `core.secrets`); a name that matches an agent-wide and a Session Secret is refused with both ids, an unknown one lists the closest names. A request resolves by id or by the name it asks for, a Source by name or label.
- `add` refuses an existing agent-wide name before asking for the value and names `update <NAME> --value-stdin`. Without `--agent` it grants the Secret to the Run's Agent (`VBOT_RUN_AGENT_ID`), outside a Run to none; `--agent` without ids grants none. `--for` and `--until` become `expires_at` (lifetime `until`). Session and once Secrets are not created here; the `secrets` Tool and `/secret` create them.
- `update` sends only the given parts and prints what changed, or that the Secret already had these settings, followed by the Secret as it is now; without a change flag it fails before any RPC. `--agent` and `--sink` replace the whole list.
- `delete` of a name no Secret has exits 0 with attention (`nothing was deleted`).
- `reveal` prints only the value on stdout: the named field, by default a login's password, else `value`; a stored TOTP field gives its seed, not a code.
- `answer` sends lifetime `until` with `--for` or `--until`, `permanent` with `--permanent`, else the request's own lifetime applies.
- `source entries` prints at most `ENTRY_LIMIT` (50) entries, filters `--query` on titles in the CLI and names the containers.

## Values

- A value comes only from a hidden prompt (`getpass`) when stdin is a terminal, or from piped stdin with `--value-stdin`; otherwise the command refuses. There is no implicit stdin read, so a command an Agent runs without input never waits for it.
- stdin layout: token and text take the whole input, a login the password on line 1 and an optional TOTP seed on line 2, `source unlock` one line per unlock field in the listed order. A token with several lines is refused with a pointer to `--kind text`. A text value is kept exactly apart from a leading BOM, other values lose trailing CR/LF, and a text Secret cannot come from a hidden prompt. In `update`, an empty line or prompt keeps that field.
- No prompt, error or output except `reveal` contains a value; a decode failure reports only that stdin is not UTF-8.

## Inside an Agent's Run

`reveal`, `answer`, `decline`, `source unlock` and an `update` that adds Agents or sinks beyond the current ones refuse while `VBOT_RUN_AGENT_ID` is set; narrowing stays allowed. The refusal names `Settings -> Secrets` and the command for the user's terminal. Like `devices approve` (`device_management.run_refusal`) this keeps a cooperative Agent from handing out access by accident; it is no security boundary. `add`, `delete`, `list`, value, description and expiry updates and `source list|lock|entries` work inside a Run.

## Parser constants

`AGENT_SECRET_KINDS` and `AGENT_SECRET_SINKS` in `cli/_parser_common.py` mirror `SECRET_KINDS` and `SINK_CLASSES` of `core.secrets`, guarded by `test_the_parser_offers_the_kinds_and_sinks_of_the_secret_store`. `--sink` takes spaces or commas, dashes and the spellings `env`, `file` and `type`, and suggests the close match for a typo; `--until` refuses a date without a time of day. Tests: `tests/cli/test_cli_agent_secrets.py`.
