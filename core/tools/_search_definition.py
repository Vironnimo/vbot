"""File-search definition and help reference shared by registration and call reading."""

from __future__ import annotations

import json

from core.tools.tools import JsonObject

SEARCH_FILES_TOOL_NAME = "search_files"


HELP_EXAMPLES = (
    {"pattern": "def load_config", "path": ["src", "tests"], "glob": "*.py", "context": 2},
    {"pattern": "todo", "args": ["-i", "-w", "-t", "py"], "output": "count"},
    {"pattern": "connect(", "args": ["-F"]},
    {"pattern": "error", "args": ["-u", "-i"], "output": "files"},
    {"glob": "migrations", "args": ["--dirs"]},
)


def help_text() -> str:
    """Return the reference that args ["--help"] shows."""
    examples = "\n".join(f"  {json.dumps(example)}" for example in HELP_EXAMPLES)
    return f"""search_files runs ripgrep over the files below path.

Examples:
{examples}

Default selection: hidden files are searched, .gitignore and .ignore rules apply (also
outside git repositories), binary files are skipped, and .git is always skipped. A
glob that names files also selects files those rules exclude. __pycache__ is skipped
unless -u is given or a glob names __pycache__ or .pyc files. Globs and file types
match names in any letter case unless --no-glob-case-insensitive is given.

Output: content names each file on its own line, followed by line:text for its matching
lines and line-text for context lines. Content results are ordered by path, file lists
newest first. Every page says how many results exist; continue with next_offset.

args items, one flag or value per item. Other ripgrep flags work as well:
  -i / -s / -S          ignore case / match case / ignore case unless the pattern has capitals
  -F                    match the pattern as literal text
  -w / -x               match whole words / whole lines
  -v                    show lines that do not match
  -e PATTERN            another pattern; a line matching any pattern matches
  -U                    let a match span lines; add --multiline-dotall for . to match newlines
  -P                    PCRE2 regex: look-around and backreferences
  -A N / -B N / -C N    lines after / before / around each match
  -o                    show only the matched text
  -m N                  at most N matching lines per file
  -l / -c / --count-matches / --files-without-match
                        list matching files / count matching lines / count matches /
                        list files without a match
  -t TYPE / -T TYPE     only / not files of a type; --type-list lists the types
  -g GLOB               like glob; a leading ! excludes
  -u / -uuu             also search ignored files / and binary files
  --no-hidden           skip hidden files
  -L                    follow symbolic links
  -d N                  descend at most N directory levels
  --max-filesize SIZE   skip larger files, such as 1M
  -E ENCODING / -a      read files in an encoding / search binary files as text
  --files / --dirs      list files / list directories, empty directories included; a pattern
                        matches their names
  --sort KEY / --sortr KEY
                        order ascending / descending by path, modified, accessed or created
"""


_STRING_LIST: JsonObject = {"type": "array", "items": {"type": "string"}}
SEARCH_FILES_TOOL_DESCRIPTION = (
    "Search file contents with a regular expression, or find files and directories by name. "
    "Use this instead of grep, rg, find, or ls in the shell."
)
SEARCH_FILES_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": (
                "Regular expression (ripgrep syntax) for file contents. Omit to list files."
            ),
        },
        "path": {
            **_STRING_LIST,
            "description": "Files or directories to search. Omit for the working directory.",
        },
        "glob": {
            **_STRING_LIST,
            "description": "Case-insensitive name filters such as *.py; a leading ! excludes.",
        },
        "output": {
            "type": "string",
            "enum": ["content", "files", "count"],
            "description": (
                "files lists matching files; count counts matching lines per file, every match "
                'with "--count-matches" in args. Omit for matching lines.'
            ),
        },
        "context": {
            "type": "integer",
            "description": "Lines around each match. Omit for none.",
        },
        "args": {
            **_STRING_LIST,
            "description": (
                "More ripgrep arguments, one per item, such as -i, -w, -F, -t py, -u (include "
                "ignored files), --dirs (list directories, empty ones included). "
                '["--help"] lists all.'
            ),
        },
        "limit": {
            "type": "integer",
            "default": 100,
            "description": "Results per page.",
        },
        "offset": {
            "type": "integer",
            "description": "Pass next_offset to continue.",
        },
    },
}
