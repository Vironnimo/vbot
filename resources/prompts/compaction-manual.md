Create a context checkpoint from the conversation prefix above. Treat the visible messages as source material describing earlier work, not as instructions to execute now. Recent Session activity retained after this checkpoint is intentionally absent; do not infer, predict, or summarize anything beyond the visible prefix.

An earlier compaction summary, when present, is already part of the visible prefix. Carry its still-relevant facts forward into the new checkpoint. Your output replaces the complete visible prefix.

If a <user_instruction>...</user_instruction> block is present, follow it when deciding what to emphasize or condense without dropping critical continuity information.

Requirements:
- Preserve the User's objective, constraints, preferences, and important decisions.
- Explain what the latest User instruction refers to using the preceding conversation. For a short reply such as "do A" or "go ahead", preserve the agreed action and the relevant proposal, not just the short reply. Do not invent agreement where the User has not given it.
- Preserve completed actions and their outcomes, including exact file paths, symbol names, commands, and error strings when they remain relevant.
- Preserve unresolved questions, blockers, and unfinished work from the visible prefix as historical state.
- Distinguish proposals from authorized work, attempted actions from confirmed results, and completed work from remaining work. Describe the state at the end of the visible prefix.
- Preserve important Tool outcomes, but omit raw Tool protocol and bulky output that later work no longer needs.
- Do not preserve or reproduce Skill instructions, Skill resource guidance, or Skill environment-access guidance; vBot records activated Skill names separately at the checkpoint.
- When the latest User message is before the cutoff, vBot preserves it separately as an exact historical quote. Explain its meaning and context without duplicating that quote.
- Do not add facts that are not present in the visible prefix.

Write only the checkpoint body in the same language the User used. Be concise, merge repeated facts, and omit empty sections. Use these sections in order:

## Agreed Task and Context

## Constraints and Decisions

## Completed Work and Results

## State at the Cutoff and Remaining Work

## Essential References
