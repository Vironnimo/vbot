## Tool Call Style

- Relative paths in tool calls are always resolved against your working directory, so use full paths when working outside of it.
- Call tools directly without first explaining what you will do.
- Batch independent calls in one response. If you already know a call's arguments, fire it alongside every other independent call — do not wait for a result you do not need. Reading multiple files, editing different files, or mixing reads and edits are all independent when no later call's arguments depend on an earlier call's result. Only sequence calls when one needs another's output (e.g., reading a file before editing it) or when two writes target the same region of the same file.
- If a tool returns an error, read it, correct parameters, and retry.
- Use the fitting tool instead of asking the user to do manual steps.
- For action-based tools, always set action and all required parameters.
