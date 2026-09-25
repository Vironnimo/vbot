"""The file-search owner's option vocabulary, parser, and reference output."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Option:
    names: tuple[str, ...]
    key: str
    description: str
    argument: bool = False
    value: str = "true"
    content_only: bool = False
    native: bool = False
    repeat: bool = False


def _option(
    names: str,
    key: str,
    description: str,
    *,
    argument: bool = False,
    value: str = "true",
    content: bool = False,
    native: bool = False,
    repeat: bool = False,
) -> Option:
    return Option(tuple(names.split()), key, description, argument, value, content, native, repeat)


OPTIONS = (
    _option("--limit", "limit", "Page size, equivalent to the limit field.", argument=True),
    _option(
        "--offset", "offset", "Results to skip, equivalent to the offset field.", argument=True
    ),
    _option(
        "-i --ignore-case",
        "case",
        "Ignore content letter case.",
        value="ignore",
        content=True,
        native=True,
    ),
    _option(
        "-s --case-sensitive",
        "case",
        "Match content letter case (default).",
        value="sensitive",
        content=True,
        native=True,
    ),
    _option(
        "-S --smart-case",
        "case",
        "Ignore case unless a pattern contains an uppercase literal.",
        value="smart",
        content=True,
        native=True,
    ),
    _option(
        "-F --fixed-strings",
        "literal",
        "Search literal text instead of regex.",
        content=True,
        native=True,
    ),
    _option(
        "--no-fixed-strings",
        "literal",
        "Search regex patterns (default).",
        value="false",
        content=True,
        native=True,
    ),
    _option(
        "-w --word-regexp",
        "boundary",
        "Match whole words.",
        value="word",
        content=True,
        native=True,
    ),
    _option(
        "-x --line-regexp",
        "boundary",
        "Match whole lines; overrides whole-word matching.",
        value="line",
        content=True,
        native=True,
    ),
    _option(
        "--no-word-regexp",
        "boundary",
        "Disable whole-word matching.",
        value="none",
        content=True,
        native=True,
    ),
    _option(
        "--no-line-regexp",
        "boundary",
        "Disable whole-line matching.",
        value="none",
        content=True,
        native=True,
    ),
    _option(
        "-v --invert-match",
        "invert",
        "Select lines matching none of the patterns.",
        content=True,
        native=True,
    ),
    _option(
        "--no-invert-match",
        "invert",
        "Select matching lines (default).",
        value="false",
        content=True,
        native=True,
    ),
    _option(
        "-U --multiline",
        "multiline",
        "Allow matches spanning lines; dot still excludes newlines.",
        content=True,
        native=True,
    ),
    _option(
        "--no-multiline",
        "multiline",
        "Use line-oriented search (default).",
        value="false",
        content=True,
        native=True,
    ),
    _option(
        "--multiline-dotall",
        "dotall",
        "Let dot match newlines with multiline search.",
        content=True,
        native=True,
    ),
    _option(
        "--no-multiline-dotall",
        "dotall",
        "Let dot exclude newlines (default).",
        value="false",
        content=True,
        native=True,
    ),
    _option(
        "-P --pcre2",
        "engine",
        "Use PCRE2, including lookaround and backreferences.",
        value="pcre2",
        content=True,
        native=True,
    ),
    _option(
        "--no-pcre2",
        "engine",
        "Use the default Rust regex engine.",
        value="default",
        content=True,
        native=True,
    ),
    _option(
        "--engine",
        "engine",
        "Regex engine: default, pcre2, or auto.",
        argument=True,
        content=True,
        native=True,
    ),
    _option(
        "--no-unicode",
        "unicode",
        "Disable Unicode regex matching.",
        value="false",
        content=True,
        native=True,
    ),
    _option(
        "--unicode",
        "unicode",
        "Enable Unicode regex matching (default).",
        content=True,
        native=True,
    ),
    _option(
        "-E --encoding",
        "encoding",
        "Text encoding; default auto detects a UTF BOM, otherwise UTF-8.",
        argument=True,
        content=True,
        native=True,
    ),
    _option("--crlf", "crlf", "Recognize CRLF line boundaries.", content=True, native=True),
    _option(
        "--no-crlf", "crlf", "Use normal line boundaries.", value="false", content=True, native=True
    ),
    _option(
        "--null-data",
        "null_data",
        "Use NUL instead of newline as the record delimiter.",
        content=True,
        native=True,
    ),
    _option(
        "-a --text",
        "binary",
        "Search binary files as text.",
        value="text",
        content=True,
        native=True,
    ),
    _option(
        "--binary",
        "binary",
        "Include binary-file matches.",
        value="binary",
        content=True,
        native=True,
    ),
    _option(
        "--no-text --no-binary",
        "binary",
        "Use normal binary detection.",
        value="skip",
        content=True,
        native=True,
    ),
    _option(
        "-g --glob",
        "glob",
        "File glob; ! excludes. Repeated filters are ordered and never override ignore rules.",
        argument=True,
        repeat=True,
    ),
    _option(
        "--iglob", "iglob", "A case-insensitive file glob; ! excludes.", argument=True, repeat=True
    ),
    _option(
        "--glob-case-insensitive",
        "glob_case",
        "Match path patterns and globs case-insensitively (default).",
        value="ignore",
    ),
    _option(
        "--no-glob-case-insensitive",
        "glob_case",
        "Match path patterns and globs case-sensitively.",
        value="sensitive",
    ),
    _option(
        "-t --type",
        "type",
        "Include a named file type; repeat to include several.",
        argument=True,
        repeat=True,
    ),
    _option("-T --type-not", "type_not", "Exclude a named file type.", argument=True, repeat=True),
    _option(
        "--type-add",
        "type_add",
        "Define a file type for this call, e.g. web:*.{html,js}.",
        argument=True,
        repeat=True,
    ),
    _option(
        "--type-clear",
        "type_clear",
        "Remove a named type's globs for this call.",
        argument=True,
        repeat=True,
    ),
    _option(
        "-u --unrestricted",
        "unrestricted",
        "Disable ignores; twice includes hidden entries; three times searches binary text.",
        repeat=True,
    ),
    _option("--no-ignore", "ignore", "Disable ignore rules.", value="false"),
    _option("--ignore", "ignore", "Enable ignore rules (default)."),
    _option("--hidden -.", "hidden", "Include hidden entries (default)."),
    _option("--no-hidden", "hidden", "Skip hidden entries under each search root.", value="false"),
    _option("--no-ignore-dot", "ignore_dot", "Disable .ignore and .rgignore files.", value="false"),
    _option("--ignore-dot", "ignore_dot", "Enable .ignore and .rgignore files (default)."),
    _option("--no-ignore-vcs", "ignore_vcs", "Disable .gitignore and Git excludes.", value="false"),
    _option("--ignore-vcs", "ignore_vcs", "Enable .gitignore and Git excludes (default)."),
    _option(
        "--no-ignore-parent",
        "ignore_parent",
        "Do not read ignore files above each root.",
        value="false",
    ),
    _option("--ignore-parent", "ignore_parent", "Read applicable parent ignore files (default)."),
    _option("--no-ignore-global", "ignore_global", "Disable global Git excludes.", value="false"),
    _option("--ignore-global", "ignore_global", "Enable global Git excludes (default)."),
    _option(
        "--no-ignore-exclude",
        "ignore_exclude",
        "Disable repository info/exclude files.",
        value="false",
    ),
    _option(
        "--ignore-exclude", "ignore_exclude", "Enable repository info/exclude files (default)."
    ),
    _option(
        "--ignore-file",
        "ignore_file",
        "Additional ignore file; relative paths use cwd.",
        argument=True,
        repeat=True,
    ),
    _option(
        "--no-ignore-files",
        "ignore_files",
        "Disable explicitly supplied ignore files.",
        value="false",
    ),
    _option("--ignore-files", "ignore_files", "Enable supplied ignore files (default)."),
    _option(
        "--ignore-file-case-insensitive", "ignore_case", "Match ignore rules without letter case."
    ),
    _option(
        "--no-ignore-file-case-insensitive",
        "ignore_case",
        "Match ignore rules with letter case (default).",
        value="false",
    ),
    _option(
        "--no-require-git",
        "require_git",
        "Honor .gitignore outside Git repositories (default).",
        value="false",
    ),
    _option("--require-git", "require_git", "Honor .gitignore only inside a Git repository."),
    _option("-L --follow", "follow", "Follow symbolic links; loops are skipped and reported."),
    _option("--no-follow", "follow", "Do not follow symbolic links (default).", value="false"),
    _option(
        "-d --max-depth",
        "depth",
        "Maximum traversal depth; 0 selects only explicit roots.",
        argument=True,
    ),
    _option(
        "--max-filesize",
        "size",
        "Maximum file size, optionally suffixed K, M, or G.",
        argument=True,
    ),
    _option("--one-file-system", "one_fs", "Do not cross filesystem boundaries."),
    _option(
        "--no-one-file-system",
        "one_fs",
        "Allow crossing filesystem boundaries (default).",
        value="false",
    ),
    _option(
        "-A --after-context",
        "after",
        "Lines after matches.",
        argument=True,
        content=True,
        native=True,
    ),
    _option(
        "-B --before-context",
        "before",
        "Lines before matches.",
        argument=True,
        content=True,
        native=True,
    ),
    _option(
        "-C --context",
        "context",
        "Lines before and after matches.",
        argument=True,
        content=True,
        native=True,
    ),
    _option(
        "-l --files-with-matches",
        "output",
        "Return paths containing matches.",
        value="files",
        content=True,
    ),
    _option(
        "--files-without-match",
        "output",
        "Return paths containing no matches.",
        value="without",
        content=True,
    ),
    _option(
        "-c --count",
        "output",
        "Count matching lines per file (spanning multiline matches count as matches).",
        value="lines",
        content=True,
    ),
    _option(
        "--count-matches",
        "output",
        "Count individual matches per file.",
        value="counts",
        content=True,
    ),
    _option("--include-zero", "zero", "Include files with zero counts.", content=True),
    _option(
        "--no-include-zero",
        "zero",
        "Omit files with zero counts (default).",
        value="false",
        content=True,
    ),
    _option(
        "-o --only-matching",
        "only",
        "Return each matching substring, with its source position.",
        content=True,
    ),
    _option(
        "--no-only-matching",
        "only",
        "Return complete matching lines (default).",
        value="false",
        content=True,
    ),
    _option(
        "-q --quiet",
        "quiet",
        "Return an explicit matched boolean; stop after a proven match.",
        content=True,
    ),
    _option(
        "--sort",
        "sort",
        "Ascending order: path, modified, accessed, created, or none.",
        argument=True,
    ),
    _option(
        "--sortr",
        "sortr",
        "Descending order: path, modified, accessed, created, or none.",
        argument=True,
    ),
    _option(
        "-m --max-count",
        "max_count",
        "Stop after this many matched lines per file; not the global result limit.",
        argument=True,
        content=True,
        native=True,
    ),
    _option(
        "--stop-on-nonmatch",
        "stop",
        "Stop each file at the first nonmatch after a match.",
        content=True,
        native=True,
    ),
    _option("--stats", "stats", "Include observed search statistics."),
    _option("--no-stats", "stats", "Omit search statistics (default).", value="false"),
    _option("--debug --trace", "debug", "Include bounded file-selection diagnostics."),
    _option(
        "--type-list",
        "reference",
        "List supported file types, honoring type additions and clears.",
        value="types",
    ),
    _option(
        "-h --help", "reference", "Show the supported options and search examples.", value="help"
    ),
    _option(
        "-n --line-number -H --with-filename --no-heading",
        "satisfied",
        "Already provided by this Tool; accepted without changing results.",
        content=True,
    ),
    _option(
        "-r -R --recursive -I",
        "satisfied",
        "Recursive search that skips binary files is the default; accepted without changes.",
    ),
    _option("--no-config", "satisfied", "Ambient ripgrep configuration is always disabled."),
    _option(
        "--color",
        "color",
        "Only never is valid; results contain plain source evidence.",
        argument=True,
    ),
)
BY_NAME = {name: option for option in OPTIONS for name in option.names}


@dataclass
class SearchOptions:
    entries: list[tuple[Option, str]] = field(default_factory=list)

    def get(self, key: str, default: str = "") -> str:
        result = default
        unrestricted = 0
        for option, value in self.entries:
            if option.key == "unrestricted":
                unrestricted += 1
                if key == "ignore":
                    result = "false"
                elif key == "hidden" and unrestricted >= 2:
                    result = "true"
                elif key == "binary" and unrestricted >= 3:
                    result = "text"
            if option.key == key:
                result = value
        return result

    def enabled(self, key: str, default: bool = False) -> bool:
        return self.get(key, str(default).lower()) == "true"

    def values(self, key: str) -> list[str]:
        return [value for option, value in self.entries if option.key == key]

    def native_arguments(self) -> list[str]:
        result: list[str] = []
        unrestricted = 0
        for option, value in self.entries:
            if option.key == "unrestricted":
                unrestricted += 1
                if unrestricted == 3:
                    result.append("--text")
            if option.native:
                # Preserve the actual spelling; its polarity is part of the contract.
                result.append(option.names[0])
                if option.argument:
                    result.append(value)
        return result

    @property
    def context(self) -> tuple[int, int]:
        symmetric = self.get("context", "0")
        return int(self.get("before", symmetric)), int(self.get("after", symmetric))

    @property
    def ordering(self) -> tuple[str, bool] | None:
        for option, value in reversed(self.entries):
            if option.key in {"sort", "sortr"}:
                return value, option.key == "sortr"
        return None


def parse_options(tokens: list[str], *, action: str, kind: str) -> SearchOptions:
    result = SearchOptions()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("-") or token == "--":
            raise ValueError(
                f"Unexpected option value {token!r}; use args=['--help'] for search syntax."
            )
        pending: list[tuple[str, str | None]] = []
        if token.startswith("--"):
            name, separator, long_value = token.partition("=")
            pending.append((name, long_value if separator else None))
        else:
            position = 1
            while position < len(token):
                name = "-" + token[position]
                option = BY_NAME.get(name)
                position += 1
                inline = (
                    token[position:]
                    if option and option.argument and position < len(token)
                    else None
                )
                pending.append((name, inline))
                if option and option.argument:
                    break
        for name, inline in pending:
            option = BY_NAME.get(name)
            if option is None:
                raise ValueError(f"Unsupported search option {name!r}. Use args=['--help'].")
            if (
                action == "paths"
                and option.key == "case"
                and option.value in {"ignore", "sensitive"}
            ):
                option = BY_NAME[
                    "--glob-case-insensitive"
                    if option.value == "ignore"
                    else "--no-glob-case-insensitive"
                ]
            if action == "paths" and option.content_only:
                raise ValueError(
                    f"{name} searches contents; omit --files/--dirs/--entries to use it."
                )
            if option.argument:
                if inline is None:
                    if index >= len(tokens):
                        raise ValueError(f"{name} requires a value.")
                    inline = tokens[index]
                    index += 1
                value = inline
            else:
                if inline is not None:
                    raise ValueError(f"{name} does not take a value.")
                if (
                    len(pending) == 1
                    and index < len(tokens)
                    and tokens[index].casefold() in {"true", "false"}
                ):
                    boolean = tokens[index].casefold()
                    index += 1
                    if boolean == "false":
                        raise ValueError(
                            f"{name} false needs an explicit supported reverse flag; "
                            "see args=['--help']."
                        )
                value = option.value
            _validate_value(option, value)
            result.entries.append((option, value))
    if (
        action == "paths"
        and kind != "files"
        and any(option.key in {"size", "type", "type_not"} for option, _ in result.entries)
    ):
        raise ValueError(
            "File type and size filters require --files; they cannot select directories."
        )
    if any(result.context) and (
        result.get("output") or result.enabled("only") or result.enabled("quiet")
    ):
        raise ValueError(
            "Context requires matching-line output; remove the content-output "
            "selector or context options."
        )
    if result.enabled("dotall") and not result.enabled("multiline"):
        raise ValueError("--multiline-dotall requires -U/--multiline.")
    return result


def _validate_value(option: Option, value: str) -> None:
    if (
        option.key in {"before", "after", "context", "depth", "max_count", "limit", "offset"}
        and not value.isdecimal()
    ):
        raise ValueError(f"{option.names[0]} requires a nonnegative integer.")
    if option.key == "engine" and value not in {"default", "pcre2", "auto"}:
        raise ValueError("--engine must be default, pcre2, or auto.")
    if option.key in {"sort", "sortr"} and value not in {
        "path",
        "modified",
        "accessed",
        "created",
        "none",
    }:
        raise ValueError("Sort by path, modified, accessed, created, or none.")
    if option.key == "color" and value != "never":
        raise ValueError("Search results use plain source text; --color accepts only never.")
    if option.key == "size":
        size_bytes(value)


def size_bytes(value: str) -> int:
    suffix = value[-1:].upper()
    multiplier = {"K": 1024, "M": 1024**2, "G": 1024**3}.get(suffix, 1)
    digits = value[:-1] if suffix in {"K", "M", "G"} else value
    if not digits.isdecimal():
        raise ValueError(
            "--max-filesize requires bytes or a nonnegative number suffixed K, M, or G."
        )
    return int(digits) * multiplier


def help_text() -> str:
    lines = [
        "Fields: pattern (regex; omit it to list files), path, glob, output "
        "(content, files, count), context, limit, offset.",
        "args adds ripgrep arguments, one per item; no shell quoting or expansion.",
        "args alone also works: ['-w', 'TODO', 'src']. Literal: ['-F', 'call(', 'src'].",
        "Repeat -e/--regexp for OR patterns. -- ends option parsing.",
        "Paths: ['--files', '-g', '*.py', 'src']; --dirs finds directories; --entries finds both.",
        "Repeated flags follow their documented order; repeated globs are ordered filters.",
        (
            "Defaults: regex, case-sensitive content, hidden included, ignores"
            " honored, no symlink following."
        ),
        "Name globs are case-insensitive. Globs without / match basenames at every depth;",
        "globs with / are root-relative. Use './*.py' for top-level files only.",
        "Positive filters never override ignore rules.",
        (
            "Content-only flags are marked [content]; -i/-s also control name case. "
            "File type/size filters require --files for paths."
        ),
        (
            "Roots are literal files/directories; omit them for cwd. "
            "--dirs includes empty directories."
        ),
    ]
    sections = {
        "-i": "Content matching and decoding",
        "-g": "File selection",
        "-u": "Ignore rules and traversal",
        "-A": "Results and context",
        "--sort": "Ordering and processing limits",
        "--type-list": "Reference and fixed formatting",
    }
    for option in OPTIONS:
        if option.names[0] in sections:
            lines.append("\n" + sections[option.names[0]])
        marker = " [content]" if option.content_only else ""
        value = " VALUE" if option.argument else ""
        lines.append(f"{', '.join(option.names)}{value}{marker}: {option.description}")
    lines.append(
        "Source coordinates and plain-text result formatting are fixed; sh"
        "ell commands and output transformations are not search options."
    )
    lines.extend(
        [
            "\nExamples:",
            '{"pattern":"run","path":["src","tests"],"glob":"*.py","context":2,"args":["-w"]}',
            '{"args":["--dirs","-g","migrations"]}',
            '{"args":["-F","-i","-l","-u","error"]}',
        ]
    )
    return "\n".join(lines)
