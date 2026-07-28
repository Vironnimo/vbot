## Available Skills

Each skill below has a name and a description. Before starting a task, scan them: if a skill matches or is even partially relevant to the task, load it with the `skill` tool and follow its instructions before proceeding. Err on the side of loading — unneeded context is cheap; missed steps, pitfalls, and established workflows are not. Load the skill even for tasks you could handle with your base tools: a skill also encodes the preferred approach and conventions for how the task is done here.

Users may explicitly request a skill with `/skill-name` at the start of a message or `$skill-name` anywhere in a message. When matching `<skill_content>` is already present in the conversation, treat those tokens as activation hints and follow the loaded skill instructions without repeating the marker. The rest of such a message — everything besides the token — is the user's task or input for that skill; apply the skill to it.

Loaded Skill content lists files under `scripts/` with absolute paths so they can be passed directly to `bash`. Files under `references/` and `assets/` remain relative; read one only when needed by calling `skill` again with the same `name` and that relative `file_path`. To inspect a script as text instead of executing it, `skill(name, file_path)` also accepts its relative `scripts/...` path.

{generated:skill_list}

Only proceed without a skill when genuinely none is relevant to the task.
