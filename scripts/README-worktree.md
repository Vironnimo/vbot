# Worktree Workflow

This document explains how `scripts/worktree.py` is meant to be used, what it
creates, how vBot behaves inside a worktree, and what an agent or human needs to
know so the workflow stays predictable.

The short version:

- stay on `main` in the primary checkout
- create one worktree per task
- list active managed worktrees when you need orientation
- work from inside that worktree directory
- start and stop vBot from inside the worktree with normal relative commands
- delete the worktree when the task is finished

## What the script does

`scripts/worktree.py` manages parallel vBot checkouts under `.worktrees/`.

For each created worktree it does all of the following:

- creates a Git worktree under `.worktrees/<name>`
- creates a dedicated data directory at `~/.vbot-<name>`
- copies the default contents from `.data-dir-base/` into that data directory
- rewrites `agents/main/agent.json` so the main agent points at that data dir's `workspace-main`
- writes `settings.json` in that data directory with a dedicated `server_port`
- writes a `.vbot-worktree` marker into the worktree root
- installs frontend dependencies in `webui/`
- builds the frontend once during creation

The goal is that once you are inside the worktree, normal commands like
`python cli/main.py server start` or `python scripts/test-env.py start` use the
worktree's own data dir and port automatically.

## Basic model

There are three separate things involved:

1. Git worktree
2. Dedicated data directory
3. Dedicated port

For a worktree named `feature-a`, the expected layout is:

```text
.worktrees/feature-a/
~/.vbot-feature-a/
```

The main checkout remains independent and normally keeps using:

```text
repo root          -> current branch, usually main
~/.vbot            -> main data dir
8420               -> main server port
```

The first generated worktree port starts at `8421`. Additional worktrees get the
next free port.

## Normal workflow

### 1. Stay on `main` in the primary checkout

The intended setup is:

- main checkout stays on `main`
- each worktree gets its own task branch

That means you can keep spawning new worktrees from `main` without moving your
primary checkout away from `main`.

### 2. Create a worktree

From the repository root:

```bash
python scripts/worktree.py create my-task
```

Worktree names must be a single safe path segment. They may contain letters,
numbers, dots, underscores, and hyphens, and must start with a letter or number.

This creates:

- `.worktrees/my-task/`
- `~/.vbot-my-task/`
- a branch named `my-task`

It also prints the assigned port, data dir, path, and local URL.

If creation fails after Git has created the worktree, the script attempts to
clean up the partial worktree, the dedicated data dir, and the managed branch.

### 3. Enter the worktree

From the repository root:

```bash
cd .worktrees/my-task
```

From this point on, use normal relative commands inside the worktree.

Examples:

```bash
python cli/main.py server status
python cli/main.py server start
python scripts/test-env.py start
python scripts/quality.py tests/scripts/test_test_env.py
python scripts/quality-frontend.py webui/src/lib/__tests__/i18n.test.js
```

### 4. List managed worktrees

From the repository root:

```bash
python scripts/worktree.py list
```

The list output includes each marker-backed worktree's name, path, branch, data
dir, port, and whether the task branch was created by the script.

### 5. Do the work inside the worktree

Treat the worktree as its own checkout.

- edit files there
- run tests there
- run the server there
- run frontend builds there

Do not assume a process launched from the main checkout will magically operate on
the worktree. The important boundary is the current working directory.

### 6. Stop the worktree server and delete the worktree

From the repository root:

```bash
python scripts/worktree.py delete my-task
```

This deletes:

- the Git worktree at `.worktrees/my-task`
- the dedicated data dir `~/.vbot-my-task`
- the managed branch `my-task` if the script created that branch itself

On success, the command prints `status: deleted`.

If the worktree was created from an existing branch with `--from`, the existing
branch is not deleted.

## Create modes

### Default mode: create a new branch from the current HEAD

```bash
python scripts/worktree.py create my-task
```

This is the normal mode for parallel task work.

If your main checkout is on `main`, the new branch is created from `main`.

This is what you usually want when you say:

"I want to start several worktrees from main."

### Existing-branch mode: check out an already existing branch

```bash
python scripts/worktree.py create my-fix --from some-existing-branch
```

This checks out an existing branch into a new worktree instead of creating a new
branch.

Use this only when you explicitly want that exact branch.

Important:

- `--from` does not create a new branch
- `delete` does not delete that borrowed branch afterward

### Do not use `--from main` for normal parallel feature work

If your main checkout already uses `main`, then `--from main` is usually the
wrong tool for normal task branches.

For parallel work based on `main`, the intended workflow is:

```bash
python scripts/worktree.py create feature-one
python scripts/worktree.py create feature-two
python scripts/worktree.py create feature-three
```

