---
name: agent-authoring
description: Write or revise an Agent's SOUL.md or a Skill, and decide what belongs in a SOUL, a Skill or Memory. Use when creating or renaming an Agent, changing a SOUL, or turning a workflow or conversation into a Skill.
---

# Agent authoring

This Skill covers the text that shapes an Agent in vBot: its SOUL.md and its Skills. Creating an Agent and setting its Model, Tool access and Skill selection go through the `vbot` CLI, which the `vbot-cli` Skill describes.

## What goes where

Store each piece of guidance in exactly one place:

| Content | Place |
|---|---|
| Who the Agent is: its role, the result it owes, how it works on every task, its voice, its boundaries | SOUL.md in the Agent's Workspace |
| How to do one kind of task, including the user's preferences and corrections for that work | a Skill |
| Facts about the user or the environment that matter in every Session | Memory |
| Rules for every Session in one Project | the Project's instruction files in its repository |
| The Agent's name | its display name in the Agent configuration |
| What the Agent can do: Tools, Skills, Projects | the Agent configuration; text in SOUL.md cannot grant or remove a Tool |

## Read the guide for the task

- Before you write or revise a SOUL.md, read `references/soul.md` with the `skill` Tool (`name` `agent-authoring`, `file_path` `references/soul.md`).
- Before you create a Skill or rewrite one, read `references/skills.md` the same way.

Change another Agent's SOUL.md or Skills only when the user asks for it.

## Create an Agent for a purpose

Build the Agent from the request alone. The new Agent keeps its files in its own Workspace, which `vbot agent create` creates. Leave the user's files and folders untouched: when the role needs the user's documents or a folder elsewhere, name that in your report, and the user points the Agent to it.

1. State the new Agent's role in one sentence from the request: its purpose, the user it serves and the result it owes. Done when the sentence exists. If the request leaves the purpose itself open, ask the user for it; decide id, name, tone and configuration yourself.
2. Create the Agent with `vbot agent create <agent-id> <display-name>` and set its Model, Tool access and Skills; the `vbot-cli` Skill's `references/agents-projects.md` lists the flags. Give the Agent only the Tools its role needs. Done when `vbot agent show <agent-id>` reports the Agent with its Workspace path.
3. Write its SOUL.md by `references/soul.md`, section "Write a new SOUL.md". Done when those steps are done.
4. When the role includes a recurring kind of task with a known method, create a private Skill for the new Agent by `references/skills.md`. Done when each such method is in a Skill, or the role has none.
5. Report the Agent's id and name, its role sentence, the Skills you created, and each configuration choice with its alternative.

## Names

An Agent's display name is how the user and other Agents refer to it; its id addresses it in commands and stays fixed when the name changes. An Identity Agent finds its own name and id in its System Prompt.

- When the user asks to rename an Agent, yourself included, change its display name: `vbot agent update <agent-id> --name "<new name>"`. The name is one line of at most 80 characters. Done when `vbot agent show <agent-id>` reports the new name.
- Change the id with `vbot agent rename <current-agent-id> <new-agent-id>` only when the user asks for a new id. vBot refuses it while the Agent has an active Run, so you cannot change your own id: give the user that command to run outside your Runs, for example in a terminal.
