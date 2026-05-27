# Doctor Config Validator Plan

Goal: add a central JSON validation layer for user-editable runtime
configuration, enforce it when files are read, and expose it through `vbot
doctor` so agents can preflight a data directory before normal runtime paths use
the files.

## Checklist

- [x] Move raw JSON diagnostics into a dedicated central validation module.
- [x] Keep `settings.json` validation there instead of growing `settings.py`.
- [x] Add validators for `agent.json`, `channels/*/channel.json`, and `cron/jobs.json`.
- [x] Enforce validation at read time for settings, agents, channels, and cron.
- [x] Add `vbot doctor config` for the full data-dir config bundle.
- [x] Keep `vbot doctor settings` for the focused settings-only check.
- [x] Print explicit success/failure output with file/path diagnostics for agent callers.
- [x] Cover valid, missing, invalid JSON, wrong root type, and invalid field cases with tests.
- [x] Update project specs/docs and the `vbot-cli` skill.
- [x] Run quality gates and commit the finished slice.