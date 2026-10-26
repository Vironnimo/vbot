# execute_code Tool

Runs a fresh Python program that calls the current Run's allowed Tools and processes their results before choosing what to print for the Model.

## Ownership and entry points

`core/tools/execute_code.py` owns the Tool definition, argument reading, process admission and result/display projection. `_code_process.py` owns one child process, its authenticated loopback bridge, bounded output and cleanup; `_code_worker.py` is the standard-library child and its `vbot_tools` client. The child imports no vBot runtime. Chat's `core/chat/_programmatic_tools.py` owns nested dispatch and durable child activity; its shared contract lives in `tools/registry-dispatch.md` -> ToolContext.

The Tool follows `bash` activation (the Model name is `powershell` on Windows), belongs to the execution family, and also checks that the current Run can call that shell Tool. Its definition follows the ordinary On-demand Tool policy; there is no separate catalog or System Prompt block. The outer call is a scheduling barrier and holds no execution slot while its children use normal slots, avoiding a parent waiting for capacity it occupies itself.

## Program and client contract

Only `code` is advertised. The normalizer accepts plain Python source and the `python`, `script`, `source` and `command` aliases without rewriting the program. Each call starts a new interpreter in `ToolContext.effective_cwd`, with a 300-second hard deadline. Imports, variables and threads never carry into another call. The bundled vBot interpreter runs the child; a project's activated environment does not select another interpreter. After the trusted bootstrap imports finish, the working directory joins Python's import path, so ordinary project imports work when their dependencies are installed in that interpreter.

The program runs like `python program.py`: the host writes the unchanged source to a temporary `program.py` (removed with the cell), and the child runs it as `__main__` with that `__file__`, so `multiprocessing` and `ProcessPoolExecutor` can import its functions. After the program, the child takes Python's exit steps: it waits for non-daemon threads and runs `atexit` handlers before reporting its exit code. Tracebacks omit the child's own frames. Source that Python cannot compile is reported as not run (`CodeExecutionResult.ran`).

- `from vbot_tools import tools` supplies synchronous `tools.<name>(**arguments)` calls using the ordinary Tool names and parameters. `from vbot_tools import read` is accepted too. Name resolution and argument repair remain the normal dispatch owners' responsibility.
- `tools.call(name, arguments_dict)` handles a dynamic name or a parameter that cannot be written as a Python keyword. Both forms also accept a positional argument dictionary plus additional keyword arguments; a duplicated argument, and a positional value that is not one dictionary (`tools.read("a.pdf")`, seen in the 2026-10-26 eval), are refused with a `TypeError` before sending. The routing name and argument-dictionary parameters are positional-only, so a Tool's own `name` or `arguments` fields do not collide with the client.
- A successful call returns the Tool envelope's `data`, already decoded as Python data. It is not the Model's plain-text rendering and needs no `json.loads`, but a Tool's `data` can itself be a rendering: `read` returns its numbered, paginated page in `content`. Local files and commands therefore go through Python's own `open()` and `subprocess`, which have the same authority (Authority and lifetime). Decoded objects are `dict` subclasses whose missing key raises a `KeyError` naming the keys the result has, so a guessed key corrects itself from the traceback. A failure raises `ToolError`, importable from `vbot_tools`, with `tool`, `code`, `message` and the complete `result` envelope. Catching it does not remove the failure from the outer execution's failure count and bounded failure summary.
- Inputs must serialize as finite JSON. Convert objects such as `Path` to the Tool's documented string representation. The client refuses an unserializable or oversized request before sending it. Calls from ordinary Python threads have independent connections, while Chat's child batch still enforces Tool concurrency limits and serial barriers.
- MCP Tools return the complete preserved protocol payload, including content blocks and any `structuredContent`, before the Model-facing preview and text-flattening step. Binary media still follows MCP's attachment preservation; this is not a promise to return the original base64 bytes. Other Tools keep their own data contracts, including pagination or bounded reads (`extensions/mcp.md`).
- Tool definitions still come from ordinary `load_tools` and MCP discovery. Calling `load_tools` inside Python does not mark its unprinted definitions as known by the Model. Explicit Skill activation and Project-context changes retain their normal context effects; hiding a child Result is not a way to suppress those effects.

The bridge permits at most 1,000 Tool calls per execution, 2 MiB per request and 32 MiB per response. A response over its bound is a failure after the Tool ran, never a refusal that promises no effects. Recursive `execute_code` calls are refused. The bridge never reconnects and replays an unanswered request: after a connection loss, whether a mutation ran is unknown.

## Authority and lifetime

The user chose the existing shell/process trust model for Python execution (2026-10-26). Before launch, `execute_code` checks the start directory, reuses `CommandChecker.check_argv` for the equivalent Python `-c` command, and builds the same restricted or unrestricted shell environment. This adds no Python sandbox: an admitted program's internal filesystem, network and subprocess operations have the same limitations as an admitted shell-launched Python program (`permissions.md` -> Requirements and honest limits). The private bridge grants only the current Run's Tool authority; it is not the public RPC API.

