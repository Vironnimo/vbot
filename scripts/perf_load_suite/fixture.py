"""The disposable Project the load Agents work in.

The harness writes this directory once per run and registers it as the Project
every load Agent is rooted in, so relative Tool paths resolve inside it. The
fake Provider scripts its Tool calls against the same constants.
"""

from __future__ import annotations

from pathlib import Path

SOURCE_DIRECTORY = "src"
SOURCE_FILE_COUNT = 12
SOURCE_FILE_FUNCTIONS = 30
SEARCH_NEEDLE = "PERF_NEEDLE"
BASH_COMMAND = 'python -c "print(1)"'
PROJECT_DISPLAY_NAME = "perf-fixture"

_AGENTS_MD = """# Perf fixture

Synthetic Project used by the vBot load harness. Files under `src/` are
generated filler code; nothing here is meant to be edited.
"""


def source_file_paths() -> list[str]:
    """Project-relative paths the scripted ``read`` calls rotate through."""
    return [f"{SOURCE_DIRECTORY}/module_{index:02d}.py" for index in range(SOURCE_FILE_COUNT)]


def write_fixture_project(root: Path) -> Path:
    """Create the fixture Project under ``root`` and return its directory."""
    project = root / PROJECT_DISPLAY_NAME
    (project / SOURCE_DIRECTORY).mkdir(parents=True, exist_ok=True)
    (project / "AGENTS.md").write_text(_AGENTS_MD, encoding="utf-8")
    for index, relative_path in enumerate(source_file_paths()):
        (project / relative_path).write_text(_module_source(index), encoding="utf-8")
    return project


def _module_source(module_index: int) -> str:
    lines = [f'"""Generated module {module_index:02d} for load testing."""', ""]
    for function_index in range(SOURCE_FILE_FUNCTIONS):
        name = f"compute_{module_index:02d}_{function_index:02d}"
        lines.extend(
            [
                "",
                f"def {name}(values: list[int]) -> int:",
                f'    """Fold values for step {function_index}."""',
                "    total = 0",
                "    for value in values:",
                f"        total = (total * 31 + value + {function_index}) % 1_000_003",
            ]
        )
        if function_index % 7 == 0:
            lines.append(f"    # {SEARCH_NEEDLE}: marker {module_index}-{function_index}")
        lines.append("    return total")
    return "\n".join(lines) + "\n"
