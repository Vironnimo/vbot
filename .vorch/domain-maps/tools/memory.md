# Memory Tool

Pinned memory CRUD over `USER.md` and `MEMORY.md`.

## Interfaces

- Tool name: `memory`
- Registration: `register_memory_tool(registry, memory_service)`
- Bound service: `MemoryService`
- Model-facing schema: one flat object with required `action` (`list`, `add`, `replace`, or `remove`) and `scope`, plus optional sibling fields `content` and `entry_id`; it has no `additionalProperties` keyword or defaults. Descriptions state each field's action dependency, and the handler rejects missing, inapplicable, unknown, or malformed arguments. Retired nested `request.operation` and operation-key shapes are rejected.
- `scope`: one of `user` or `agent`.
- `content`: required for `add` and `replace`.
- `entry_id`: required for `replace` and `remove`; 1-based id from the current list response.

## Result Contract

- All responses use the stable tool result envelope.
- Success data always includes `content` and `scope`.
- `list` returns `entries` and derives an exact presentation-only `results` count from that array for the Tool row.
- Mutations return the affected `entry` and the updated `entries` list.
- Mutations and failures do not publish a display count even though successful mutations carry the updated array.
- Invalid schema-level values return `invalid_arguments`.
- Expected memory validation or I/O failures return `memory_error`.

## Constraints & Gotchas

- The tool edits only the `## Entries` section managed by `core/memory/`.
- Existing prose in `USER.md` or `MEMORY.md` must be preserved.
- Use `user` scope for durable user facts and `agent` scope for stable agent/workflow notes.
- The tool is for concise, durable facts. It is not a session transcript, scratchpad, or broad recall search surface.
- Agent `memory_prompt_mode` controls whether and which pinned memory files are inserted into the system prompt. Tool availability only controls whether the model can call the memory CRUD tool.
