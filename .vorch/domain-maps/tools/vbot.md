# vbot Tool

Chat grants the `vbot` Tool when the Agent's effective Skill selection includes the available, bundled `vbot-docs` package. A same-named package from another source cannot confer the grant. The built-in App Agent also receives the Tool through its profile. Dispatch checks the current Agent, Tool access, Skill selection and Permission Boundary. A restricted App Agent needs the bundled Skill as well.

`core/tools/vbot.py` accepts argument strings and optional text for standard input. A leading `vbot` is optional. The runner invokes the application's CLI directly, so each value stays one argument, including quotes, spaces and shell punctuation. The Runtime supplies the data directory and active listen address. The CLI owns syntax, credentials, operations and their normal validation. A terminating command returns its exit code and captured output. Nonzero exits report the CLI diagnostic as a Tool failure. The normal Run identity and the update handoff identify the Agent and its Session for commands that use them.

A restricted Run uses the Tool directly. The bound CLI parses the command before execution. Changes to any Agent's Permission Rules, Tool or Skill access, delegation or working scope, including Project overrides and shared permission defaults, request one-time user approval. This also covers saving or deleting a shared Permission Profile and assigning or clearing one on an Agent. A restricted Run supplies profile rules through stdin. The approval binds the original arguments and stdin and displays the concrete profile rules reviewed for this change. Dispatch rechecks the Agent's authority and sends the reviewed snapshot to the CLI with the original input. A profile change that no longer matches the review requires a fresh call. Other vBot operations run with their normal validation. Host files used as command inputs are subject to the Run's path permissions. For Extension operations, the parsed live schema identifies every document supplied as a file. The CLI opens the reviewed targets only after the whole set passes the Run's path checks. Documents supplied through stdin are content. A full-access Run needs no access-change approval. Restricted shell access to vBot's CLI and server remains protected.

The command contract and the reference files that teach it belong to `../cli.md`. `tests/core/tools/test_vbot.py` and `tests/cli/test_app_agent.py` cover dispatch, review, argument classification, output and the update handoff.

## Agent-facing text

| Wording | Reason |
|---|---|
| `Run a vBot CLI command on this instance. Use the vbot-docs Skill or pass --help for command syntax.` | Tells the Agent where its command acts and how to discover syntax (F1). |
| `Arguments after vbot, one string per argument, such as ["config", "get", "server.timezone"].` | Shows how a familiar CLI command maps to the Tool's input (F2). |
| `Text for commands that read standard input.` | Supports commands whose input is a document or a multiline value (F2). |
| `The vbot Tool is unavailable to this Agent.` / `This Agent needs access to the bundled vbot-docs Skill.` | Identifies the missing Tool or Skill grant, including a grant withdrawn during review (F5). |
| `This command changes Agent access.` / `The access change was not approved.` | Identifies the user's decision and the outcome of a refusal (F4). |
| `No documents were read. The reviewed file paths could not be confirmed. Call the Tool again.` | The live schema or resolved paths changed before the files could be opened, so the Agent needs a fresh review (F4). |
| `The local vBot server has no active port.` | Names the unavailable runtime dependency before a command can start (F5). |
| `vbot exited with code …` followed by CLI output | Preserves the command owner's diagnosis and recovery instructions (F4). |
