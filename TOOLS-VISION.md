# Tools Vision

## Purpose

vBot tools exist to give the agent real host-level agency. The workspace is the
agent's home and default working directory, not a sandbox. Relative paths
resolve from the workspace by default; absolute paths are allowed.

## Product direction

- High agency, minimal restrictions.
- Tools should let the agent work in the repo, in its workspace, and on the
  wider machine.
- We are not designing a locked-down sandbox.
- The denylist should stay very small and focus only on clearly catastrophic
  host destruction.
- Explicit user intent wins: if the user tells the agent to shut down the
  machine, the agent may do it.
- Recursive deletion is not categorically blocked.
- We should avoid speculative safety rules that reduce usefulness without
  protecting against real catastrophic damage.

## Parallel execution is a core requirement

Parallel tool execution is not a nice-to-have and not a distant future idea. It
is part of the intended execution model.

- If the assistant emits multiple tool calls in one step, the runtime should
  treat them as one internal parallel batch.
- A tool call should start as soon as that individual call is complete enough
  to execute. The runtime should not wait until the whole batch has been fully
  received before starting the first tool call.
- Tool calls in the same batch should execute concurrently by default,
  including multiple calls to the same tool.
- The next model request should not start until every tool call in that batch
  has reached a terminal state.
- Live events may interleave in real execution order.
- Persisted results should still be deterministic and use the assistant's
  original tool-call order.
- Every tool call must have a stable `tool_call_id` that is used everywhere:
  events, persistence, logs, and follow-up assistant context.
- Batch is an internal runtime concept only. The UI should not expose batch
  concepts.

`read` v1 does not have to prove the whole parallel system by itself, but the
architecture we start now should already fit this model.

## Foundational decisions

These should be treated as early architecture decisions, not tool-specific
details.

- Tools should receive a small typed, mostly read-only `ToolContext` plus
  `arguments`, not just raw arguments.
- Tool results should use a stable envelope that can return either inline data
  or future artifact references.
- Tool failures should not end the run. A tool execution failure, exception, or
  child-process crash should be turned into a structured tool result and
  returned to the agent.
- Tools should be isolated enough that handler failures do not damage the app
  runtime or the agent loop.
- Execution failures inside a tool should come back to the agent as structured
  tool results. Only runtime-integrity failures outside normal tool execution
  may fail the run.
- Tool lifecycle is first-class. The backend should support generic tool
  lifecycle events now in principle, even if only some of them are used at
  first.
- Tool names should stay flat, simple, and stable. No namespacing like
  `fs.read`.
- Built-in tools should be registered at runtime start.
- Registration should stay simple enough that a tool can live in its own file
  and be wired in with a small obvious registration step.
- Parallel tool execution is a core runtime assumption, not an optional extra.
- Same-turn sibling tool calls should execute concurrently by default.
- The same tool may run multiple times in parallel.
- There should be no tool-level exclusivity restrictions in the base design.
- Default concurrency limits should be 50 active tool calls per run and 50
  active tool calls globally, with configuration available later.
- The backend should not assume that tools will always run strictly one after
  another.
- Internal batch concepts should not leak into the UI.

## What a vBot tool should be

A runtime tool should have these parts:

- `name`
- `description`
- `parameters`
- `handler(context, arguments)` (runtime implementation)

Only `name`, `description`, and `parameters` should be sent to the model / API.
The `handler` is internal.

### `ToolContext`

The exact shape can evolve, but the direction should be clear now. Context is
where runtime-owned execution information belongs, for example:

- agent identity
- run identity
- tool call identity
- internal batch identity if needed later
- workspace path
- app root / data root
- event emission hooks
- cancellation hooks
- future execution helpers

That keeps tool schemas small and avoids leaking runtime plumbing into model
visible parameters.

#### `ToolContext` shape

`ToolContext` should be a real runtime object, not a loose dictionary.

Minimum stable fields:

- `agent_id`
- `session_id`
- `run_id`
- `tool_call_id`
- `tool_name`
- `tool_call_index`
- `workspace`
- `app_root`
- `data_root`

Useful hooks / helpers:

- `emit(event_type, payload)`
- `is_cancelled()` or equivalent cancellation token
- `create_artifact(...)` for future large-output tools

Optional:

- `logger`

This should stay small, stable, and mostly read-only.

### `name`

- Short, stable, and explicit
- Prefer verb-first names
- Prefer provider-friendly identifiers such as `read`, `glob`, `grep`, `write`,
  `bash`, or `write_file` when one word is not enough
- Avoid vague names like `file_tool` or overloaded names like `do_everything`
- Avoid unnecessary words when the shorter name is already clear

Good:

- `read`
- `write`
- `bash`

Bad:

- `read_file_from_local_filesystem`
- `tool_for_reading_text`
- `misc`

### `description`

- Short and literal
- Say what the tool does, when to use it, and what kind of result it returns
- Do not waste tokens on marketing language, repetition, or long examples
- Mention only the constraints the model actually needs to know

