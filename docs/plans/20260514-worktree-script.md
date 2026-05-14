# Plan: Worktree Management Script

**Goal:** A single `scripts/worktree.py` script (with `create` / `remove` subcommands) that manages git worktrees for parallel agent development, assigning each worktree a free port and a dedicated data directory.

**Context:** vBot is developed with git worktrees so multiple agents can work in parallel without sharing a server port or data directory. This script is the single entry-point for creating and removing those worktrees consistently. Agents read `.vorch/WORKTREE.md` at session start to get their concrete, baked-in commands.

**Requirements:**
- `python scripts/worktree.py create <name> [--branch <branch>]`
- `python scripts/worktree.py remove <name> [--force]`
- Port 8420 reserved for main checkout; worktrees use 8421+
- Output format: `key: value` per line, matching `cli/main.py` / `test-env.py`
- stdlib only (`subprocess`, `shutil`, `json`, `pathlib`, `argparse`, `socket`)
- Unit tests for port-assignment logic and settings.json merge logic only

**Scope:**
- In: `scripts/worktree.py`, `tests/scripts/test_worktree.py`, `.gitignore`, `AGENTS.md`
- Out: changes to `core/`, `server/`, `cli/`, `webui/`; env var support; venv activation

---

## Phases

### Phase 1: Static edits ⚡ *parallel with Phase 2*

- [ ] Edit `.gitignore` — append `.vorch/WORKTREE.md` on its own line (place it in the "Local app data" section, after `.data-dir-base/`)
  — files: [`.gitignore`]

- [ ] Edit `AGENTS.md` — in the **Session Start** section, add a step 3 immediately after step 2:
  `3. If \`.vorch/WORKTREE.md\` exists, read it immediately after \`GLOSSARY.md\`.`
  The existing step "Your agent file lists any additional files…" becomes the closing paragraph, not a numbered step — it is currently unnumbered prose, so no renumbering is needed.
  — files: [`AGENTS.md`]

---

### Phase 2: Script + unit tests ⚡ *parallel with Phase 1*

Write `scripts/worktree.py` and `tests/scripts/test_worktree.py` together.

#### scripts/worktree.py

**Module layout (top → bottom):**

```
imports (stdlib only)
constants
output helpers
port-assignment helpers
settings-merge helper
WORKTREE.md content generator
create command
remove command
argparse / main
```

---

**Constants:**

```python
MAIN_PORT = 8420
FIRST_WORKTREE_PORT = 8421
WORKTREES_DIR = PROJECT_ROOT / ".worktrees"
DATA_DIR_BASE = PROJECT_ROOT / ".data-dir-base"
WORKTREE_DOC_PATH = Path(".vorch") / "WORKTREE.md"   # relative, written inside each worktree
```

---

**Output helpers:**

```python
def print_ok(**fields: str | int | Path) -> None
    # prints each kwarg as "key: value" on its own line
    # converts Path to str

def print_error(reason: str) -> None
    # prints "error: <reason>"
```

---

**Port-assignment logic (unit-testable, no side effects):**

```python
def scan_used_ports(worktrees_dir: Path) -> set[int]:
    """
    Walk worktrees_dir / * / .vorch / WORKTREE.md.
    For each file, find the line starting with "port: " and parse the integer.
    Return the set of all found ports.
    Silently skip files that are missing or malformed.
    """

def is_port_bound(port: int) -> bool:
    """
    Try socket.create_connection(("127.0.0.1", port), timeout=0.2).
    Return True if connection succeeds (port in use), False otherwise.
    """

def find_free_port(worktrees_dir: Path, start: int = FIRST_WORKTREE_PORT) -> int:
    """
    Collect used_ports = scan_used_ports(worktrees_dir).
    Starting at `start`, increment until a port is not in used_ports AND not bound.
    Return that port.
    """
```

---

**Settings merge helper (unit-testable):**

