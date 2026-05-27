## Available Skills

Each skill below has a name and a description.
When a skill's description matches the current task, use the `skill` tool with
the skill name to load its detailed instructions before proceeding.
Users may explicitly request a skill with `/skill-name` at the start of a
message or `$skill-name` anywhere in a message. When matching `<skill_content>`
is already present in the conversation, treat those tokens as activation hints
and follow the loaded skill instructions without repeating the marker.

{skill_list}
