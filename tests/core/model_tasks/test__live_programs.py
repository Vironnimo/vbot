"""Coding program screens: readiness, startup questions, and where typed text lands.

The screens follow what codex-cli 0.153.2 and Claude Code 2.1.280 drew in a vBot
Terminal (PowerShell 7.6, rendered by the Terminal's screen renderer).
"""

from __future__ import annotations

import pytest

from core.model_tasks.live import (
    CODING_PROGRAMS,
    CodingProgram,
    program_input_visible,
    program_prompt,
    program_ready,
    program_text_pending,
    selected_answer,
    shell_prompt_visible,
)

CODEX = CODING_PROGRAMS["codex"]
CLAUDE = CODING_PROGRAMS["claude"]

SHELL = "PowerShell 7.6.6\nPS C:\\work\\app>"
SHELL_WRAPPED = (
    "PowerShell 7.6.6\n"
    "PS C:\\Users\\someone\\AppData\\Local\\Temp\\a-very-long-folder-name-that-does-not-fit\n"
    "-into-one-line\\app>"
)
CODEX_HEADER = (
    "╭────────────────────────────────────────────────────────╮\n"
    "│ >_ OpenAI Codex (v0.153.2)                             │\n"
    "│                                                        │\n"
    "│ model:     {model}   /model to change                  │\n"
    "│ directory: C:\\work\\app                                 │\n"
    "╰────────────────────────────────────────────────────────╯\n"
)
CODEX_LOADING = (
    "PowerShell 7.6.6\nPS C:\\work\\app> codex\n"
    + CODEX_HEADER.format(model="loading")
    + "\n\n› Ask Codex to do anything\n\n  ? for shortcuts"
)
CODEX_STARTING = (
    "PowerShell 7.6.6\nPS C:\\work\\app> codex\n"
    + CODEX_HEADER.format(model="gpt-5 high")
    + "\n  Tip: Type / to open the command popup; Tab autocompletes slash commands.\n\n"
    "• Starting MCP servers (2/4): codex_apps, docs (0s • esc to inte…\n\n\n"
    "› Ask Codex to do anything\n\n  gpt-5 high · C:\\work\\app · Contex…"
)
CODEX_READY = (
    "PowerShell 7.6.6\nPS C:\\work\\app> codex\n"
    + CODEX_HEADER.format(model="gpt-5 high")
    + "\n  Tip: Type / to open the command popup; Tab autocompletes slash commands.\n\n\n"
    "› Ask Codex to do anything\n\n  gpt-5 high · C:\\work\\app · Contex…"
)
CODEX_UPDATE = (
    "PowerShell 7.6.6\n"
    "  ✨ Update available!e0.153.2 -> 0.157.0s\\app> codex\n\n"
    "  Release notes: https://github.com/openai/codex/releases/latest\n\n"
    "› 1. Update now (runs `npm install -g @openai/codex`)\n"
    "  2. Skip\n"
    "  3. Skip until next version\n\n"
    "  Press enter to continue"
)
CODEX_TRUST = (
    ">oYou are in C:\\work\\new\n"
    "PS C:\\work\\new> codex\n"
    "-eDo8youctrust5theecontents1ofcthishdirectory?tWorkingewith untrustedrcontentson\n"
    "_scomespwithshigher risk of prompt injection. Trusting the directory allows\n"
    "  project-local config, hooks, and exec policies to load.\n\n"
    "› 1. Yes, continue\n"
    "  2. No, quit\n\n"
    "  Press enter to continue"
)
CLAUDE_READY = (
    "PowerShell 7.6.6\nPS C:\\work\\app> claude\n"
    " ▐▛███▛█   Claude Code v2.1.280\n"
    "▝▜██████▀  Opus (1M context) with high effort\n"
    "  ▝▝ ▝▝    C:\\work\\app\n\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    '❯\xa0Try "refactor <filepath>"\n'
    "────────────────────────────────────────────────────────────────────────────────\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle)"
)
CLAUDE_TRUST = (
    "PowerShell 7.6.6\nPS C:\\work\\new> claude\n\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    " Accessing workspace:\n\n C:\\work\\new\n\n"
    " Quick safety check: Is this a project you created or one you trust?\n\n"
    " ❯ No, exit\n"
    "   Yes, I trust this folder\n\n"
    " Enter to confirm · Esc to cancel"
)


@pytest.mark.parametrize(
    ("program", "screen", "ready", "prompt"),
    [
        (CODEX, SHELL, False, None),
        (CODEX, "PowerShell 7.6.6\nPS C:\\work\\app> codex", False, None),
        (CODEX, CODEX_LOADING, False, None),
        (CODEX, CODEX_STARTING, False, None),
        (CODEX, CODEX_READY, True, None),
        (CODEX, CODEX_UPDATE, False, "update"),
        (CODEX, CODEX_TRUST, False, "trust"),
        (CLAUDE, SHELL, False, None),
        (CLAUDE, CLAUDE_READY, True, None),
        (CLAUDE, CLAUDE_TRUST, False, "trust"),
    ],
)
def test_recognizes_the_observed_start_screens(
    program: CodingProgram, screen: str, ready: bool, prompt: str | None
) -> None:
    assert program_ready(program, screen) is ready
    found = program_prompt(program, screen)
    assert (found.kind if found else None) == prompt


def test_codex_input_line_shows_before_it_finished_starting() -> None:
    # Typing is allowed only once ready; the input line alone is not enough.
    assert program_input_visible(CODEX, CODEX_LOADING)
    assert not program_ready(CODEX, CODEX_LOADING)


