# Command-line analysis

`core/permissions/commands/` turns one shell command line into a `CommandPlan`: the simple commands it runs, the paths it reads, writes or changes into, the http(s) URLs it names, and what it could not resolve. It runs nothing and never touches the filesystem (no realpath, no existence checks); deciding about a plan, link resolution included, belongs to the Permission Boundary. Public interface: `core/permissions/commands/commands.py`; everything else in the folder is private.

## Interface

- `CommandAnalyzer(*, powershell_executable: Path | None, limits=None, helper_launcher=None)`, one per server. `async analyze(command_line, *, shell, cwd, env, home) -> CommandPlan`; `async analyze_argv(argv, *, cwd, env, home) -> CommandPlan` for a program started directly (a terminal's program: literal words, the program dispatched like a command, a shell with a command analyzed as a nested shell); `async aclose()`. The Runtime's `CommandChecker` owns the one instance and creates it on first use (`permissions.md` -> Command checks). `shell` is a `ShellKind`: `"powershell"`, `"bash"` (also zsh and other POSIX shells) or `"cmd"`. `env` is the environment the command would run with; `home` the user's home folder.
- `powershell_executable` comes from `core.tools.shell.powershell_executable(search_path)`, the lookup the Shell Tool uses; None makes PowerShell analysis unavailable. `helper_launcher` replaces the helper process (tests); `limits` is a `PowerShellHelperLimits`.
- `CommandAnalysisUnavailable` (a `VBotError`): the PowerShell helper could not answer (no executable, start failure, timeout, crash, unknown answer). Callers fail closed. bash and cmd never raise it.
- `shell_kind(program) -> ShellKind | None`: the analysis language of a shell program (`pwsh.exe` -> `powershell`), for the terminal: a line typed at such a prompt is analyzed with `analyze`, the terminal's shell and its reported working directory.
- `normalize_command_pattern(pattern)`, `command_pattern_matches(pattern, command, *, case_insensitive)`, `command_pattern_may_match(...)`: see Patterns.
- `MAX_COMMAND_LINE_CHARS` (1,000,000) and `MAX_NESTING_DEPTH` (8).

## CommandPlan contract

- `commands: tuple[SimpleCommand, ...]` in source order, from every depth: pipelines, `$( )`, `@( )`, script blocks, backticks, subshells, control-flow bodies, nested shells. `SimpleCommand(name, words, source, shell)`: `name` is canonical (PowerShell alias resolved to its cmdlet; program without folder and `.exe`/`.com`/`.cmd`/`.bat`; transparent wrappers removed), `words` the arguments as `CommandWord(text, static)`, `source` the original text (at most 500 characters), `shell` the language the command was written in. A static word's `text` is its value after expansion; a dynamic word's `text` is its source.
- `paths: tuple[PathAccess, ...]`: `PathAccess(kind, path, source, subtree)`. `kind` is `read`, `write` or `start_dir` (every folder a `cd` changes into). `path` is absolute and lexically normalized. `subtree` True: the access may reach anything below `path` (a wildcard's literal folder, recursive operations, `Remove-Item`, pipeline-fed targets, archive extraction), so a rule below it applies too.
- `unresolved: tuple[UnresolvedElement, ...]`: `UnresolvedElement(kind, source, reason)` with `kind` `path_read`, `path_write` (a read or write whose path is unknown), `cwd` (a change into an unknown folder) or `command` (something the analysis cannot see into). `reason` is a lower-case clause for refusals and logs.
- `urls: tuple[str, ...]`: http(s) URLs found in any word.
- Entries are deduplicated in first-seen order. A plan is conservative: when a directory change may fail, later paths resolve against both folders.

What becomes an unresolved `command`: a command line above the limit or that does not parse; nesting deeper than 8; a computed command name (`& $x`, `. $profile`, `$cmd args`); `Invoke-Expression`/`iex`, `eval`, `pwsh -Command`, `bash -c` or `cmd /c` of a computed string; `--%` (the rest of the line goes to the program unparsed); a name the line redefines with `Set-Alias`/`New-Alias`; a shell that reads commands from its input (`curl ... | sh`, `pwsh -`); `wsl`; fish scripts; `env -S`.

## Lowering

- PowerShell: PowerShell's own parser in the helper (Helper below), with `StaticParameterBinder` for cmdlets and functions; aliases resolve against the session's default commands. bash: tree-sitter-bash in process (imported on first use, about 4 us per parse). cmd: a small tokenizer in `_cmd.py` (`&`, `&&`, `||`, `|`, blocks, `^`, redirects, `if`/`for ... do`/`else`).
- Nested shells unwrap recursively: `pwsh`/`powershell` `-Command`/`-c`, `-EncodedCommand`/`-e` (UTF-16LE base64), `-File`/`-f` and a script operand are reads, `-WorkingDirectory` a `start_dir`; `bash`/`sh`/`zsh`/... `-c`; `cmd /c` and `/k` (`/v:on` turns on delayed expansion); `iex`, `eval`, `watch`, `trap` with literal strings; `find -exec`; `Start-Process` (its program and `ArgumentList` become a command, `WorkingDirectory` a `start_dir`, redirect parameters accesses).
- Wrappers: `timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`, `builtin`, `exec`, `ionice`, `env` (its assignments make those variables dynamic for nested shells; `-C` changes folder), `xargs` (the inner command's targets come from the pipeline) and cmd's `call` are removed. `sudo` and `doas` stay and the inner command is added too.
- Variables: PowerShell `$env:NAME` (case-insensitive on Windows), `$HOME`, `$PWD`, `~`; bash `$HOME`, `$PWD`, `$OLDPWD`, `~` and exact environment names; cmd `%NAME%` and `%CD%`. A variable the script assigns anywhere (assignments, `for`, `read`, `Set-Variable`, `env:` item writes, `set X=`), a missing one, and every other expansion are dynamic. `[Environment]::SetEnvironmentVariable` makes all PowerShell variables dynamic.

## Paths

- Tier A, known commands, by bound parameter or per-program spec (`_specs.py`): the PowerShell item, content, archive, ACL and web cmdlets in `CMDLETS`, plus any `<write verb>-<noun>` cmdlet with a generic path parameter (`Path`, `LiteralPath`, `FilePath`, `Destination`, `OutFile`, ...); POSIX file programs in `PROGRAMS` (rm, mv, cp, ln, mkdir, touch, chmod, sed, cat, grep, rg, ls, tee, rsync, zip, ...), with option parsing (bundles, `--x=v`, `--`) and the last operand as destination; special handlers for find, tar, dd, unzip, curl, wget, icacls and attrib; cmd built-ins in `CMD_BUILTINS` (del, rd, md, copy, move, ren, type, dir, mklink). Redirects are accesses (`$null`, `NUL`, `/dev/null` are none). A tier A path that is dynamic or cannot be placed is unresolved.
- Tier B, every other command: only words that are clearly paths count, as reads: absolute, drive-letter, UNC, `~` or home-variable paths, and relative paths that climb out with `..`, also as the value of `--opt=value` or `key=value`; `file://` URLs give their path. URLs, git refs and other words are ignored; dynamic tier B words are not unresolved.
- Globs: a wildcard path is a `subtree` access on its literal prefix folder (`src/*.py` -> `src/**`); `..` after a wildcard is unresolved. PowerShell treats wildcards per parameter (`-LiteralPath` none).
- Pipelines: a cmdlet or `xargs` command that takes its targets from the pipeline gets `subtree` accesses on the folders the listing command before it names (Get-ChildItem, Get-Item, Resolve-Path, Select-String; ls, find, fd, grep, rg, dir), through filters (Where-Object, Select-Object, Sort-Object, ...); with any other producer the targets are unresolved.
- Windows forms: drive-relative `C:foo` resolves against the cwd only on the same drive, else against the folder the line last used on that drive or the `=C:` environment entry, else unresolved; root-relative `\x` against the cwd's drive; UNC stays as-is; `\\?\` and `\\?\UNC\` are stripped, other device paths kept; `FileSystem::` is stripped; non-file PowerShell drives (`HKCU:`, `env:`, `variable:`, ...) are no paths; other PowerShell drives are unresolved. bash on Windows (Git Bash) maps `/c/x` to `C:\x`; other POSIX absolute paths are unresolved there.

## Working directory tracking

- Handled: `cd`, `chdir`, `Set-Location`/`sl`, `pushd`/`Push-Location`, `popd`/`Pop-Location`, `cd -`, `cd..`, `cd\`, `cd~`, `X:` drive switches, cmd `cd /d`, `env -C`, `pwsh -WorkingDirectory`.
- Every change may fail, so the old folder stays a candidate; at most 16 candidates, beyond that the folder is unknown. A dynamic or unplaceable target makes later relative paths unresolved. Every folder changed into is a `start_dir` access.
- bash scopes changes to subshells, command substitutions, pipeline elements and background commands; PowerShell and cmd have no such scopes.
- `cd -` without a known previous folder is unresolved (bash falls back to `$OLDPWD`); a bare `cd`/`Set-Location` goes home.

## Patterns

`Shell(...)` patterns follow Claude Code: the command is its canonical name and static words joined by single spaces; `*` matches any text; a trailing ` *` also matches the bare command; a dynamic word matches only `*`. `normalize_command_pattern` collapses spaces, reads the legacy `prefix:*` as `prefix *`, reduces a first word with a folder or extension to the program name, and raises `ValueError(reason)` for an empty pattern or control characters. PowerShell commands always compare case-insensitively, and the pattern's first word resolves through PowerShell's default aliases (`rm *` matches `Remove-Item x`; on Linux and macOS only the aliases that are no programs there). `command_pattern_may_match` is for deny rules: it matches when some value of the dynamic words could make the pattern match (`rm <$X>` may match `rm -rf /`).

## PowerShell helper

- One long-lived `pwsh` per analyzer, started lazily: `-NoLogo -NoProfile -NonInteractive -Command <bootstrap>`; the bootstrap reads the base64 helper script `powershell_analysis.ps1` (a resource beside the module, shipped with `core`) from the first stdin line and runs it, so no execution policy applies. Module autoloading is off; the helper imports the built-in Management, Utility, Security and Archive modules from `$PSHOME`, builds its command table and runs one warm-up analysis before it reports ready.
- JSON lines over stdin/stdout: ready line `{"ready": true, "version": ...}`, request `{id, script: base64}`, answer `{id, ok, result | error}`. Requests are serialized.
- Limits (`PowerShellHelperLimits`): start 20 s, request 5 s, restart after 2000 requests or above 512 MiB RSS, stop after 600 s idle. A failed request is retried once with a fresh helper; after a failed start, requests fail at once for 15 s. Every failure raises `CommandAnalysisUnavailable`.
- Started like every server child (`core/utils/processes.py`): windowless in a new process group with System32 as cwd inside the server's kill-on-close Job on Windows; under the process guardian with its own session on POSIX. `aclose` kills the process tree.
- Measured on Windows with pwsh 7.6 over 50 varied lines: about 0.55 s from start to ready (warm-up included), then 4 ms median and 8 ms at the 90th percentile per line, nested shells included.

## Limits and false-refusal risks

- Command-line analysis is lexical: links, junctions and mounts are not resolved, so a path that reaches elsewhere through a link looks local.
- Scripts a line runs (`./x.sh`, `. .\setup.ps1`, `source f`, `python s.py`) are reads; their contents are not analyzed.
- Unknown programs contribute only tier B reads; a program that writes where its arguments do not say (or through a config file) is not seen.
- Likely false refusals: any `cd` keeps the old folder, so relative paths after it also resolve in the starting folder; dynamic tier A paths (loop variables, `$_`) are unresolved; PowerShell parameter binding errors on a tier A cmdlet (`rm -rf x`) are unresolved; POSIX absolute paths under Git Bash; `cd -` first on a line.
- PowerShell analysis needs `pwsh`; the Shell Tool runs PowerShell only on Windows, and the tests of the PowerShell table run only there.

## Tests

`tests/core/permissions/commands/`: one plan table per shell (`test_command_analysis.py` for bash and cmd on every platform, `test_command_analysis_powershell.py` for PowerShell with one helper per module on Windows with pwsh, plus helper lifecycle tests against a fake helper), and `test_command_patterns.py`. Rows compare commands, paths (relative to a fixed root, `/**` for subtree) and unresolved kinds (`support.py`).
