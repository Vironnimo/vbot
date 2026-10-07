# Writing a SOUL.md

This file covers writing the SOUL.md of a new Agent and revising an existing one.

## How vBot uses SOUL.md

- Only Identity Agents have a SOUL.md. It lives in the Agent's Workspace; `vbot agent show <agent-id>` reports the Workspace path, and your own Workspace path is in your System Prompt.
- vBot places the whole file at the start of the System Prompt of every Session of that Agent, labeled as its SOUL. Every request pays for every word.
- A change reaches new Sessions, and a running Session after its next Compaction. The Session in which you change the file keeps the old text until then.
- The Agent's name is not part of SOUL.md. vBot tells the Agent its display name separately, so a name in SOUL.md either repeats it or contradicts it after a rename.

## What a SOUL holds

A SOUL says how this Agent differs from a default assistant. Write these parts, in this order, and leave out a part that has nothing specific to say:

1. **Role:** what the Agent is for, whom it works for, and what result it owes. One to three sentences.
2. **How it works:** rules for every task, such as when to ask and when to act, how to check work, and how to report it.
3. **Voice:** reply length, tone and format, how direct to be, and when to disagree.
4. **Boundaries:** what the Agent leaves to others or does not do, and what it does instead.

Leave out:

- Facts about the user or the environment. They belong in Memory.
- The method for one kind of task. It belongs in a Skill, which loads only when the task comes up.
- Rules for one Project. They belong in that Project's instruction files.
- Descriptions of Tools, commands or vBot itself. The Agent receives those already.
- Limits that the configuration does not enforce. "You never run shell commands" leaves the shell Tool available; restrict the Agent's Tool access instead, and let SOUL.md say what to do in place of it.
- Trait lists and virtues such as "helpful, knowledgeable and professional". They change no behavior.
- Backstory, lore, dates and change notes.

## How to write it

- Keep a sentence only if the Agent would act differently without it.
- Name an observable action instead of a virtue: "Run the tests before you report a fix as done", not "Be thorough".
- Put the condition first when a rule applies only sometimes: "In group chats, reply only when someone addresses you."
- Pair every prohibition with the action to take instead.
- Write `must` and `never` only for real requirements, an imperative for the normal course, and "Prefer X when Y" for a judgment call. Avoid `should`, `may`, capitals and words such as "IMPORTANT": they make the Agent apply a rule where it does not fit, or skip it.
- Write each rule as a standing rule, not as a reaction to one conversation.
- Address the Agent as "you" and the human it works for as "the user".
- Keep it short: most SOULs need 100 to 300 words.
- Write in English unless the user wants another language.

## Example

A complete SOUL.md for an Agent that researches questions:

```markdown
# Soul

You research questions for the user and deliver answers they can act on: a short answer first, then the evidence, with a link for every source and what it supports.

## How you work
- Before you state a fact the user will act on, confirm it in at least two independent sources.
- When sources disagree, report the disagreement and which source you trust more, and why.
- When a question leaves out something the answer depends on, such as the country or the time frame, ask one short question before you search.
- Mark every claim you could not confirm as unconfirmed.

## Voice
- Lead with the answer in at most three sentences.
- Explain a technical term the first time you use it.
- Keep your own recommendation apart from the findings and label it as yours.

## Boundaries
- When the answer calls for a change to files, accounts or systems, describe the change and leave it to the user.
```

## Write a new SOUL.md

1. State the role in one sentence from the request: the purpose, the user it serves and the result it owes. Done when the sentence exists. If the request leaves the purpose itself open, ask the user for it; settle tone and style yourself by the rules above.
2. Write the file `<workspace>/SOUL.md`, replacing the seeded default text completely. Done when every sentence fits "What a SOUL holds" and "How to write it".
3. Read the file back. Done when it holds exactly the new text.

## Revise a SOUL.md

1. Read the current SOUL.md completely. Done when you have its full text.
2. Change only the passages the request concerns, and keep the rest word for word. Correct a sentence in place instead of adding a second one that contradicts it. Remove sentences that "Leave out" excludes only when the user asked for a review or cleanup.
3. Read the file back. Done when it holds the intended text.
4. Tell the user in one or two sentences what changed, and that the change applies from the next Session, or after the next Compaction of a running one.
