Create the condensed continuation context that becomes this Session's next compaction checkpoint.

Your output replaces the entire conversation above, and work resumes immediately from it alone. Nothing else is retained: every detail required to continue seamlessly must be in your output.

An earlier compaction summary, when present, is already part of the conversation above. Treat it as established context and carry its still-relevant facts forward.

If a <user_instruction>...</user_instruction> block is present, the user gave it for this specific compaction. Follow it and let it steer what you emphasize, keep, or condense — without ever dropping the critical details required below.

Requirements:
- Preserve exact file paths, symbol names, command names, and exact error strings.
- Preserve what has already been tried and the outcome of each attempt.
- Preserve unanswered user questions and open requests.
- Preserve the current task status and the immediate next concrete step.
- Keep important technical decisions, constraints, and blockers.
- Preserve important Tool outcomes, but omit raw Tool protocol and bulky output that the continuation no longer needs.
- Do not preserve or reproduce Skill instructions, Skill resource guidance, or Skill environment-access guidance; vBot records activated Skill names separately at the checkpoint. Preserve only task decisions, constraints, progress, and outcomes that remain relevant.
- Do not add facts that are not present in the conversation.

Write the output as continuation context, not as a retrospective summary.
Start with this exact line:
Here is the context of the ongoing task.

Then use these sections in order, keeping the headers exactly as written; when a section has no content, write "None." on its own line:
Current objective:
What was tried:
Known errors and constraints:
Current status:
Next concrete step:
