# Provider Compatibility Verification

Read this reference before adding, re-validating, or changing a Provider, Connection/Wire, endpoint, Model capability, Reasoning control, Reasoning Replay policy, media contract, Tool behavior, Usage normalization, or request limit. Its purpose is to prove what the selected Provider path actually delivers to a Model and then prove that vBot preserves that behavior end to end.

## Completion condition

A compatibility audit is complete only when the exact scoped Provider/Connection/Model combinations have evidence for every capability vBot claims, the raw wire and actual vBot Adapter/history path agree, durable facts are encoded in the correct source, regression tests protect the result, and unknowns remain explicitly unknown. A successful request, plausible answer, generated catalog, third-party implementation, or serializer unit test alone cannot satisfy this condition.

## 1. Freeze the scope before probing

Record the following before the first request:

- Provider id, exact Connection/Wire, authentication mode, base URL, endpoint, API version if applicable, and verification date.
- Exact wire Model ids included in the audit and exact ids intentionally excluded. Never generalize a result to an excluded Model or another Connection.
- Claimed capabilities to verify: Reasoning, Reasoning control, Reasoning Replay, streaming, Usage, Tools, media, structured output, sampling controls, Context, output limit, and error behavior as applicable.
- Material request variants required by the Provider contract, especially streaming vs non-streaming, Tools present vs absent, Tool continuation vs a later Run, and stateful vs stateless endpoints.
- Existing vBot profile values and their source: Provider config, generated Model catalog, override file, Adapter code, or default policy.

Keep upstream Model capabilities separate from gateway capabilities. A Model card describes the Model; the selected Provider and endpoint determine what vBot can actually send, receive, and replay.

## 2. Read the complete official contract

Read the official API reference for every relevant endpoint, plus every capability and compatibility page that affects the audit. Record page URLs and the read date in the evidence notes. Search-result snippets and one compatibility page are not enough when the Provider offers multiple wires.

Build a wire matrix before testing:

| Contract | Record exactly |
| --- | --- |
| Request endpoint | Method, path, streaming mode, statefulness, and auth |
| Reasoning request control | Exact field path, type, values, defaults, and unsupported-value behavior |
| Reasoning response carrier | Exact non-streaming field and streaming delta/event shape |
| Historical Reasoning carrier | Exact Assistant history field or opaque item/block shape accepted on later requests |
| Tools | Definition shape, Tool Call shape, Result correlation, identifier constraints, parallel behavior, and whether Tools change Reasoning rules |
| Usage | Input, output, Reasoning, cache, and total fields; which event carries final streaming Usage |
| Limits | Context source, output limit, request validation, and whether limits vary by Model or endpoint |
| Media and structured output | Exact supported MIME types and schema/request forms |

Never infer that the Reasoning request control, response carrier, and historical carrier share a name. For example, a Provider can accept `reasoning_effort`, emit `reasoning`, and require a different Assistant history field or opaque block. Treat each direction as an independent contract.

## 3. Trace the real vBot path before trusting a probe

Follow one canonical request from Session history through Chat shaping, Reasoning scope filtering, the selected Adapter, request serialization, and the final HTTP payload; then follow the response back through streaming/non-streaming normalization and persistence. Confirm the effective precedence of Provider defaults, Model overrides, Adapter selectors, and the system Reasoning Replay default.

Capture sanitized payloads at the final Adapter boundary when possible. Verify that every supposedly active probe option actually changes the outgoing payload: endpoint, Reasoning effort, Tool definitions, Tool choice, history carrier, streaming flag, and any Provider-specific option. A helper that parses an option but never forwards it can create a convincing false result.

Before reusing an existing probe script, inspect its request construction and compare one emitted payload with an independently constructed raw request. Treat scripts as test instruments that require calibration, not as authorities.

## 4. Establish a raw-wire baseline

For each in-scope endpoint and Model, first send the smallest valid request and record the status, response headers needed for diagnosis, finish state, content carrier, Reasoning carrier, Tool Call carrier, and Usage. Repeat in streaming mode when vBot claims streaming support and confirm the event framing plus final Usage behavior.

Then exercise documented Reasoning controls independently:

1. Omit the control to observe the Provider default.
2. Send the documented off value and confirm whether Reasoning actually stops.
3. Send every level or budget boundary vBot intends to expose, including Provider rejection or silent coercion.
4. Confirm that disabling Reasoning does not accidentally disable Tool Calls or another claimed feature.

If the Provider accepts a control but produces unchanged Reasoning, record it as ignored or unresolved; do not advertise a false control. If Reasoning requires an explicit enable flag, omitting that flag invalidates a negative replay result because there may be no Reasoning state to replay.

## 5. Prove Reasoning Replay with controlled input accounting

Use actual Provider-emitted Reasoning whenever the wire permits it. For each scope and materially different request shape, send three otherwise identical follow-up requests:

- A - no historical Reasoning carrier.
- B - the same history plus the candidate Reasoning carrier.
- C - the same added text placed in ordinary visible history as an accounting control.

Record Provider-reported input tokens for A, B, and C. The visible control proves that the added text is large enough to affect the Provider's accounting and calibrates the expected token delta. Keep System Prompt, user content, Tool definitions, Tool choice, sampling values, and all other fields identical. Account for reported cache-read/cache-write subsets when the Provider exposes them.

Run both scope experiments:

- Current Run: capture a real Assistant Reasoning plus Tool Call, then send the correlated Tool Result and continuation in the same Run.
- Full Session: place the completed Assistant turn in earlier Session history and send a later Run. Repeat with and without Tools whenever the official contract distinguishes those shapes.

Interpret the comparison conservatively:

- B increases input tokens by approximately the carrier's visible-control cost -> the Provider transports or renders that historical state for this shape.
- B adds zero while C adds the expected tokens -> the gateway strips or ignores the candidate carrier for this shape.
- Missing, impossible, inconsistent, or highly unstable Provider input counts -> the transport result is unresolved; do not replace the measurement with behavioral recall.

Behavioral tests using a unique secret or instruction are useful secondary diagnostics, but a correct answer can come from visible context, cache, template behavior, or chance. An incorrect answer can come from sampling even when the field arrived. HTTP 2xx proves only that the gateway accepted the request envelope.

Classify the effective Reasoning Replay policy from the complete request-shape matrix:

- `full_history` when a later Run consumes historical Reasoning in any supported shape that vBot must preserve. A Provider may still ignore that history in another documented shape; preserving it is required so the consuming shape works.
- `current_run` when Tool-loop or same-Run continuation consumes Reasoning but every relevant later-Run shape demonstrably does not.
- `none` when neither scope consumes the readable or opaque state vBot can replay on that wire.
- Unresolved when Usage cannot establish transport or a required request shape remains untested. Do not pin a narrower policy from an unresolved result.

Determine replay fidelity separately from scope. Test whether the wire requires readable text, exact opaque blocks/items/signatures, or a documented preference between them. Never send both classes just to maximize the chance that one works; duplicate Reasoning can waste Context or invalidate Provider state.

## 6. Repeat through the exact vBot Adapter and history path

Raw-wire success proves only the Provider contract. Repeat the decisive A/B/C cases through the actual Connection, Adapter, effective Model profile, Chat history shaping, and Session persistence. Inspect the final sanitized payload to confirm whether the carrier is present and under the correct field.

Verify at least:

- The effective Reasoning control rendered by the Adapter matches the profiled control and level ladder.
- The effective replay policy selects the intended Session turns and the intended fidelity.
- Route, Model, Connection, Account, interruption, and Compaction boundaries behave according to shared Request Policy.
- Tool Call ids and Results remain correlated and historical Reasoning survives exactly where the Provider requires it.
- Streaming and non-streaming normalization produce the same canonical capability claims.

Adapter and serializer unit tests prove deterministic local translation. They do not replace live Provider input-token evidence; live raw-wire evidence does not replace the Adapter test. Both layers are required.

## 7. Verify the rest of the claimed capability surface

Exercise only capabilities vBot advertises or intends to advertise, but verify each one end to end:

- Tools: single and parallel Calls, argument encoding, malformed arguments, missing/late ids, Tool Results, multi-step loops, finish state, and Reasoning plus Tools.
- Streaming: framing, content/Reasoning/Tool deltas, terminal outcome, final Usage, heartbeats, malformed/in-band errors, and a stream ending without a terminal frame.
- Usage: nonzero input for nonempty requests, output/Reasoning/cache subsets, total consistency without inventing missing components, and short-request anomalies.
- Media: exact MIME types on the selected wire, multi-item ordering, Tool Result media, and rejection/degradation for unsupported media.
- Structured output: exact schema field, Tool coexistence, streaming behavior, and Provider strictness constraints.
- Sampling: each exposed parameter changes or is accepted according to the official contract; silent acceptance is not proof that a parameter reaches the Model.
- Context: catalog/API value, per-Model variance, effective local or gateway cap, and how vBot budgets the real serialized request.
- Output: send an intentionally oversized limit to obtain an exact Provider rejection ceiling when available. Acceptance of an absurd value proves only lack of front-door validation, not that the Model can generate that many tokens; retain a documented limit or unknown rather than inflating the profile.
- Errors: invalid auth, unsupported Model/parameter, rate limit, timeout, transient gateway failure, content filtering, and retry classification without exposing secrets.

## 8. Evaluate alternate endpoints independently

When a Provider offers native, Chat Completions, Responses, Messages, or other compatible endpoints, treat each as a separate wire. Run the relevant contract and replay matrix on each serious candidate. Do not transfer a positive or negative result between endpoints.

Choose the endpoint that best preserves vBot's required end-to-end contract: Reasoning control and continuity, Tools, streaming Usage, media, statefulness, output/context behavior, and reliable errors. Familiarity or superficial compatibility is not a deciding factor. Record why the selected endpoint wins and which unverified or weaker capabilities prevent using the alternatives.

For stateful endpoints, distinguish server-held continuation identifiers from client-replayed Session history. For stateless endpoints, verify the exact historical item/block format instead of assuming a previous-response field exists.

## 9. Use competitor implementations only as leads

Inspect current source at a pinned commit when OpenCode, Hermes Agent, OpenClaw, or another client appears to support the same Provider/Model. Trace its final serialized request and history filter, not only its capability table or comments.

A competitor implementation can reveal a field name, request-shape branch, or missing experiment. It cannot prove that the Provider consumes the field: it may use family heuristics, stale metadata, incomplete history conversion, or tests that assert only JSON shape. Re-run every discovered claim against the live Provider and the actual vBot path.

## 10. Record evidence before changing profiles

Keep a compact row for every decisive experiment:

| Date | Provider/Connection | Model | Endpoint | Shape | Control | Response carrier | History carrier | A/B/C input tokens | Result | Official source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| YYYY-MM-DD | provider:connection | exact-id | method/path | stream, Tools, scope | exact field/value | exact field/event | exact field/item | A / B / C | measured conclusion | URL + read date |

Retain sanitized request/response samples or reproducible probe output when practical. Never retain credentials, raw auth headers, Account ids, user content, or secret-bearing production traces.

Resolve contradictions explicitly. Prefer the evidence appropriate to the claim:

- Official docs define the intended wire contract and required request shapes.
- Controlled live Provider input accounting proves whether a historical field is transported/rendered on that endpoint and shape.
- The final Adapter payload plus local tests prove what vBot sends and normalizes.
- Live end-to-end vBot results prove the integration of those layers.
- Catalog aggregators, Model-family assumptions, benchmarks, and competitor code are discovery aids only.

## 11. Encode and protect the result

Put each fact in its durable owner:

- Adapter code for wire serialization, normalization, routing, fidelity, and Provider mechanics.
- Bundled Provider config for stable Connection-wide defaults and endpoint/auth facts.
- Provider/Model override files for verified durable facts omitted or misstated by generated feeds, including per-Model Reasoning Replay and gateway limits.
- Generated Model catalogs only through their refresh pipeline; never hand-edit them.
- Focused unit tests for exact request fields, response carriers, policy precedence, normalization, and limits.
- Provider-specific reference for dated Provider/Model observations, endpoint decisions, exceptions, and remaining unknowns.
- This reference for the generic verification method; never copy the whole workflow into one Provider's reference.

Run the scoped tests while iterating and the required full quality gate before committing code. In the final audit report, list included and excluded Models, exact policy/control/limit changes, unresolved cases, verification date, tests/gates, updated domain references, and any Provider facts that remain intentionally unknown.

## Existing local probes

- `scripts/probe_reasoning_replay_exact.py` exercises the effective vBot Adapter/history path for supported Providers and policies; inspect its payload construction before extending it to another wire.
- `scripts/probe_reasoning_replay_tokens.py`, `scripts/probe_reasoning_replay_alt_wires.py`, and `scripts/probe_reasoning_replay_native.py` contain Provider-specific raw-wire experiments. Reuse their controlled-comparison pattern, not their Provider assumptions.
- `scripts/probe_reasoning_replay_behavior.py` is a behavioral diagnostic and cannot establish transport by itself.

When a probe is wrong, fix the instrument and add a regression test or explicit output that makes the missing option/request shape visible. Do not preserve a prior conclusion merely because later agents trusted the old script or domain note.
