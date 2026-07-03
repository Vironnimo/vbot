Author a reusable skill for yourself from the source described below. A skill is a SKILL.md playbook that teaches you how to handle a specific task or domain.

Use the `skill_manage` tool with operation "create" to write exactly one well-formed skill into your own skill home. Give it a short, descriptive, hyphenated name; the SKILL.md needs YAML front matter with `name` (matching the skill's directory) and a `description` of at most 60 characters that says when to use the skill. Structure the body with clear sections in a fixed order: Overview (what it is for and when to use it), Steps (the procedure), then Notes (edge cases and gotchas). Keep it concise and actionable.

Frame any tool usage in terms of vBot's actual tools — `read`, `write`, `edit`, `glob`, `grep`, `bash`, `web_fetch`, `web_search`, `process`, `status` — and do not invent tools, commands, or facts that are not in the source. If the source is a folder or URL, read it first with your file/web tools; if it is the recent conversation or pasted text, work from that. Capture only what is genuinely there.

After creating the skill, tell the user in one or two sentences what skill you created and when it will help. Do not paste the full SKILL.md back.
