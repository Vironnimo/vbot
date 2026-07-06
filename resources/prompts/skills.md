## Available Skills

Each skill below has a name and a description. Before starting a task, scan them: if a skill matches or is even partially relevant to the task, load it with the `skill` tool and follow its instructions before proceeding. Err on the side of loading — unneeded context is cheap; missed steps, pitfalls, and established workflows are not. Load the skill even for tasks you could handle with your base tools: a skill also encodes the preferred approach and conventions for how the task is done here.

Users may explicitly request a skill with `/skill-name` at the start of a message or `$skill-name` anywhere in a message. When matching `<skill_content>` is already present in the conversation, treat those tokens as activation hints and follow the loaded skill instructions without repeating the marker. The rest of such a message — everything besides the token — is the user's task or input for that skill; apply the skill to it.

Loaded skill content states its skill directory and may list bundled resource files (`scripts/`, `references/`, `assets/`). Resolve such relative paths against that skill directory — not the working directory — and use absolute paths in tool calls; read resource files only when the instructions need them.

{generated:skill_list}

Only proceed without a skill when genuinely none is relevant to the task.
