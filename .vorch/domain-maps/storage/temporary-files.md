# Temporary files

Filenames and the cleanup sweep of `TemporaryFileManager` (`core/storage/temp_files.py`, `test_temp_files.py`). Categories, retention and leases are in `../storage.md` -> Temporary files.

- New retained filenames use `tmp_` plus 12 lowercase base32 characters and the owner-selected suffix; exclusive file creation retries collisions without truncating existing logs.
- With a running Event Loop every sweep, the first included, runs on the `temporary-files` worker pool, never on the loop (a sweep lists every retained file, more than ten thousand command logs on a busy install); a start without a loop sweeps once inline.
- A sweep refuses links or junctions at `artifacts/`, `temp/` or a category, reads type and modification time from the directory listing (`os.scandir`: no per-file system call on Windows, one stat elsewhere) and confirms each expired regular file with a fresh unfollowed stat before removing it, because listing data can lag behind a file still open elsewhere; links and junctions are never deletion candidates.
- The final lease check and removal share the manager lock with `reopen`, including its file recreation, so a producer that reactivates an expired file during a sweep keeps its active file.
- `stop` ends an in-flight sweep after its current file and `aclose` awaits it.