```python
def merge_settings(settings_path: Path, updates: dict) -> None:
    """
    Read settings_path as JSON if it exists (default to {}).
    Merge `updates` into the dict (updates win on key conflict).
    Write back as indented JSON (indent=2).
    Create parent directories if missing.
    """
```

---

**WORKTREE.md content generator:**

```python
def worktree_doc_content(name: str, port: int, data_dir: Path) -> str:
    """Return the full text of the WORKTREE.md to write inside the worktree."""
```

Generated content (actual values baked in, no placeholders):

```markdown
# Worktree: <name>

port: <port>
data-dir: <data_dir>

## Commands

Start server:  python scripts/test-env.py start --port <port> --data-dir <data_dir>
Stop server:   python scripts/test-env.py stop --port <port> --data-dir <data_dir>
Backend quality: python scripts/quality.py
Frontend quality: python scripts/quality-frontend.py
CLI: python cli/main.py --port <port> --data-dir <data_dir> server <command>
```

The `port: <port>` line **must remain on line 3** (after the heading blank line) — it is the line scanned by `scan_used_ports`.

---

**`create` command — `cmd_create(args: argparse.Namespace) -> int`:**

1. If `.worktrees/<name>/` already exists → `print_error(f"worktree '{name}' already exists")` → return 1.
2. Call `find_free_port(WORKTREES_DIR)` → `port`.
3. Build `git worktree add` command:
   - With `--branch <branch>`: `git worktree add .worktrees/<name> <branch>`
   - Without: `git worktree add -b <name> .worktrees/<name>`
   - Run via `subprocess.run(cmd, check=False)`. On non-zero exit → `print_error(stderr or "git worktree add failed")` → return 1.
4. Build `data_dir = Path.home() / f".vbot-{name}"`.
5. Copy `.data-dir-base/` → `data_dir`:
   - If `DATA_DIR_BASE` exists and has any contents: `shutil.copytree(DATA_DIR_BASE, data_dir)`.
   - Otherwise: `data_dir.mkdir(parents=True, exist_ok=True)`.
   - On failure (e.g., `data_dir` already exists from a prior partial run) → `print_error(...)` → return 1.
6. Call `merge_settings(data_dir / "settings.json", {"server_port": port})`.
7. Run `npm install` in `.worktrees/<name>/webui/` via `subprocess.run(["npm", "install"], cwd=..., check=False)`. On failure → `print_error("npm install failed")` → return 1.
8. Compute `doc_path = WORKTREES_DIR / name / WORKTREE_DOC_PATH`. Create parent dirs. Write `worktree_doc_content(name, port, data_dir)`.
9. `print_ok(name=name, port=port, **{"data-dir": data_dir}, path=WORKTREES_DIR/name, url=f"http://localhost:{port}")` → return 0.

Error handling: if any step after step 3 fails, the worktree already exists on disk. Do **not** attempt rollback — print the error and return 1 with the partial state visible. The user can run `remove --force` to clean up.

---

**`remove` command — `cmd_remove(args: argparse.Namespace) -> int`:**

1. If `.worktrees/<name>/` does not exist → `print_error(f"worktree '{name}' does not exist")` → return 1.
2. Build `git worktree remove` command:
   - Without `--force`: `git worktree remove .worktrees/<name>`
   - With `--force`: `git worktree remove --force .worktrees/<name>`
   - Run via `subprocess.run(cmd, capture_output=True, text=True, check=False)`.
   - On non-zero exit without `--force`:
     - Check if stderr contains "dirty" or "modified" → `print_error("worktree has uncommitted changes, use --force to override")`.
     - Otherwise → `print_error(stderr.strip() or "git worktree remove failed")`.
     - Return 1.
   - On non-zero exit with `--force` → `print_error(...)` → return 1.
3. Delete `~/.vbot-<name>/` via `shutil.rmtree(data_dir, ignore_errors=True)` (always, even if git step succeeded partially).
4. `print_ok(name=name, path=worktree_path, **{"data-dir": data_dir}, status="removed")` → return 0.

---

**`parse_args` / `main`:**

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace: ...

