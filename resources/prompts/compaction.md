Compact the conversation prefix above into a continuation context for the next model call so work can resume without losing critical details.

An earlier compaction summary, when present, is already part of the conversation above. Treat it as established context and carry its still-relevant facts forward. Your output replaces the entire prefix above and must stand alone.

If a <user_instruction>...</user_instruction> block is present, the user gave it for this specific compaction. Follow it and let it steer what you emphasize, keep, or condense — without ever dropping the critical details required below.

Requirements:
- Preserve exact file paths, symbol names, command names, and exact error strings.
- Preserve what has already been tried and the outcome of each attempt.
- Preserve the current task status and the immediate next concrete step.
- Keep important technical decisions, constraints, and blockers.
- Preserve important Tool outcomes, but omit raw Tool protocol and bulky output that the continuation no longer needs.
- Do not preserve or reproduce Skill instructions, Skill resource guidance, or Skill environment-access guidance; vBot records activated Skill names separately at the checkpoint. Preserve only task decisions, constraints, progress, and outcomes that remain relevant.
- Do not add facts that are not present in the conversation.

Write the output as continuation context, not as a retrospective summary.
Start with this exact line:
Here is the context of the ongoing task.

Then use these sections in order:
Current objective:
What was tried:
Known errors and constraints:
Current status:
Next concrete step:
