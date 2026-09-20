# Designing Agent-Facing Tools

This is the design map for the agent-facing part of every Tool: the model-facing definition the Model sees. Read it whenever work touches that surface - adding a Tool, changing its public contract, migrating an older shape, or editing a description, parameter, or model-facing schema. It owns the design rules; concrete per-Tool behavior contracts live in the sibling maps under `tools/`, routed from `tools.md`.

## Purpose and Priority

A Tool definition helps a fresh Agent choose a capability, supply a complete request, and achieve the intended result. Optimize the whole task: correct effects and useful evidence first, clear first-use guidance next, then unnecessary context and call cost. A shorter definition that causes empty operations, misleading results, or extra discovery calls is an unsuccessful optimization. Provider portability must preserve these semantics.

Start with the user task and the observable outcome the Tool should enable. Its current name, schema, parameters, description, results and division into Tools are revisable design choices, not constraints to preserve. Prefer the simplest complete workflow an Agent can use independently; recurring errors may call for redesign rather than another instruction sentence. Judge context footprint across definitions, arguments, results and repeated history together with unnecessary discovery, execution, verification, recovery and continuation calls. Preserve correctness and needed guidance while reducing that total cost.

Apply the same efficiency during redesign and testing: deterministic tests cover known argument/runtime variants; focused Model trials resolve actual uncertainty about Tool choice and use. Batch independent checks, reuse relevant evidence and expand trials for observed failures or uncertainty. Do not use exhaustive exact-JSON Model matrices as a usability gate or repeatedly rerun unchanged passing checks.

## Model Contract and Runtime Contract

- The model-facing Tool definition describes only what the Model needs to choose and call the Tool.
- Runtime behavior follows `../tools.md` -> Contracts -> Agent error tolerance: when operation, target, values, scope, and explicit constraints remain reliably identifiable, process the intended request despite mistakes in its expression; a unique similar schema match alone does not establish that intent. The advertised schema teaches the preferred call shape; it is not a reason to reject an understandable request.
- The handler owns actual defaults, normalization, conditional validation, authorization, security checks, side effects, and error handling.
- A permissive model-facing schema is acceptable when its descriptions reliably guide the Model and the handler safely validates execution.
- Do not expand the model-facing schema merely to encode every invalid runtime state.

## Tool Names

- Use a short, stable, descriptive `snake_case` name.
- Prefer an established domain noun or verb. Do not include implementation details, Provider names, transport names, or versions.
- Keep one Tool responsible for one coherent capability. Separate genuinely different discovery and execution operations when combining them would make invocation ambiguous.

## Tool Descriptions

- State when to use the Tool, what it does, and only the operational limitations that change how the Model should call it. Say what the Tool accomplishes and any decision the Agent must make; do not spend the description teaching the Model to construct an avoidably complex envelope.
- Every Tool must have a clear description. Context efficiency comes from removing redundancy and irrelevant implementation detail, not from withholding guidance the Model needs.
- Lead with the action and its observable result, such as editing file contents or finding matching lines. Explain targeting or context as part of that action, never as a substitute for the action itself. An editing Tool must teach how to supply the change as well as where it belongs.
- Keep enough guidance for the first complete call. Remove words that add no decision value; brevity is not a separate acceptance criterion.
- Schema types and names do not explain how arguments work together. Repeat a schema fact only when it resolves a likely misunderstanding about the requested effect.
- Do not describe handler internals, libraries, storage layout, UI rendering, logging, telemetry, or validation machinery unless the Model must account for it when calling the Tool.
- Mention result behavior only when it affects Tool choice, pagination, follow-up calls, or safe use.
- Do not reference another Tool unless that Tool is guaranteed to be available in the same Model context. Availability-dependent guidance belongs in a dynamically gated prompt block.

## Choose the Public Shape

Use this decision order:

1. **One behavior:** expose its arguments directly in one open flat object. A Tool named `channel_send` that only sends should accept delivery fields directly; `action: "send"` would repeat the Tool name.
2. **One repeatable independent behavior:** use one required plural array of compact operation objects when batching materially reduces Agent roundtrips. State ordering semantics in the array description, preserve input order, keep per-operation options on each item, and report indexed outcomes.
3. **One behavior with optional targeting or selection:** keep direct optional target fields and validate their dependencies. `status()` checks the current Session, `status(session_id)` checks another Session for the same Agent, and `status(agent_id, session_id)` changes the owner and Session; these are target variants, not actions.
4. **Several genuinely different behaviors:** require one top-level `action` enum and place every action argument beside it. CRUD, lifecycle transitions, and read-versus-mutate behavior normally qualify. `memory(action, scope, content?, entry_id?)` and `history(action, ...)` are the reference shape.

Do not expose `request.operation`, an operation-key object such as `{"create": {...}}`, a stringified nested request, or several mutually exclusive booleans that encode actions. Do not infer a behavioral mode from an arbitrary combination of optional fields when a required `action` would state it directly.

Action Tool mechanics:

- Represent an Action Tool as one flat object. Make `action` a required string enum whose description names every action in operational language.
- Put all arguments used by any action in the same `properties` object.
- Leave action-dependent arguments out of the root `required` list and state their action dependency briefly in their descriptions.
- Do not represent actions with `oneOf`, `anyOf`, `allOf`, conditional schemas, or duplicated per-action object branches.
- The handler must validate the selected action's required arguments and return a concise actionable error for an invalid combination.

Example:

```json
{
  "type": "object",
  "properties": {
    "action": {
      "type": "string",
      "enum": ["list", "read", "write"]
    },
    "id": {
      "type": "string",
      "description": "Required for read and write."
    },
    "content": {
      "type": "string",
      "description": "Required for write."
    }
  },
  "required": ["action"]
}
```

## Parameters

- Keep the parameter set minimal and Agent-relevant. Do not expose implementation details such as internal source paths, executable flags, storage layout, working directories, or cache controls unless choosing them is part of the user's requested behavior.
- Represent one concept once. Prefer one normalized field over several overlapping schedule, selector, or mode fields; do not preserve aliases in the canonical Tool contract.
- When two Tool names separate discovery from exact retrieval but share the same authority and lifecycle, keep one configurable capability and derive the reader as a companion. A search result should return the reader's directly callable argument shape so the Agent does not translate identifiers or depend on both toggles being configured independently.
- Use a plain JSON Schema object with `type`, `properties`, and `required`. Never emit `additionalProperties` in a model-facing Tool schema.
- Put a field in `required` only when every valid call requires it. Make a field optional by leaving it out of `required`. Do not add `null`, nullable unions, sentinel values, or duplicate optionality metadata. Use `required: []` for a Tool with no required arguments.
- Use the narrowest simple type that preserves the callable interface. Use an enum when the Model must choose from a small fixed vocabulary. Add bounds, patterns, length limits, or format hints only when they materially help the Model construct a valid argument; runtime-only constraints do not belong in the model-facing schema.
- Do not add a root parameter-object description unless it conveys conditional argument rules that cannot be stated more compactly on the affected properties.
- Make a field optional only when omission has a real default, an unambiguous derived fallback, or selects the current target.
- Use stable IDs returned by prior Tool Results for follow-up mutations. If IDs can shift, say when the Agent must list or refresh first.
- Use one vocabulary consistently: `action` for a domain-behavior discriminator, `mode` only for a genuine execution contract such as Bash foreground/background, domain names for arguments, and existing project terms for entities. Do not alternate between `action`, `operation`, `mode`, and `type` for the same role.

## Parameter Descriptions

