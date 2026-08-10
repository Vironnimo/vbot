---
name: browser-use
description: Drive an isolated browser through the bundled Python wrapper around agent-browser. Use when a task requires interactive navigation, accessibility snapshots, clicking, filling forms, keyboard input, scrolling, or visual page inspection that web search and web fetch cannot complete.
metadata:
  vbot:
    requirements:
      binary: agent-browser
---

# Browser Use

Use the bundled Python script to control one isolated agent-browser session on the machine running the vBot server. This is a script-backed prototype, not a built-in Tool. On a remote Pi-server plus Windows Desktop Client setup it controls a browser on the Pi, not the user's Windows browser.

## Installation

### Windows PowerShell

1. Run `node --version` and `npm --version`. If either command is missing, install the current Node.js LTS release first, reopen PowerShell, and verify both commands before continuing.
2. Run `npm install -g agent-browser`.
3. Run `agent-browser install` to download Chrome for Testing.
4. Run `agent-browser --version`.
5. Run `python {baseDir}/scripts/browser_use.py --session install-check doctor` and require `"ok": true`.
6. Run `python {baseDir}/scripts/browser_use.py --session install-check start about:blank`, require `"ok": true`, then run `python {baseDir}/scripts/browser_use.py --session install-check close`.

### Linux

1. Run `node --version` and `npm --version`. If either command is missing, install a supported Node.js release through the host's approved package or version manager, then verify both commands.
2. Run `npm install -g agent-browser`. A user-local Node/npm setup provides a writable global npm prefix when needed.
3. On Debian, Ubuntu, Raspberry Pi OS, and compatible containers, run `agent-browser install --with-deps` to install Chrome for Testing plus required system libraries.
4. On other distributions, run `agent-browser install`; if launch verification fails, use `agent-browser doctor --json` to identify missing libraries.
5. Run `agent-browser --version`.
6. Run `python {baseDir}/scripts/browser_use.py --session install-check doctor` and require `"ok": true`.
7. Run `python {baseDir}/scripts/browser_use.py --session install-check start about:blank`, require `"ok": true`, then run `python {baseDir}/scripts/browser_use.py --session install-check close`.

The native agent-browser binary supports Windows x64 and Linux x64/ARM64. Keep `agent-browser` discoverable on the vBot server's `PATH` so vBot requirement checks and later Runs see the installation.

## Script contract

- Run `python {baseDir}/scripts/browser_use.py --session <session> doctor` to verify backend availability.
- Choose a unique lowercase session name using letters, digits, hyphens, or underscores. Reuse it for the complete task so tabs, cookies, and page state survive between script calls.
- Treat stdout JSON as data. Exit code zero with `"ok": true` means the operation completed. A nonzero exit or `"ok": false` means it did not.
- Screenshots and oversized snapshots are written below `tmp/browser-use/<session>/` in the effective cwd. Load screenshot paths with vBot's `read` Tool before reasoning about pixels.
- The wrapper invokes `agent-browser` without a shell, removes credential-like environment variables from its child environment, and enables agent-browser's page-content boundary markers.

## Workflow

1. Start the isolated browser with `start`. Use `--headed` for a visible browser window.
2. Read the returned interactive accessibility snapshot. Elements have refs such as `@e12`; use those exact refs for `click` and `fill`.
3. Prefer `fill` for form fields. Use `type` for the focused editable element.
4. Every state-changing command returns a fresh snapshot. Inspect it before choosing the next action and refresh refs after navigation or a substantial page update.
5. Use `screenshot` for layout, canvas content, visual state, or missing accessibility information. Call `read` on the returned image path.
6. Close the session when the task is complete.

## Commands

```text
python {baseDir}/scripts/browser_use.py --session <session> doctor
python {baseDir}/scripts/browser_use.py --session <session> start https://example.com
python {baseDir}/scripts/browser_use.py --session <session> navigate https://example.com/account
python {baseDir}/scripts/browser_use.py --session <session> snapshot --full
python {baseDir}/scripts/browser_use.py --session <session> snapshot --boxes
python {baseDir}/scripts/browser_use.py --session <session> click @e12
python {baseDir}/scripts/browser_use.py --session <session> fill @e7 "search text" --submit
python {baseDir}/scripts/browser_use.py --session <session> type "text for the focused field"
python {baseDir}/scripts/browser_use.py --session <session> press Enter
python {baseDir}/scripts/browser_use.py --session <session> scroll down --amount 600
python {baseDir}/scripts/browser_use.py --session <session> back
python {baseDir}/scripts/browser_use.py --session <session> screenshot --full-page
python {baseDir}/scripts/browser_use.py --session <session> close
```

`start` accepts `--engine chrome|lightpanda` and `--headed`.