Nested calls keep the same Agent, Session, Run, Permission Boundary, grants and live denials. They pass normal Extension hooks, secret handling, result validation and persistence acknowledgement. Each child has its own invocation identity and its parent Tool-call id. The durable child declarations, Results and media remain visible in History, but the child messages are excluded from Provider requests, Compaction input and Recall content. The printed outer result is the Model-facing record. Persistence and projection details belong to Chat and Sessions, not the Python worker.

The process owner stops and drains its process tree and bridge requests on completion, failure, deadline or cancellation. Normal cleanup terminates daemon threads and subprocesses that are still running; if the operating system refuses termination, the result reports that termination could not be confirmed instead of waiting indefinitely. Stopping only this Tool ends the program like the deadline does and returns its result with status `stopped` and the output printed until then (the timeline shows it as cancelled, like a stopped `bash` call); a stop that arrives before launch returns `cancelled_by_user` with nothing run. Cancelling the Run propagates through normal Run cancellation. Completed child effects remain durable even when the parent fails or stops. There is no transaction rollback or automatic rerun of the program.

## Output and recovery

The result reports the Python execution's status, selected stdout, exit code when known, stderr when present, and Tool-call/failure information. A successful Tool envelope alone does not imply that the Python program completed successfully: inspect its execution status. Error and timeout outcomes carry an explicit notice in the Tool display. The source program and its output have normal input/output detail blocks.

Inline stdout keeps up to 64 KiB and stderr up to 16 KiB, preserving head and tail with an omission marker. When a Scratch Area is available, a capture file keeps stdout and stderr up to 8 MiB; shortened output names that file, and the file is deleted when nothing was shortened. The file's own truncation is stated separately, so it is never presented as complete after reaching its cap. Read the saved output instead of rerunning a program that already produced effects. A script that catches Tool errors still reports their count, retaining at most 20 summaries.

## Usage examples

These examples illustrate the client contract; choose Tools from the actual Session's definitions. A normal loop filters results before printing:

```python
from vbot_tools import tools

matches = []
for path in ["a.txt", "b.txt"]:
    result = tools.read(path=path)
    if "TODO" in result["content"]:
        matches.append(path)
print(matches)
```

A dynamically selected Tool uses the same arguments, with Python-only objects converted before the call:

```python
from pathlib import Path
from vbot_tools import tools

name = "read"
arguments = {"path": str(Path("notes.txt"))}
print(tools.call(name, arguments)["content"])
```

Catch a Tool failure only when the program has a deliberate continuation; this example reports it without replaying it:

```python
from vbot_tools import ToolError, tools

try:
    print(tools.read(path="notes.txt")["content"])
except ToolError as error:
    print({"tool": error.tool, "code": error.code, "message": error.message})
```

## Agent-facing text

This file owns the rationale for the definition and execution-specific results/errors in `execute_code.py`, `_code_process.py` and `_code_worker.py`. Shared Tool refusals and validation text remain in `tools/agent-facing-text.md`. The import and decoded-result guidance is supported by Hermes' documented production failure classes in its `code_execution_tool.py::_FAILURE_HINT_RULES` (source inspected 2026-10-26): wrong imports, treating result dictionaries as strings and absent interpreter packages. Other rows describe the contract's concrete first-use or recovery requirement, not a measured vBot Model failure.

