Create a standalone condensed record of this session as the next compaction checkpoint.

No run is active and nothing continues automatically after this compaction. Your output replaces the entire conversation above, and any later message may continue the recorded work or start something entirely new — so the record must stand alone and preserve everything needed to resume the recorded work faithfully.

An earlier compaction summary, when present, is already part of the conversation above. Treat it as established context and carry its still-relevant facts forward.

If a <user_instruction>...</user_instruction> block is present, the user gave it for this specific compaction. Follow it and let it steer what you emphasize, keep, or condense — without ever dropping the critical details required below.

Requirements:
- Preserve exact file paths, symbol names, command names, and exact error strings.
- Preserve what has already been tried and the outcome of each attempt.
- Preserve unanswered user questions and open requests.
- Preserve the current task status and, when the recorded work is unfinished, its concrete next step.
- Keep important technical decisions, constraints, and blockers.
- Preserve important Tool outcomes, but omit raw Tool protocol and bulky output that is no longer needed.
- Do not preserve or reproduce Skill instructions, Skill resource guidance, or Skill environment-access guidance; vBot records activated Skill names separately at the checkpoint. Preserve only task decisions, constraints, progress, and outcomes that remain relevant.
- Do not add facts that are not present in the conversation.

Write the output as condensed session context, not as a retrospective summary.
Start with this exact line:
Here is the context of the ongoing task.

Then use these sections in order, keeping the headers exactly as written; when a section has no content, write "None." on its own line:
Current objective:
What was tried:
Known errors and constraints:
Current status:
Next concrete step:
