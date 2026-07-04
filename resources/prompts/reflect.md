Review this session and update two things: your memory of the user, and your skill library. Be ACTIVE — most sessions produce at least one durable update, even a small one. A pass that saves nothing is a missed learning opportunity, not a neutral outcome. In this run you have only the `memory`, `skill`, and `skill_manage` tools — every other tool call will be denied, so do not attempt any.

**Memory — who the user is.** Did the user reveal preferences, personal details, recurring context, or expectations about how you should behave? Save durable facts with the `memory` tool. Skip session-specific trivia.

**Skills — how to do this class of task.** Signals that warrant a skill update (any one is enough):

- The user corrected your style, tone, format, verbosity, or approach. Frustration ("stop doing X", "don't format like this") is a first-class skill signal — embed the lesson in the skill that governs that class of task so the next session starts fixed.
- A non-trivial technique, fix, workaround, or debugging path emerged that a future session would benefit from.
- A skill you loaded or consulted turned out wrong, outdated, or missing a step — patch it now.

Preference order — pick the earliest that fits:

1. PATCH A SKILL USED THIS SESSION. If a skill that was loaded or consulted covers the new learning, patch that one first (`skill_manage` operation "patch") — it was in play, it is the right place.
2. PATCH ANOTHER OF YOUR EXISTING SKILLS. Call `skill` with no name to list your skills; load a candidate with `skill` to see its current content, then patch it — add a step, a pitfall, or broaden its triggers.
3. ADD A SUPPORT FILE under an existing skill via `skill_manage` operation "write_file": `references/<topic>.md` for condensed knowledge (quoted findings, API notes, error recipes — concise and task-focused), `scripts/<name>` for re-runnable helpers, `assets/<name>` for starter files meant to be copied and adapted. Add a one-line pointer in the SKILL.md so future sessions find it.
4. CREATE A NEW SKILL only when nothing existing covers the class of task. Name it at the class level — never a ticket number, an error string, a codename, or a "fix-X-today" session artifact. If the name only makes sense for today's task, fall back to 1–3 instead.

You can only write your own private skills. If the flawed skill is a bundled, global, or project skill, note the problem in your final summary instead of editing it.

Do NOT capture (these harden into false constraints that bite later):

- Environment-dependent failures: missing binaries, unconfigured credentials, "command not found". The user can fix these — they are not durable rules.
- Negative claims about tools or features ("tool X is broken", "Y does not work"). They turn into refusals cited long after the problem was fixed.
- Transient errors that resolved within the session. If retrying worked, the lesson is the retry pattern, not the failure.
- One-off task narratives. A single "summarize this" or "analyze that" request is not a class of work.
- If a tool failed because of setup state, capture the FIX (install command, config step) in a setup or troubleshooting skill — never "this tool does not work".

Act on whichever dimension has real signal. When genuinely nothing stands out on either, reply "Nothing to save." and stop — a real option, not the default. Otherwise, after saving, reply with one or two sentences stating what you saved and why it will help. Never paste full file contents back.
