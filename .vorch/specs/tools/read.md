# Read Tool

Reads UTF-8 text files from the Agent workspace or an absolute path.

## Interfaces

- Tool name: `read`
- Registration: `register_read_tool(registry)`
- Schema: required `path`; optional positive 1-indexed `offset` and `limit` line controls; `additionalProperties: false`.
- Success: `{ ok: true, data: { content }, error: null, artifacts: [] }`
- Display: summary field `path`.

## Conventions

- Relative paths resolve from `ToolContext.workspace`; absolute paths are allowed.
- `read` is the authoritative read-like tool and must not include a provider/tool parameter named `description`.
- Successful results do not include `data.path`; the agent already knows the requested path from arguments.

## Constraints & Gotchas

- File bytes decode as UTF-8 with replacement.
- Output is truncated by built-in line/byte limits.
- Expected file, argument, and read-time filesystem errors return failure envelopes.
