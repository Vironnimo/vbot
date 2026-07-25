# Worktree Workflow

This document explains how `scripts/worktree.py` is meant to be used, what it creates, how vBot behaves inside a worktree, and what an agent or human needs to know so the workflow stays predictable.

The short version:

- stay on `main` in the primary checkout
- create one worktree per task
- list active managed worktrees when you need orientation
- work from inside that worktree directory
- start and stop the complete vBot + fake Provider test instance from inside the worktree with `scripts/test-env.py`
- delete the worktree when the task is finished

## What the script does

`scripts/worktree.py` manages parallel vBot checkouts under `.worktrees/`.

For each created worktree it does all of the following:

- creates a Git worktree under `.worktrees/<name>`
- creates a dedicated data directory at `~/.vbot-<name>`
- initializes the canonical empty data-directory structure through `core/storage/layout.py`
- writes `settings.json` in that data directory with a dedicated `server_port`, a paired local fake-Provider endpoint, and chat/fallback/image/speech fake Models
- writes a `.vbot-worktree` marker into the worktree root
- installs frontend dependencies in `webui/`
- builds the frontend once during creation

The Worktree utility does not seed an Agent or copy machine-local Workspace content. Runtime creates the bootstrap Identity Agent and first Session on the first server start. Once you are inside the worktree, `python scripts/test-env.py start` uses the Worktree's own data dir, starts its Settings-declared fake Provider, builds the WebUI, and starts vBot. Direct `python cli/main.py server start` starts only vBot and therefore leaves fake-Model calls unavailable.

## Basic model

There are four separate things involved:

1. Git worktree
2. Dedicated data directory
3. Dedicated vBot server port
4. Dedicated fake-Provider port

For a worktree named `feature-a`, the expected layout is:

```text
.worktrees/feature-a/
~/.vbot-feature-a/
```

The primary development checkout remains independent and uses:

```text
repo root          -> current branch, usually main
~/.vbot-dev        -> primary development data dir
8421               -> primary development server port
```

The separately installed application retains the product defaults `~/.vbot` and `8420`. The first generated Worktree server port starts at `8422`; its paired fake Provider uses `18422`. Additional Worktrees get the next server port whose paired fake-Provider port is also unassigned and unbound.

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

The primary checkout's local marker additionally sets `"cwd_only": true`. That scope makes the marker apply when a command is launched from the checkout itself, but not merely because an editable installation imports code from that checkout. Script-generated task-worktree markers omit the field so their module-root fallback continues to work from the installed `vbot` entrypoint.

`Config()` uses the following precedence for the default data dir:

1. `VBOT_DATA_DIR`
2. `.vbot-worktree` in the current working directory
3. repository-root `.vbot-worktree` resolved from the module path, unless it sets `cwd_only: true`
4. `~/.vbot`

That means the worktree behavior depends on where the process is launched from. The primary checkout carries a git-ignored cwd-only `.vbot-worktree` marker selecting `~/.vbot-dev`; its `settings.json` selects port `8421`. Outside that checkout, a parameterless installed CLI falls through to `~/.vbot` and port `8420`.

If you are inside `.worktrees/my-task`, normal relative entrypoints should use:

- `~/.vbot-my-task`
- that worktree's assigned `server_port`

If you launch a command from the main checkout, it uses the primary development instance at `~/.vbot-dev:8421`, not the separately installed application at `~/.vbot:8420`.

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

From inside the worktree, start the complete test instance:

```bash
python scripts/test-env.py start
```

`scripts/test-env.py start` rebuilds the frontend, starts the Settings-declared fake Provider with an owned PID under the Worktree data directory, then starts vBot. Use `python cli/main.py server start` only when intentionally testing vBot without the fake endpoint.

### Stop the local worktree server

From inside the worktree:

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

This contains the dedicated `server_port`, the keyless `providers.custom.fake` endpoint, manual fake Models for chat/fallback/image/speech, and the corresponding default/task-model bindings. Existing user values in a reused data directory are preserved; missing fixture values are filled.

### `~/.vbot-<name>/.env` and canonical directories

Worktree creation uses the same canonical initializer as Setup and Runtime. It seeds `.env` from `resources/data-dir/.env.example` only when absent, preserves pre-existing configuration, creates every canonical directory, and leaves `agents/` empty until Runtime's first start.

## Delete rules and safety

`python scripts/worktree.py delete <name>` is intentionally conservative.

Important behavior:

- it resolves the data dir from the marker only when the marker matches the
  expected managed path
- it does not blindly trust arbitrary marker paths for deletion
- it deletes the branch only when the marker says the branch was script-managed
- if the worktree is dirty, delete fails unless you explicitly use `--force`;
  the error output lists each blocking file as an `uncommitted:` line so you
  can decide whether to commit the work or discard it with `--force`

Ignored files never block a non-force delete. Build artifacts such as
`node_modules/`, `webui/dist/`, `coverage/`, plan files under `docs/plans/`,
and Vite temp config bundles (`*.timestamp-*.mjs`) are all gitignored, so a
worktree that only contains generated artifacts deletes cleanly without
`--force`.

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

### Delete reported `terminated:` or `leftover:` lines

On Windows, files inside the worktree can be locked by running processes —
the common case is an orphaned `esbuild.exe` service process left behind by a
Vite build, which blocks deletion of `webui/node_modules`. `delete` handles
this automatically:

1. Processes whose executable lives inside the worktree are terminated and
   reported as `terminated:` lines. These are always disposable build helpers;
   external programs (e.g. your editor) are never touched.
2. If files remain locked by an external process (e.g. an editor language
   server holding a native module), the stuck directory is renamed to
   `.worktrees/.trash-<name>-<timestamp>` and reported as a `leftover:` line.
   The worktree still counts as deleted; trash directories are swept
   automatically on later create/delete runs once the locks are gone.

In both cases the delete finishes: data dir and managed branch are cleaned up
and the worktree name is immediately reusable.

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