- Describe the value the Model should provide, not the Python type or handler variable.
- Every parameter must have a concise description that makes its purpose clear and, when optional, tells the Model when to omit it. A short self-evident description is preferable to no description.
- State the omission condition from the Model's decision point, for example `Omit when no specific destination is given.` Add the resulting omission behavior or runtime default only when it materially affects that decision.
- For a conditionally required parameter, state the relevant action or mode directly, for example `Required for write and submit.`
- Do not repeat `required`, enum values, numeric bounds, or defaults in prose unless the repetition resolves a real ambiguity.
- Use short canonical examples when syntax or the relationship between arguments needs explanation. Each example must perform the advertised operation: include the target and the actual change or query, not just a locator or context fragment. Show omitted optional fields as omitted, not as `null`, unless `null` is meaningful. Validate examples against independently specified results.

## Defaults

- The handler is the sole authority for every actual default.
- Do not emit the JSON Schema `default` keyword for strings, booleans, enums, arrays, objects, or null values.
- A numeric `default` may be emitted only when omission is common, the number materially helps the Model plan the call, the value is stable, and the handler applies exactly the same value.
- Do not add a numeric `default` merely because the handler has one. Pagination sizes, result limits, offsets, and timeouts are typical candidates; incidental implementation constants are not.
- Explain a non-numeric omission result in the parameter description only when it materially affects whether the Model should omit the field.
- Never rely on a Provider to apply a default or inject an omitted argument.

## Schema and Handler Boundary

- The canonical model-facing input has one open JSON-object root with `type`, `properties`, and `required`.
- The schema owns field names, simple JSON types, small fixed enums, universal requirements, and only constraints that materially help the Model construct a valid call. The handler owns unknown fields, conditional requirements, inapplicable fields, actual defaults, cross-field meaning, authorization, existence, state transitions, and other semantic checks.
- Interpret recognizable mistakes before rejecting unknown or inapplicable fields. A typo or stale field is not automatically a failed call: repair it when owner-specific evidence establishes the same intended effect. A unique close spelling or successful schema validation alone is insufficient. If meaning remains ambiguous or an instruction cannot be honored, return an actionable error; do not silently discard a meaningful part of the request.
- Apply the Agent error tolerance contract through shared normalization for representation changes and the Tool's own boundary for known aliases, call syntax, and domain-specific repairs. Preserve valid payload keys and exact target ids; unknown fields may express unsupported effects. For example, a boolean field accepts both `true` and `"true"`. This example does not limit tolerance to type conversion. Keep the model-facing schema canonical and do not disable normalization merely to enforce exact JSON types.
- Handlers must safely reject or normalize malformed arguments regardless of what the model-facing schema permits, and must apply defaults explicitly: an omitted argument must behave correctly even if the Provider ignores every schema annotation.
- Authorization, path safety, input sanitization, resource limits, and destructive-action checks belong to runtime code and must never depend on Model compliance.
- Runtime errors should identify the invalid argument and the valid correction without exposing implementation details.

## Multi-Type Parameters

- Prefer one simple type.
- Use `oneOf` only when one individual parameter genuinely accepts multiple representations and changing that public input would remove useful capability; `read.offset` is the reference case. A union never encodes actions, modes, targeting variants, optionality, or cursor continuation.
- Never use a union to encode Action Tool branches or optionality.
- Keep union branches free of duplicated descriptions and constraints.

## Provider Rendering

- Canonical Tool definitions remain Provider-neutral.
- Provider adapters may change only the transport wrapper required by the Provider, such as OpenAI Chat `function`, OpenAI Responses `name` plus `parameters`, or Anthropic `input_schema`.
- Provider rendering must never enable strict Tool calling. That is a deliberate decision - strict mode repeatedly caused problems in practice - so design the natural canonical contract instead: never force optional fields to be required-and-null, remove schema features, or otherwise reshape a Tool for a Provider's strict-schema subset.
- Provider rendering must not add defaults, required fields, nullable types, closed-object keywords, or other semantics absent from the canonical Tool definition.

## Results, Errors, and Display

