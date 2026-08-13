Review this Session for durable improvements to your private Skill library. Extract reusable ways of working, not a narrative of this Run. Be active but evidence-based: a correction, a non-trivial technique, or an outdated instruction is enough reason to improve a Skill. From now on, every other Tool is disabled. Use only `skill` and `skill_manage`; do not attempt any other Tool call.

Signals that warrant a Skill update include:

- The user corrected your style, tone, format, verbosity, or approach. Frustration such as "stop doing X" is a first-class Skill signal: put the lesson where the next relevant Session will load it.
- A non-trivial technique, fix, workaround, verification method, or debugging path emerged that future work of the same class would benefit from.
- A Skill used or consulted in this Session was wrong, stale, ambiguous, or missing an important step.

Prefer the smallest durable change in this order:

1. Update a private Skill used in this Session. Read the relevant file with `skill`, then use `skill_manage` with `patch` or `edit`.
2. Update another existing private Skill. Call `skill` with no arguments to list your Skills, inspect the best candidate with `skill`, and add the lesson where it naturally belongs.
3. Add a focused support file to an existing Skill under `references/`, `scripts/`, or `assets/`, then point to it briefly from `SKILL.md`.
4. Create a new private Skill only when no existing Skill owns this class of work. Name it for the reusable task class, never for today's ticket, error string, or codename.

Make only the change supported by the reviewed evidence. Do not add unrelated improvements, helper scripts, templates, or completeness work. Because this Reflection Run cannot execute support scripts, create or change a script only when that exact script or procedure already ran successfully in the reviewed Session; otherwise save instructions or a reference.

Each `skill_manage` action changes the Skill directly. Use flat fields such as `{"action":"patch","name":"wiki-research","match":"...","content":"..."}` and `{"action":"write_file","name":"wiki-research","file_path":"references/api.md","content":"..."}`. If the flawed Skill is bundled, global, or Project-owned, report the issue in your final summary instead of editing a different scope.

Do not encode environment-dependent failures, transient errors, unverified negative claims about Tools, or one-off task narratives as durable instructions. Capture the reusable fix or retry pattern, not the temporary failure.

When genuinely nothing reusable was learned, reply "Nothing to save." and stop. Otherwise, finish with one or two sentences stating what you changed and why it will help. Never paste complete Skill files into the summary.
