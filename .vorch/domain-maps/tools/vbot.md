# vbot Tool

The built-in App Agent uses the installed CLI on the serving instance. Chat grants its private `vbot` Tool from the resolved Agent profile. Registration excludes it from ordinary Agent catalogs. Dispatch resolves the executing Session's Agent and checks the built-in profile as well as the Session Grant. A restricted Permission Boundary protects vBot's own CLI as it does for the shell Tool.

`core/tools/vbot.py` accepts argument strings and optional text for standard input. A leading `vbot` is optional. The runner invokes the application's CLI directly, so each value stays one argument, including quotes, spaces and shell punctuation. The Runtime supplies the data directory and active listen address. The CLI owns syntax, credentials, operations and their normal validation. A terminating command returns its exit code and captured output. Nonzero exits report the CLI diagnostic as a Tool failure. The normal Run identity and the update handoff identify the Agent and its Session for commands that use them.

The command contract and the reference files that teach it belong to `../cli.md`. `tests/core/tools/test_vbot.py` covers private dispatch, Session authority, argv, stdin, output and the update handoff.

## Agent-facing text

| Wording | Reason |
|---|---|
| `Run a vBot CLI command on this instance. Use the vbot-docs Skill or pass --help for command syntax.` | Tells the Agent where its command acts and how to discover syntax (F1). |
| `Arguments after vbot, one string per argument, such as ["config", "get", "server.timezone"].` | Shows how a familiar CLI command maps to the Tool's input (F2). |
| `Text for commands that read standard input.` | Supports commands whose input is a document or a multiline value (F2). |
| `The vbot Tool is available only to the App Agent.` | Identifies the Session that can perform the requested operation (F5). |
| `The local vBot server has no active port.` | Names the unavailable runtime dependency before a command can start (F5). |
| `vbot exited with code …` followed by CLI output | Preserves the command owner's diagnosis and recovery instructions (F4). |
