# Secrets Agent-facing text

Why each sentence of the Agent-facing text the Secrets domain owns exists: the `secrets` Tool (`core/tools/secrets.py`: definition, results, errors), the `## Secrets` System Prompt block and the Secret line format it shares with results and notes, the note `/secret` and its form add to a Session (`core/chat/secret_giving.py`), and the substitution refusals of `core/secrets/agent_secrets.py`. Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text. Failure codes F1-F6: `tools.md` -> Terms.

The texts were written with the design of 2026-10-21 and have not yet been checked in real Sessions or an eval; the reasons below are the failures each sentence is meant to prevent.

## `secrets` Tool definition

| Text | Reason |
|---|---|
| Description: `Manage Secrets: passwords, API keys and other values you use by reference without seeing them.` | Names the resource and the one property every use depends on: the Agent never sees a value (F3: an Agent that expects to read the value tries to print it). |
| Description: `When a task needs such a value you do not have, request it here instead of asking the user to write it in the chat.` | Without it Agents ask for a password in their reply, and the value lands in the Session history, the Provider request and logs (F1). |
| `action`: `request shows the user a form and waits up to 10 minutes for the answer;` | The Agent must know the call blocks and is answered by the user, so it does not poll or ask again in its reply meanwhile (F4). |
| `action`: `requesting a name you have replaces its value.` | The way to repair a wrong or expired value; otherwise Agents invent a second name (F3). |
| `action`: `generate stores a new random password, for registering an account or changing a password.` | Agents otherwise make up passwords in the chat or with a shell one-liner whose output reaches the history (F1). |
| `name`: `Required for request and generate, such as GITHUB or STRIPE_API_KEY.` | Shows the form names take (upper case, underscores) so the reference the Agent writes later matches (F2). |
| `kind`: `For request: token for one key or code, login for a username and password, text for multi-line values such as an SSH key. Omit for token.` | The form the user sees follows the kind; a login asked as token loses the username (F3). |
| `description`: `For request and generate: what the Secret is for, shown to the user, ...` | The user decides whether to answer from this text; an empty form is hard to judge. The example shows a concrete purpose rather than a restated name. |
| `lifetime`: `For request: the lifetime you suggest; the user decides. session ends with this session, once ends after the first use (one-time codes). Omit for session.` | User decision: `/secret` and requests default to this session. `the user decides` keeps the Agent from treating its suggestion as settled; `once` names its one use case so it is not used for ordinary keys. |
| `username`: `For generate: the account's username or email. Omit if it has none.` | A generated login without its username cannot be used for a sign-in later. |
| `length`: `For generate.` (`default` 24) | Scope only; the number and its default are in the schema. |
| `symbols`: `For generate: the symbols the password can contain; "" for letters and digits only. Omit to allow common ASCII symbols.` | Many sign-up forms restrict symbols; the Agent needs the value that turns them off and a reason to leave the field out otherwise (F2, F3). |
| Summary: `Use passwords and API keys without seeing them.` | Lets an Agent that loads Tools on demand pick `secrets` when a task needs a credential. |

## `## Secrets` block and the Secret line

| Text | Reason |
|---|---|
| Block, shown only while the Agent has Secrets granted to it and never in a Sub-Agent Session: `## Secrets` + `You can use these Secrets without seeing their values.` | A Secret the Agent does not know of is never used; the block is empty (dropped) when there is none, so Agents without Secrets pay nothing. A Sub-Agent Session uses only passed Secrets (user decision 2026-10-21), which its start note lists; the Agent's list there would name Secrets it cannot use. User approval of the wording is pending (vbot-agent-text). |
| `Write a reference such as {{secret:NAME.field}} where the value belongs, in the places listed for each Secret; vBot inserts the value there.` | The reference form and the rule that it works only in the listed places (sinks); elsewhere it stays literal text (F2, F3). |
| `Tool output shows values as their references.` | Without it an Agent that sees `{{secret:X}}` in command output thinks the substitution failed and retries (F4). |
| Line: `- NAME (kind[, this session \| one use, this session \| until <time>]): <description>.` | Kind and lifetime tell the Agent which fields exist and how long it can rely on the Secret. |
| Line fields: `username julian, password {{secret:NAME.password}}` | User decision: visible fields (a username) show their value; concealed ones only their reference, which is what the Agent writes. |
| Line: `Usable in: shell env_keys ["NAME"] (NAME_USERNAME, NAME_PASSWORD), terminal input, typed text.` | The places vBot substitutes in, named by the Tool parameter the Agent fills; for the environment the exact `env_keys` value and the variables it sets, because a shell reference in the command line is refused (F2). |
| `nowhere until the user allows a use in Settings` | A Secret without sinks must not look usable. |

## Results and errors

