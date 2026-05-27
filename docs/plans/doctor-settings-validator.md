# Doctor Settings Validator Plan

Goal: add a `vbot doctor` surface that validates user-editable runtime
configuration before vBot uses it. First scope is `settings.json`; the command
shape should leave room for agents, channels, cron, and provider checks later.

## Checklist

- [x] Add a pure raw `settings.json` validator in `core/settings`.
- [x] Validate current known settings sections without writing or normalizing files.
- [x] Report precise diagnostics with path, severity, message, and optional JSON path.
- [x] Add a `vbot doctor settings` CLI command that runs locally against the target data dir.
- [x] Print explicit success and failure output for agent callers.
- [x] Cover valid, missing, invalid JSON, wrong root type, and invalid field cases with tests.
- [x] Update project specs/docs for the new doctor command.
- [x] Run quality gates and commit the finished slice.