Good:

- `Read a text file from disk. Relative paths resolve from the workspace.`

Bad:

- Long paragraphs
- Repeating parameter docs already obvious from schema
- Telling the model how amazing or powerful the tool is

### `parameters`

- JSON Schema object
- Keep it minimal
- Prefer flat shapes over deeply nested input
- Use clear parameter names so the description can stay short
- Only include parameters the model really needs
- Use `required` and `enum` where they genuinely reduce ambiguity
- Avoid optional parameters that exist only for future flexibility
- Prefer `additionalProperties: false` when that matches the intended contract

## Tool design principles

- One tool should do one clear job
- Tools should be easy for the model to choose between
- Tool behavior should be predictable, boring, and easy to infer
- Do not hide multiple unrelated actions behind one tool
- Prefer explicit parameters over magical behavior
- Prefer stable outputs over clever outputs

## Tool result principles

- Tool results should always be JSON objects
- Tool results should follow one stable result envelope
- Results should be compact, structured, and machine-friendly
- Return the data the model needs for the next step, not an essay
- Prefer stable fields over free-form text
- Tool failures should come back as structured tool results
- Runtime should catch tool exceptions and convert them into structured failure
  results instead of killing the run
- The agent should be able to inspect a failed tool result and decide what to
  do next
- Success and failure should use the same top-level envelope fields

### General result envelope

Success:

```json
{
  "ok": true,
  "error": null,
  "data": { "path": "C:/...", "content": "..." },
  "artifacts": []
}
```

Failure:

```json
{
  "ok": false,
  "error": { "code": "not_found", "message": "File not found" },
  "data": null,
  "artifacts": []
}
```

Notes:

- `data` is the normal inline result payload
- `artifacts` is for future large outputs, files, or references
- `artifacts` will often be empty for small tools like `read`
- We should keep the envelope stable even when individual tools differ inside
  `data`

### Public tool event model

Public tool events should stay simple and align with the current server naming
direction.

Public event names:

- `tool_call_started`
- `tool_call_result`
- later, for streaming tools: `tool_call_stdout`, `tool_call_stderr`,
  `tool_call_cancelled`

There should not be a separate public `tool_call_failed` event. Tool failures
should appear as `tool_call_result` with `result.ok = false`.

Every public tool event should carry at least:

- `tool_call_id`
- `tool_name`
- `tool_call_index`

`tool_call_started` payload shape:

```json
{
  "tool_call": {
    "id": "call_1",
    "index": 0,
    "name": "read",
    "arguments": { "path": "README.md" }
  }
}
```

`tool_call_result` payload shape:

```json
{
  "tool_call": {
    "id": "call_1",
    "index": 0,
    "name": "read"
  },
  "result": {
    "ok": true,
    "error": null,
    "data": { "path": "README.md", "content": "..." },
    "artifacts": []
  }
}
```

Example success shape:

```json
{
  "ok": true,
  "error": null,
  "data": { "path": "C:/...", "content": "..." },
  "artifacts": []
}
```

Example expected failure shape:

```json
{
  "ok": false,
  "error": { "code": "not_found", "message": "File not found" },
  "data": null,
  "artifacts": []
}
```

## API / token best practices

Tool definitions cost context tokens. They are not free. We should treat every
tool definition and every tool result as part of the prompt budget.

That means:

- Keep tool names short
- Keep descriptions short
- Keep schemas small
- Keep parameter sets minimal
- Keep results compact
- Do not expose tools the current agent does not need
- Do not duplicate information across name, description, schema, and result if
  one place already makes it clear

There is no single cross-provider perfect standard we should optimize around.
The practical standard is simpler: stay comfortably small, clear, and boring.

### Practical compactness guidelines

- Prefer 1-3 word tool names
- Prefer 1-2 short sentences for descriptions
- Prefer shallow parameter objects
- Prefer a few strong parameters over many weak ones
- Avoid giant enums, long inline examples, and verbose schema prose
- Avoid returning huge raw payloads when a smaller structured response would do

## Provider compatibility principles

Our internal tool shape should map cleanly to the common provider tool formats.

- OpenAI-compatible providers want function-like tool definitions
- Anthropic wants tool definitions with `input_schema`
- We should keep our tool contracts provider-agnostic and simple enough to map
  cleanly to both
- We should not design tools around provider-specific quirks unless a real tool
  needs it

## Concurrency model

This is the runtime model for parallel tools.

- One assistant message can produce multiple tool calls.
- Those tool calls form an internal batch.
- A tool call should start as soon as its own name and arguments are complete
  enough to execute deterministically.
- The runtime should not guess readiness from parseable partial fragments.
- Provider adapters should emit an internal `tool_call_ready` signal when one
  specific tool call is fully assembled and safe to start.
- The runtime should start one execution unit per tool call.
- The runtime should wait for the full batch before making the next provider
  request.
