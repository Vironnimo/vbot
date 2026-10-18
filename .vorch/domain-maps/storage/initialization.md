# Data-directory initialization

How `core/storage/layout.py` publishes a new data-directory root and what `initialize_data_directory(require_new=True)` guarantees. What initialization creates is in `../storage.md` -> Canonical layout.

## Root publication

A new root is prepared with its bootstrap marker listing no databases (`core.database.marker.write_bootstrap_marker`) in a private sibling, then published atomically without replacing any existing entry (Windows rename; Linux `renameat2(RENAME_NOREPLACE)`). Concurrent initializers therefore never see a newly published root without its marker, and losing publication never authorizes an unrelated existing directory. A failed marker write leaves the target absent and retryable; handled failures clean up their staging, while a killed process can leave only its private `.<root-name>.initializing-*` sibling, which is never adopted. Unsupported no-replace publication fails rather than falling back to a replacing rename. Canonical subdirectories created concurrently count as existing, while a non-directory at a canonical path still raises `NotADirectoryError` (`test_layout.py`).

## Fresh roots

Callers that require a fresh root use `initialize_data_directory(require_new=True)`: an existing root, including one created concurrently, raises `FileExistsError` before any seeding. Worktree creation uses this mode; its separate ownership records govern cleanup (`scripts/README-worktree.md`).
