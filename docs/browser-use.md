# Browser automation

Browser automation ships as the `playwright-cli` Skill under `resources/skills/playwright-cli/`. It runs the official Playwright CLI through the existing `bash` Tool. There is no bundled `browser` Tool or Browser Use Extension, so browser automation adds no Tool schema to Provider requests.

## Setup and use

Allow the Agent to use `playwright-cli` in its Skills and `bash` in its Tools. Normal Project and Skill policies apply. Activate it with `$playwright-cli` or the `skill` Tool. Only its name and description enter the Skill catalog; the main instructions load on activation and reference files load on demand.

On the server host, install Node.js 18 or newer and npm, then run `npm install -g @playwright/cli@latest`. This bundled snapshot accompanies CLI 0.1.19. Follow the CLI's browser installation diagnostics for the selected browser and host. The vBot Installer no longer installs Chromium unconditionally. Neither Node, the CLI nor browser binaries are bundled Python dependencies or downloaded at vBot startup. Missing executables do not hide the Skill, so the Agent can read its setup instructions.

CLI commands and files belong to the server host. Headed windows and the annotation dashboard require a graphical display there; a remote Desktop Client does not relocate them. Use a unique named browser session per task and retain it across calls. Close only that session or detach from an attached browser. Outputs may link to snapshot, log, screenshot, trace, and video files; these must be read or presented explicitly.

The package includes the upstream references for Playwright test debugging, custom Playwright code, request mocking, storage, browser sessions, test generation, traces, video, and element attributes. Browser/platform support is determined by the installed Playwright version and host; it is not guaranteed merely by loading the Skill.

## Upstream and updates

Source: [microsoft/playwright-cli](https://github.com/microsoft/playwright-cli/tree/655530f6d0dc71a0d6bf46ae165877d3c7311099/skills/playwright-cli), retrieved 2026-09-10. The repository and npm registry both report 0.1.19. `UPSTREAM.json` records the exact revision, original file hashes, and local change. The nine reference files are unmodified; `SKILL.md` adds only the vBot execution/session/file-handling introduction. The Apache-2.0 license is included alongside the Skill.

To update, fetch the official package at a fixed revision, review the complete Agent-facing wording, retain or revise the vBot introduction, update provenance and notices, and verify every linked reference through Skill activation. Do not silently update runtime instructions at startup.

## Archived Browser Use

`archive/browser-use.zip` preserves the former Extension, its Skill, focused tests, old documentation/domain map, and developer probes at their original repository paths. Files were byte-verified against the source before removal. The archive is outside all bundled discovery roots, is excluded from source distributions, and contains no registered Tool. Repository clones retain it for development; the WebUI asset and wheel do not include it.

To restore for development, extract the Extension and focused tests to their original paths in a worktree. The saved probes, installer and docs are historical context: reconcile them with current files rather than overwriting those shared files wholesale. The archived code is frozen and does not receive current API maintenance.

Existing stored Browser Use settings, grants, downloaded clients, profiles, and artifacts are left untouched. A running server must reload Extensions or restart to retire code already loaded in memory. Old Session history and pinned Skill catalogs may still mention Browser Use until Compaction; live activation cannot load the removed package.

## Verification of this snapshot

The scoped backend gate passes 397 tests (6 host-dependent skips), including
bundled startup, absence of the old Extension/Tool/Skill, activation, every
reference-file read, upstream content hashes with LF-normalized line endings,
and the optional-browser Installer boundary. The archive's 13 files were
byte-compared before their active copies were removed.

A disposable Windows Chrome fixture using CLI 0.1.19 verified named-session
launch/close, locator-based fill/click, the resulting page value, console warning,
HTTP 503 request evidence, scoped snapshots, a trace containing the actions, and
PNG screenshot bytes. This was not a run of the repository's E2E suite and does
not establish Linux/browser-matrix support. One `npx` version probe hit a Windows
libuv shutdown assertion after printing the version; direct execution of the
same downloaded CLI completed all fixture calls successfully.

With `o200k_base`, adding this Skill to the existing catalog costs 39 tokens.
Its complete standalone catalog block is 62 tokens, body activation is 3,282,
and activation with reference-file guidance is 3,382. References load separately
on demand. The replacement registers no additional Tool schema.