- Each tool call should get its own `ToolContext` instance.
- Lifecycle events should stream in real execution order.
- Persisted tool results should use the assistant's original tool-call order so
  history stays deterministic.
- Cancellation of the run should cancel all active tool calls in the batch.
- If concurrency limits are reached, additional tool calls should wait for a
  slot instead of failing the run.
- Default limits should be 50 active tool calls per run and 50 active tool
  calls globally.
- The same tool may run multiple times in parallel.
- No base-design tool exclusivity restrictions are planned.
- Tool lifecycle events must carry enough identity to correlate outputs to the
  correct tool call.
- We should avoid early backend assumptions that only one tool can be active at
  a time.
- Batch should stay an internal scheduling concept and should not appear in the
  UI.

### Implementation requirements

- Create one async task per tool call in a batch.
- For streamed tool-call generation, start each tool as soon as that specific
  tool call is fully assembled; do not wait for the full batch to finish
  arriving.
- For streamed tool-call generation, adapters should emit an internal
  `tool_call_ready` signal carrying the fully assembled tool call.
- Use the tool call ID as the primary correlation key.
- Emit lifecycle events per tool call, not just per batch.
- Collect all terminal results before resuming the model.
- Preserve a deterministic persistence order even if runtime completion order is
  different.
- Enforce concurrency through semaphores or an equivalent async scheduling
  primitive.
- Maintain both a per-run limit and a global limit, both defaulting to 50.
- Queue excess tool calls until slots are available.

## Registration direction

Built-in tools should be registered at runtime start.

- Prefer one implementation file per tool when practical
- Prefer a tiny, obvious registration step
- The path for adding a new built-in tool should stay boring and cheap
- If this stays simple enough, it can also become the basis for custom tools
  later

## Long-output principle

Some future tools, especially `bash`, may produce more output than is useful to
send back in one shot. The backend should eventually support compact summaries,
truncation, chunking, or follow-up reads instead of forcing giant payloads into
one tool result.

## What comes next

The next milestone is not "tools in general". It is a single real tool:

1. `read` v1

After that, we evaluate real tool-calling, persistence, streaming, and model
behavior before deciding the next tool.

## read v1 scope

Goal: validate the full end-to-end tool path with the smallest useful real
tool.

### Included

- Read text files only
- One tool: `read`
- Required input: `path`
- Optional inputs: `offset`, `limit`
- `offset` / `limit` should be line-based, not byte-based
- Relative `path` resolves against the agent workspace
- Absolute `path` is allowed
- Return file contents as text
- Integrate fully with normal tool calling, run events, session persistence,
  and streaming-visible lifecycle

### Excluded

- `write` / `edit` / `apply_patch`
- `bash` / shell execution
- Binary files
- Image / PDF parsing
- Broad policy machinery
- Complex batching or search features

### Result shape

`read` v1 should follow the general tool result principles above: compact,
structured, and recoverable for expected failures.

This lets the model recover and try another path instead of crashing the whole
run.

`read` v1 success shape:

```json
{
  "ok": true,
  "data": { "path": "C:/...", "content": "..." },
  "artifacts": []
}
```

`read` v1 failure shape:

```json
{
  "ok": false,
  "error": { "code": "not_found", "message": "File not found" },
  "data": null,
  "artifacts": []
}
```

### Open read v1 details

- Maximum file size / response size
- Exact text decoding behavior for non-UTF-8 files
- Whether returned content should include line numbers

## Guardrail philosophy

The tool system should not try to protect the user from every risky action. It
should only stop a very small class of obviously catastrophic host-destruction
actions.

### Allowed by default

- Access outside the workspace
- Absolute paths
- Repo modifications
- Recursive deletions
- Process control commands such as shutdown / reboot 

### Still blocked

A small, explicit denylist of actions whose main effect is catastrophic system
destruction, for example:

- Obvious "destroy the whole machine / whole disk / whole OS" commands
- Commands that are effectively equivalent to wiping or bricking the host

The exact denylist is intentionally small and will likely need real-world
iteration.

## Future note for `bash`

`bash` is not in the first milestone, but the backend should be shaped so we
can support it cleanly later.

When `bash` arrives, the backend should support:

- Separate stdout and stderr internally
- Streamed output chunks
- Final exit code
- Timeout
- Cancellation
- Working directory reporting
- Truncated / large-output handling
- A final result object that still preserves raw execution facts

Public event model for streaming-output tools:

- `tool_call_started`
- `tool_call_stdout`
- `tool_call_stderr`
- `tool_call_result`
- `tool_call_cancelled`

These public event names are already decided here. Exact payload details for
streaming-output tools such as `bash` can be finalized when those tools are
implemented, but the backend should be built to support this model cleanly from
the start.

## Success criteria for `read` v1

We should consider `read` v1 successful when we can observe one real run where:

- The model calls `read`
- The tool executes correctly
- The tool call and result are persisted
- The run streams correctly
- The assistant continues after the tool result and uses the file contents
  correctly