- Keep the stable vBot Tool Result envelope. Define a success-data schema and return stable target identifiers, state, or action outcome when they help the next call.
- For batched independent operations, keep results in input order with stable indices. Mixed outcomes may use a successful envelope with an explicit `partial` data status when completed operations must remain applied; zero successful operations use a failure envelope.
- Expected failures use precise codes and actionable messages. Set retry metadata only when it changes what the Agent should do.
- Do not add public arguments solely to improve UI labels or conceal sensitive values. Use `ToolDisplay.summary_builder` and `hidden_argument_keys`.
- A result should not expose internal filesystem paths, provenance, or implementation state unless the Agent needs that value for the next in-scope operation.

## Writing the Texts

The model-facing texts decide whether a Tool gets chosen and called correctly; the handler behind them is secondary. Three surfaces share one craft: the description selects the Tool, the parameter descriptions steer the call, the result and error texts steer the loop.

- Write for a fresh Agent with no project context: self-contained, only concepts the Agent can observe or act on. Explain the available behavior and the next valid action, and never name a hidden implementation category merely to explain an exclusion.
- All three surfaces are runtime Agent-facing text. Follow `AGENTS.md` -> Communication with the user -> Present Agent-facing text changes for review presentation.
- Place guidance where it supports the decision: the definition must enable a correct first call, including prerequisites and defaults that affect its result. Errors explain the actual failure and recovery; they do not replace instructions needed to avoid predictable mistakes. Results explain observed truncation and supply usable continuation arguments.
- Remove irrelevant detail and repetition that adds no understanding. Keep the connection between operation, target, and payload clear even when it requires a brief reminder across surfaces. A minimal parameter label is insufficient when it leaves that relationship unexplained.
- Keep in the pre-call texts what only they can teach: capabilities the Agent needs before the first call (accepted file types, output row shape, sort order, omit-defaults), deviations from standard semantics (case-insensitivity, exclusion syntax), behavior invisible in results, silent-hang warnings where bad input blocks forever without an error, and deliberate steering sentences planted to guide Agent behavior.
- Structure a parameter description role-first: lead with the parameter's role, then accepted values, then the omit-rule as its own short sentence.
- The result steers the loop; the System Prompt only orients. Name the observed state and valid next actions together - a bare `status: "running"` invites polling. Explain alternatives to a prohibited action. Distinguish an exhausted search from a partial page, and a successful invocation from a verified task outcome.
- Error wording names the lifecycle state, never a wrong cause: "already delivered" informs; "not owned" reads as a permission problem and invites retries.
- Prefer the shortest plain sentence naming the subject, the event, and the recipient; name concrete Agent actions, not abstract concepts. The glossary does not reach the Model - project context loads only for Agents working in that project, so do not rely on glossary terms in model-facing texts.

## Retired Shapes

The shared `operation_envelope_schema`, `extract_tool_operation`, `action_schema`, and `discriminated_union_schema` compatibility helpers were removed after the final nested Tool migrated - the latter two built the forbidden oneOf closed-branch action shapes. Keep new definitions in the canonical flat form rather than reintroducing these helpers. Runtime handling of a recognizable older shape still follows the Agent error tolerance contract; its age alone is not a reason to reject it. Preserve any required rendering of historical persisted calls in the WebUI.

## Change and Verification Discipline