def main() -> int:
    args = parse_args()
    if args.command == "create":
        return cmd_create(args)
    if args.command == "remove":
        return cmd_remove(args)
    return 1

if __name__ == "__main__":
    sys.exit(main())
```

`create` subparser: positional `name`, optional `--branch`.
`remove` subparser: positional `name`, optional `--force` (store_true).

---

#### tests/scripts/test_worktree.py

**Unit tests — no git, no npm, no real filesystem needed for these two units:**

Create `tests/scripts/__init__.py` (empty) so pytest can discover the module.

**Port assignment tests (`test_scan_used_ports`, `test_find_free_port`):**

```python
def test_scan_used_ports_empty(tmp_path):
    # WORKTREES_DIR with no subdirs → returns empty set

def test_scan_used_ports_reads_port_line(tmp_path):
    # Create tmp_path/.worktrees/feat/.vorch/WORKTREE.md with "port: 8422"
    # scan_used_ports returns {8422}

def test_scan_used_ports_skips_malformed(tmp_path):
    # WORKTREE.md exists but has no "port:" line → returns empty set

def test_find_free_port_skips_used(tmp_path, monkeypatch):
    # Patch scan_used_ports to return {8421, 8422}
    # Patch is_port_bound to always return False
    # find_free_port returns 8423

def test_find_free_port_skips_bound(tmp_path, monkeypatch):
    # scan_used_ports returns {}
    # is_port_bound returns True for 8421, False for 8422
    # find_free_port returns 8422
```

**Settings merge tests:**

```python
def test_merge_settings_creates_new_file(tmp_path):
    # settings.json does not exist → merge_settings creates it with {"server_port": 8421}
    # assert json content

def test_merge_settings_updates_existing_key(tmp_path):
    # Write {"server_port": 9999, "other": "keep"}
    # merge_settings with {"server_port": 8421}
    # assert server_port updated, other preserved

def test_merge_settings_adds_key(tmp_path):
    # Write {"other": "value"}
    # merge_settings with {"server_port": 8421}
    # assert both keys present

def test_merge_settings_creates_parent_dirs(tmp_path):
    # settings path inside non-existent subdirectory → no error, file created
```

Import the functions directly: `from scripts.worktree import scan_used_ports, find_free_port, merge_settings, is_port_bound`

> Note: `--import-mode=importlib` means no `__init__.py` is needed in `scripts/`, but one IS needed in `tests/scripts/` for pytest to treat it as a package. Add `tests/scripts/__init__.py` (empty).

---

**Files:** [`scripts/worktree.py`, `tests/scripts/__init__.py`, `tests/scripts/test_worktree.py`]

---

## Done When

- `python scripts/worktree.py --help` lists `create` and `remove` subcommands
- `python scripts/worktree.py create <name>` exits 0, prints 5 `key: value` lines, creates `.worktrees/<name>/`, writes `.vorch/WORKTREE.md` inside it
- `python scripts/worktree.py remove <name>` exits 0, prints 4 `key: value` lines
- `python scripts/worktree.py create <name>` (again) exits 1, prints `error: worktree '<name>' already exists`
- All unit tests in `tests/scripts/test_worktree.py` pass

## Risks / Assumptions

- `npm install` is assumed to be available on `PATH` — no fallback; if absent the error message from subprocess is surfaced as-is
- `git worktree` is assumed to be available (standard git ≥ 2.5)
- The `port: <port>` line in WORKTREE.md must be parseable exactly — the `scan_used_ports` parser looks for lines starting with `"port: "` (including the space)
- Partial `create` failures leave a worktree on disk; the script intentionally does not roll back — the user runs `remove --force` to clean up. This keeps the script simple and avoids silent partial-deletion side effects.
- `shutil.copytree` with a missing source (`.data-dir-base/`) falls into the "just mkdir" branch; an empty-but-present `.data-dir-base/` is also treated the same way (checked with `any(DATA_DIR_BASE.iterdir())`)