That leaves the primary checkout on `main` and creates one new task branch per
worktree.

## How vBot detects the worktree context

Each worktree gets a marker file:

```text
.worktrees/<name>/.vbot-worktree
```

It stores at least the worktree data dir:

```json
{
  "data_dir": "~/.vbot-my-task",
  "managed_branch": true
}
```

`Config()` uses the following precedence for the default data dir:

1. `VBOT_DATA_DIR`
2. `.vbot-worktree` in the current working directory
3. repository-root `.vbot-worktree` resolved from the module path
4. `~/.vbot`

That means the worktree behavior depends on where the process is launched from.

If you are inside `.worktrees/my-task`, normal relative entrypoints should use:

- `~/.vbot-my-task`
- that worktree's assigned `server_port`

If you launch a command from the main checkout, it should use the main instance
instead.

## What a fresh agent or shell must know

A fresh agent, shell, or terminal does not automatically "know" that it should
work inside a worktree. The deciding factor is the working directory.

If an agent is supposed to work in a specific worktree, set its CWD to that
worktree first.

Good:

```bash
cd .worktrees/my-task
python scripts/test-env.py start
```

Also good:

```bash
Push-Location .worktrees/my-task
python cli/main.py server status
Pop-Location
```

Risky:

```bash
python .worktrees/my-task/scripts/test-env.py start
```

The file path points into the worktree, but the process working directory is
still the main checkout. For the worktree workflow, prefer changing into the
worktree first and then using relative commands.

Practical rule:

- if you want worktree behavior, enter the worktree first
- once inside, use normal relative commands

## Daily commands

### List managed worktrees

From the repository root:

```bash
python scripts/worktree.py list
```

### Check the local worktree server

From inside the worktree:

```bash
python cli/main.py server status
```

### Start the local worktree server

From inside the worktree:

```bash
python cli/main.py server start
```

or:

```bash
python scripts/test-env.py start
```

`scripts/test-env.py start` also rebuilds the frontend first.

### Stop the local worktree server

From inside the worktree:

```bash
python cli/main.py server stop
```

or:

```bash
python scripts/test-env.py stop
```

### Run quality checks inside the worktree

Backend:

```bash
python scripts/quality.py
```

Frontend:

```bash
python scripts/quality-frontend.py
```

Or scope them to a smaller target:

```bash
python scripts/quality.py tests/scripts/test_test_env.py
python scripts/quality-frontend.py webui/src/lib/__tests__/i18n.test.js
```

## Files generated per worktree

### `.vbot-worktree`

This is the machine-readable marker used by config and cleanup logic.

### `~/.vbot-<name>/settings.json`

This contains at least the dedicated `server_port` for that worktree.

## Delete rules and safety

`python scripts/worktree.py delete <name>` is intentionally conservative.

Important behavior:

- it resolves the data dir from the marker only when the marker matches the
  expected managed path
- it does not blindly trust arbitrary marker paths for deletion
- it deletes the branch only when the marker says the branch was script-managed
- if the worktree is dirty, delete fails unless you explicitly use `--force`

Examples:

```bash
python scripts/worktree.py delete my-task
python scripts/worktree.py delete my-task --force
```

Use `--force` only when you are sure you want to discard worktree-local changes.

## Troubleshooting

### The main checkout moved off `main`

It should not move just because you created a worktree.

To fix the main checkout:

```bash
git switch main
```

### `git worktree list` shows `prunable`

That means Git still remembers a worktree entry whose directory is already gone.

Clean it up with:

```bash
git worktree prune
```

### A command used the wrong data dir or port

Check these first:

1. Are you actually inside the worktree directory?
2. Does `.vbot-worktree` exist in that worktree root?
3. Is `VBOT_DATA_DIR` set in the environment and overriding the marker?
4. Does `~/.vbot-<name>/settings.json` contain the expected `server_port`?

### The worktree server will not start because the port is busy

The script chooses the next free port when creating the worktree. If the chosen
port becomes busy later, free the conflicting process or recreate the worktree.

### The worktree build step failed during creation

`create` runs:

```bash
npm install
npm run build
```

inside the worktree's `webui/` directory. Fix the frontend dependency or build
issue, then create the worktree again.

## Recommended team workflow

1. Keep the primary checkout on `main`.
2. Create one worktree per task with `python scripts/worktree.py create <name>`.
3. Change into that worktree before running any vBot command.
4. Use normal relative entrypoints from inside the worktree.
5. Run tests and quality scripts inside the worktree.
6. Stop the local worktree server when done.
7. Delete the worktree with `python scripts/worktree.py delete <name>`.

If you follow those rules, you can run several independent vBot instances in
parallel without sharing ports, logs, or data directories.
