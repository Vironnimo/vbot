"""Variable names that can run code outside the analyzed command."""

_HOOKS = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "ZDOTDIR",
        "INPUTRC",
        "PATH",
        "SHELLOPTS",
        "BASHOPTS",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5OPT",
        "PERL5LIB",
        "RUBYOPT",
        "RUBYLIB",
        "PSMODULEPATH",
        "DOTNET_STARTUP_HOOKS",
        "DOTNET_ADDITIONAL_DEPS",
        "DOTNET_SHARED_STORE",
        "GIT_EXEC_PATH",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_PAGER",
    }
)


def controls_command_execution(name: str) -> bool:
    name = name.upper()
    return name in _HOOKS or name.startswith(
        ("BASH_FUNC_", "LD_", "DYLD_", "GIT_CONFIG", "GIT_SSH", "COMPLUS_", "VBOT_")
    )


def controls_command_analysis(name: str) -> bool:
    """An opaque value must not steer path resolution or repository selection."""
    name = name.upper()
    return controls_command_execution(name) or name in {
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "PYTHONPATH",
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