| Text | Reason |
|---|---|
| list: `You have no Secrets. Use action request to ask the user for one.` | The empty result names the next call (F4). |
| list, request and generate results end with `Write a reference where the value belongs; vBot puts the value there, and output shows it to you as the reference.` | Results reach Agents whose System Prompt has no block (Session Secrets, Sub-Agents), so the use rule travels with the Secret. |
| Source line: `It comes from <source>, which is <state>: ask the user to unlock it in Settings -> Secrets.` | A locked password manager fails at use time; the Agent learns the remedy before it tries. |
| request stored: `The user stored <NAME>:` / `The user replaced the value of <NAME>:` + line | The outcome and the reference to use, without the value. |
| request declined: `The user declined to give <NAME>. Continue without it, or ask the user in your reply how to proceed.` | Prevents an immediate second request (F5). |
| request expired: `Nobody answered the request for <NAME> within 10 minutes, so it was withdrawn. Ask the user in your reply; they can send /secret ...` | The user may have stepped away; the command lets them answer later without a new form. |
| request on a Channel: `Nothing was requested yet: the user is on <platform>, where vBot cannot show a form. Ask them in your reply to send /secret ... as a message of its own. vBot stores it without showing you the value; you can use it from your next turn.` | Channels have no form; the exact command with the right options saves a round trip, and `next turn` keeps the Agent from waiting in this one. |
| `no_user`: `Nothing was requested: nobody can answer in this run. Say in your reply or report that you need <NAME> and what for; ...` | Scheduled and other unattended Runs would block 10 minutes for nobody. |
| `secret_from_source`: `Nothing was requested. <NAME> comes from <source>; ask the user ... to correct it there or to unlock it ...` | A source-bound value cannot be replaced through vBot. |
| `secret_exists` (generate): `Nothing was generated. <NAME> already exists, and a new password would lose the stored one. Use another name, such as <NAME>_NEW, or ask the user to delete <NAME> ...` | Generating over a stored password locks the user out of the account. |
| generate result: `Generated a N-character password and stored it as <NAME>: ... The user can see the password in Settings -> Secrets.` | The Agent can tell the user where to find a password it registered an account with. |
| Invalid action, name, kind, lifetime and length refusals (`Nothing was ... Use one of: ...`) | Three-part errors per the vbot-agent-tools Skill (F5). |
| Unknown field note: `<fields> had no effect.` + `Secrets get their values only from the user (request) or from generate.` for `value`, `password`, `secret_value` or `token` | An Agent that tries to store a value it knows learns the only two paths; the value itself is never echoed. |
| `secrets_failed`: `The <action> call failed inside vBot, and nothing was stored. Try it once more; if it fails again, tell the user.` | An unexpected exception still produces a usable error. |

## Substitution refusals (`secret_unavailable`)

Every refusal starts with `<tool> was not run:` like the other dispatch refusals (`tools/agent-facing-text.md`).

| Text | Reason |
|---|---|
| `No Secret named <NAME>. Your Secrets: <names>. Use the secrets Tool to request one from the user.` | A misspelled reference gets the valid names; a missing one gets the way to obtain it (F5). |
| `<NAME> is not granted to you. Your Secrets: ... Ask the user to grant it in Settings -> Secrets if you need it.` | User decision: grants per Agent. Names the real state instead of "unknown", so the Agent does not request a duplicate. |
| In a Sub-Agent Session: `<NAME> was not passed to you. Your Secrets: ... If you need it, say so in your report; your Parent Agent can pass it.` | User decision 2026-10-21: Sub-Agents use only passed Secrets. A Sub-Agent would otherwise ask the user for a grant that does not help it, or request a duplicate; only the Parent can pass, at the next start. |
| `<NAME> is a vBot credential, not a Secret you can use here.` | Provider keys and other data-directory credentials share the name space of environment variables; Agents guessed them as Secret names. |
| `<NAME> has no field <field>; use <references>.` | Lists the valid references (F5). |
| `<NAME> may not be used in <place> (allowed: ...). Tell the user if this use is needed; they can allow it in Settings -> Secrets.` | The user restricts sinks; the Agent must not work around the restriction, only report it. |
| `<source> did not deliver <NAME>.<field>: <error> Tell the user to unlock it ...` / `Tell the user what failed.` | A locked or failing password manager has a remedy only the user can apply. |

## Session notes

| Text | Reason |
|---|---|
| `The user gave you a Secret for this session: <line> Write its reference where the value belongs; vBot inserts the value there.` | Session Secrets are not in the System Prompt block (it is fixed per Agent until Compaction), so the Agent learns of them from a note at the moment they arrive. |
| `The user replaced the value of the Secret <NAME>.` | After a failed sign-in the Agent knows a retry can now succeed. |
| No note for a new permanent or until Secret | The block's change reminder reports it; a note as well would say it twice. |