- Start an audit from actual delivered definitions, arguments, results, and the Agent's following decisions. Deduplicate persisted copies of calls. Investigate successful but empty, noisy, or truncated results as well as explicit errors; verify suspected false positives and false negatives against independent evidence. Separate proven defects from uncertain task outcomes or historical file state.
- Change exactly one Tool at a time. Shared Tool infrastructure may change with it only when that Tool requires the change, and every other Tool's verified behavior stays intact.
- Keep the repository releaseable after every Tool change.
- Before changing a Tool's public contract, inventory every accepted shape, default, permission rule, and persisted or UI consumer.
- After the change, recheck Provider rendering and the non-strict invariant, schema fingerprints, Tool descriptions, `ToolDisplay`, prompts, E2E fake-provider calls, and any generated Tool catalogs, and update the owning Tool map plus any documentation that teaches the call shape.
- Before moving to the next Tool, run its focused dispatch tests and live black-box task evaluations with its production definitions and prompts. A live installation round-trip is not required. The three evidence layers below are separate; transport conformance never substitutes for usability.
- For Luna probes, select `--provider openai --connection openai:subscription` explicitly. Do not run Luna through OpenCode Go or inherit the probe script's default Provider.
- Deterministic production-dispatch tests cover every action/mode, consequential default, explicit value, recoverable representation, and ambiguous or unsupported request. Pair each repair with nearby different and conflicting inputs. Assert independently specified receiving targets, payloads, scope and effects. Do not ask a Model to reproduce malformed JSON as a substitute for these tests.
- Provider conformance probes may prescribe arguments to isolate serialization, streaming, or schema transport. Label them as transport/conformance evidence. They cannot establish Tool choice, first-use understanding, or end-to-end task success.
- Black-box Model evaluations supply only a natural user goal, production definitions/prompts, realistic state and the competing Tools normally available. Keep expected Tool names, actions, argument shapes, grading criteria and the suspected fix outside Model context. Never force a Tool, demand one call, teach the answer in a test-only reminder, or hide Bash to make search selection pass. Exercise common tasks, defaults, recovery, continuation and cases where another Tool is appropriate. Use genuine results through completion and judge the requested outcome, including final claims; equivalent valid call sequences are allowed.
- Repeat fresh trials with fixed settings, compare unchanged cases before/after when possible, and keep task variants out of tuning. Preserve all attempts and diagnostic responses, including no-call replies, timeouts, wrong selections and retries. Report first-attempt success separately from recovery and final outcome, with denominators; reruns or changed thinking effort never erase failures. Fixture limitations and unresolved failures remain explicit. `--scenario tool_first_use` in `scripts/probe_provider_tool_call.py` owns search/delegation task evaluations; the older exact-call scenarios remain conformance probes.
- Commit each fully verified Tool as its own cohesive releaseable change before editing the next Tool.
- Quality gates: a scoped non-mutating pass (`python scripts/quality.py --check <paths>`) while working and before any intermediate commit; the full gate (`python scripts/quality.py`) once, before the final commit that closes the task. Tool work adds no separate gate schedule.

## Token Cost Comparisons

Always include before/after token counts in Tool audits and changes, including replacements of older Tools. Measure the full model-facing definition (name, description, and parameters), using the same tokenizer and serialization on both sides. Use `core/utils/tokens.py` for local estimates; state the encoding, compared revisions, and whether wrappers are included. A definition cost applies to each Model request containing it, subject to Provider framing and caching; it is not a separately billed Tool execution.

Where evidence exists, also measure argument and result tokens, call counts, pagination, and recovery calls for matched tasks. Count repeated history exposure separately from a payload counted once. Do not attribute whole-Run Provider usage to one Tool or compare unrelated live workloads as proof of savings. Report unavailable measurements explicitly. Present absolute counts and deltas alongside outcome quality; lower cost does not compensate for the wrong result.

## Review Checklist

Before accepting a Tool definition, verify all of the following:

- The first sentence states the action and observable result; the complete definition teaches a request that actually accomplishes it.
- Every required argument is in `required`, and every optional argument is absent from it.
- Every optional parameter tells the Model when to omit it; omission results appear only when they affect that decision.
- No `additionalProperties` keyword is present in the model-facing schema.
- No non-numeric JSON Schema `default` is present.
- Every Action Tool has one flat object and only `action` is unconditionally required unless another field is truly required by every action.
- Parameter descriptions contain no duplicated schema facts or runtime internals.
- Every parameter still has enough description for the Model to use it correctly.
- Cross-Tool guidance cannot point to an unavailable Tool.
- The handler independently validates conditional requirements and applies all defaults.
- The Provider wire explicitly remains non-strict where the Provider supports strict Tool calling.
- Before/after definition tokens and the measurement basis are reported; any claimed workflow savings have matched-task evidence.
- Focused dispatch tests and black-box task evaluations verify the intended outcome before another Tool is changed; conformance probes remain separate evidence.