| Text | Reason |
|---|---|
| `Run Python to combine Tool calls and process their results.` | Distinguishes programmatic orchestration from a single Tool call or a plain shell command, without a mandatory call-count threshold. |
| `Combine Tool calls with Python.` | Gives the On-demand Tool list the capability and language in a short selection hint. |
| `Python source.` | Fixes the language of `code`; a shell command or JSON argument object is not executable source here. |
| `Import tools with ... then call any of your Tools as ...` | Supplies a complete first-call pattern and reuses known Tool names and parameters, avoiding invented SDK names or hand-built RPC. The placeholder form replaced a `tools.read(...)` example: it steered programs to `read`, whose `data` is a numbered, paginated page, so scans and parsers silently saw prefixed or cut content (review 2026-10-26). |
| `Calls return the result's data as Python objects; failures raise vbot_tools.ToolError.` | Prevents double JSON parsing and treating an error envelope as successful loop data. |
| `For local files and commands, use Python's open() and subprocess.` | Keeps file contents and command output out of `read` and shell renderings (line gutters, pages, background hand-off) that a program would process as data. |
| `MCP results retain content blocks and structuredContent.` | Prevents assuming the Model's flattened MCP text or a bare `structuredContent` value is the whole Python result. |
| `Each execution starts fresh in the working directory and stops after 300 seconds.` | Prevents relying on a previous call's variables, an implicit temporary working directory or an unbounded execution. |
| `Print the results you need.` | Makes the output-selection step explicit; an unprinted intermediate result is deliberately absent from Model context. |
| `Nothing was run: execute_code cannot call itself.` | Identifies recursion as a pre-execution refusal instead of letting nested interpreters consume capacity indefinitely. |
| `Continue in this Python program.` | Names the available continuation after a recursive-call refusal. |
| `Nothing was run: Python execution needs the same access as ...` | Explains a shell-authority refusal without suggesting that the new Tool bypasses it. |
| `Nothing was run: execute_code needs an active conversation Run.` | Explains why an invocation without Run-bound dispatch cannot execute. |
| `Nothing was run: Python could not compile the program. Fix the error in stderr.` | Replaces the partial-effects note for a syntax error, which otherwise sent Agents to inspect effects of a program that never started. |
| `Nothing was run: the user stopped this Python execution before it started.` | States that a stop before launch left nothing to inspect. |
| `The user stopped this Python execution.` | Identifies the user-requested stop instead of reporting a program defect; the `stopped` result keeps the output printed until then for the inspection the next sentences ask for. |
| `Earlier Tool calls and Python changes remain applied.` | Prevents treating cancellation, a timeout or a later Python error as rollback. |
| `Inspect their outcomes before repeating any changes.` | Gives the safe next step when a partial program already ran. |
| `Output was shortened.` | Makes omitted output observable instead of presenting the retained head and tail as complete. |
| `The program already ran; do not repeat its effects.` | Prevents rerunning a mutating script merely to recover omitted output. |
| `Read output_file for the captured output.` | Gives the recovery path when a capture file exists. |
| `The captured output file reached its size limit and is incomplete.` | Prevents the file recovery path from implying unlimited retention. |
| `Python reached its 300-second time limit.` | Names the execution deadline as the cause of a stop. |
| `The Python program failed.` | Distinguishes a script failure from a Tool admission refusal. |
| `Nothing was sent: Tool arguments must be finite JSON data within 2 MiB.` | States that serialization or size failure happened before remote effects. |
| `Convert unsupported values or reduce the input.` | Gives the correction for Python-only objects or an oversized request. |
| `Nothing was sent: the Python Tool bridge is unavailable.` | Distinguishes a connection failure before sending from an unknown outcome after sending. |
| `The Tool connection ended without a usable result.` | Reports lost response evidence without claiming success or refusal. |
| `Whether the call took effect is unknown.` | Prevents interpreting a disconnected mutation as unapplied. |
| `Check its state before repeating a change.` | Gives the recovery step for an unanswered Tool call. |
| `The Tool ran, but its result exceeds the Python bridge's 32 MiB limit.` | Makes clear that a large response fails after execution, not before effects. |
| `Request a smaller page or range.` | Gives the next request shape for an oversized result. |
| `Check the current state before repeating a call that changes anything.` | Keeps the size-recovery advice from blindly replaying a mutation. |
| `A Tool call needs its name and arguments.` | Explains a malformed bridge message before dispatch. |
| `Tool arguments must be a Python dictionary.` | Names the required argument representation before dispatch. |
| `The Python execution has ended.` | Prevents a late request from appearing to have executed after its parent finished. |
| `The Python execution reached its ... Tool-call limit.` | Identifies the admission bound reached by a loop; further child calls did not run. |
| `The Python execution ended while this Tool call was unfinished.` | Preserves an unfinished child's outcome when parent cleanup cancels it. |
| `Pass each Tool argument once, in the dictionary or as a keyword.` | Refuses conflicting call representations before either value can be silently selected. |
| `Nothing was sent: pass the arguments of <name> by name, such as tools.<name>(<parameter>=<value>), or as one dictionary.` | Replaces Python's misleading `dict()` error for a bare positional value, which sent a Model to inspect the client instead of fixing the call (eval 2026-10-26). |
| `'<key>' is not in this Tool result. Its keys: ...` | Lets a program that guessed a `data` key (no Tool definition documents `data`) correct it from the traceback instead of an exploratory call. |

## Verification

`tests/core/tools/test_execute_code.py` covers Tool admission, unchanged source, shared Permission Rules/environment, partial output and per-Tool versus Run cancellation. `tests/core/tools/test_code_process.py` covers the real child/client boundary, threaded calls, keyed result data, `__main__` script semantics, compile failures, keyword collisions, finite-JSON inputs, bounded capture, no replay, process-tree cleanup on stop and cancellation, and the hard deadline. `tests/core/chat/test_programmatic_tools.py` covers inherited authority, hooks, durable payload acknowledgement, context exclusion, serial barriers and cancellation. MCP payload preservation is covered by its existing content tests; projection ownership and tests are linked from `tools/registry-dispatch.md`.