@pytest.mark.parametrize(
    "screen",
    [
        SHELL,
        SHELL_WRAPPED,
        CODEX_UPDATE,
        CODEX_TRUST,
        # The program exited; its old input line stays above the new prompt.
        CODEX_READY + "\nPS C:\\work\\app>",
        CODEX_READY + "\n" + SHELL_WRAPPED.split("\n", 1)[1],
    ],
)
def test_typed_text_never_lands_in_the_shell_or_a_menu(screen: str) -> None:
    assert not program_input_visible(CODEX, screen)


@pytest.mark.parametrize(
    ("screen", "shell"),
    [
        (SHELL, True),
        (SHELL_WRAPPED, True),
        ("user@host:~/app$ ", True),
        (CODEX_READY, False),
        (CLAUDE_READY, False),
        (CODEX_READY + "\nPS C:\\work\\app>", True),
        # A status line ending in % or > is not a prompt.
        (CODEX_READY + "\n  context 42%", False),
    ],
)
def test_detects_the_shell_prompt_as_the_last_line(screen: str, shell: bool) -> None:
    assert shell_prompt_visible(screen) is shell


def test_an_earlier_program_screen_does_not_count_for_the_current_one() -> None:
    screen = CODEX_TRUST + "\nPS C:\\work\\new> codex\n" + CODEX_HEADER.format(model="loading")
    assert program_prompt(CODEX, screen) is None
    assert not program_ready(CODEX, screen)


CLAUDE_EDIT_MENU = (
    CLAUDE_READY.rsplit("\n", 4)[0] + "\n\n"
    " Do you want to make this edit to app.py?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, allow all edits during this session (shift+tab)\n"
    "   3. No\n"
)
CODEX_APPROVAL = (
    CODEX_READY.rsplit("\n", 3)[0] + "\n\n"
    "  Would you like to run the following command?\n\n"
    "  $ git push\n\n"
    "› 1. Yes, proceed (y)\n"
    "  2. No, and tell Codex what to do differently (esc)\n"
)


@pytest.mark.parametrize(
    ("program", "screen"), [(CODEX, CODEX_APPROVAL), (CLAUDE, CLAUDE_EDIT_MENU)]
)
def test_numbered_menus_are_not_input_lines(program: CodingProgram, screen: str) -> None:
    assert not program_input_visible(program, screen)
    assert selected_answer(program, screen) in {"Yes, proceed (y)", "Yes"}


PROMPTS_AFTER_EXIT = {
    "powershell": "PS C:\\work\\app>",
    "cmd": "C:\\work\\app>",
    "bash": "dev@box:~/app$",
    "zsh": "dev@box ~/app %",
    "fish": "dev@box ~/app>",
    "starship": "~/app on  main\n❯",
    "oh-my-posh on PowerShell": "  C:\\work\\app   main ❯",
}


@pytest.mark.parametrize("program", [CODEX, CLAUDE], ids=["codex", "claude"])
@pytest.mark.parametrize("prompt", PROMPTS_AFTER_EXIT.values(), ids=PROMPTS_AFTER_EXIT.keys())
def test_a_shell_prompt_below_an_exited_program_is_the_shell(
    program: CodingProgram, prompt: str
) -> None:
    screen = (CODEX_READY if program is CODEX else CLAUDE_READY) + "\n" + prompt
    assert shell_prompt_visible(screen)
    assert not program_input_visible(program, screen)


def test_the_selected_answer_of_a_startup_question() -> None:
    assert selected_answer(CLAUDE, CLAUDE_TRUST) == "No, exit"
    moved = CLAUDE_TRUST.replace(" ❯ No, exit\n   Yes,", "   No, exit\n ❯ Yes,")
    assert selected_answer(CLAUDE, moved) == "Yes, I trust this folder"
    assert selected_answer(CODEX, CODEX_TRUST) == "Yes, continue"
    assert (
        selected_answer(CODEX, CODEX_UPDATE) == "Update now (runs `npm install -g @openai/codex`)"
    )


@pytest.mark.parametrize(
    ("program", "screen", "text", "pending"),
    [
        (
            CODEX,
            CODEX_READY.replace("Ask Codex to do anything", "Fix the login bug"),
            "Fix the login bug",
            True,
        ),
        (
            CLAUDE,
            CLAUDE_READY.replace('Try "refactor <filepath>"', "line one"),
            "line one\nline two",
            True,
        ),
        # A large paste shows as a placeholder.
        (
            CLAUDE,
            CLAUDE_READY.replace('Try "refactor <filepath>"', "[Pasted text #1 +40 lines]"),
            "x\n" * 41,
            True,
        ),
        # A numbered list the user dictated is still their text.
        (
            CODEX,
            CODEX_READY.replace("Ask Codex to do anything", "1. Add tests 2. Commit"),
            "1. Add tests 2. Commit",
            True,
        ),
        # Not echoed yet, text left over from before, or a menu instead of the text.
        (CODEX, CODEX_READY, "Fix the login bug", False),
        (
            CODEX,
            CODEX_READY.replace("Ask Codex to do anything", "oldFix the login bug"),
            "Fix the login bug",
            False,
        ),
        (CODEX, CODEX_APPROVAL, "Yes", False),
        (CLAUDE, CLAUDE_EDIT_MENU, "Yes", False),
        (CODEX, CODEX_READY + "\nPS C:\\work\\app>", "Ask Codex", False),
    ],
)
def test_enter_follows_only_text_the_input_line_holds(
    program: CodingProgram, screen: str, text: str, pending: bool
) -> None:
    assert program_text_pending(program, screen, text) is pending
