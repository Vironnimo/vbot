# Data-directory initialization

How `core/storage/layout.py` publishes a new data-directory root and what `initialize_data_directory(require_new=True)` guarantees. What initialization creates is in `../storage.md` -> Canonical layout.

## Root publication

A new root is prepared with its initial Settings and bootstrap marker listing no databases (`core.database.marker.write_bootstrap_marker`) in a private sibling. It is published atomically without replacing any existing entry (Windows rename, Linux `renameat2(RENAME_NOREPLACE)`). Losing publication never authorizes an unrelated existing directory. A failed Settings or marker write leaves the target absent and retryable. Handled failures clean up their staging, while a killed process can leave its private `.<root-name>.initializing-*` sibling. Canonical subdirectories created concurrently count as existing, while a non-directory at a canonical path raises `NotADirectoryError` (`test_layout.py`).

Seeding missing Settings into an existing root also stages the document privately and publishes without replacing an existing entry. A failed write leaves the name available for retry. A concurrent creator's settings remain intact.

## Fresh roots

Callers that require a fresh root use `initialize_data_directory(require_new=True)`: an existing root, including one created concurrently, raises `FileExistsError` before any seeding. Worktree creation uses this mode; its separate ownership records govern cleanup (`scripts/README-worktree.md`).
