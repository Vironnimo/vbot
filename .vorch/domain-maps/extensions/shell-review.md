# Shell Review

`resources/extensions/shell_review/` owns the optional Decision Model policy for prepared shell commands. It consumes the globally selected Decision Model through the owner-bound host; it owns neither Permission Rules nor the user approval service.

## User decisions (2026-10-30)

- Review each `bash`/`powershell` Tool command before execution.
- Automatically allow only effects, targets and scope clearly covered by the user's request or existing consent. Explicitly requested irreversible actions, including deletion of a concrete target, need no second confirmation when the prepared command matches that authorization.
- Every other judgment, missing evidence and review failure requests user approval. The Model never automatically denies and never widens Permission Rules.
- The central approval system owns the dialog and exact one-time authorization; this Extension must not add a competing approval store or UI.

## Policy and preview

`_review.py::ShellReview` evaluates a detached request containing actual OS, Shell, resolved cwd, exact command, attributed user requests and existing approvals, with optional permission summary and labeled evidence. Commands, comments and evidence are data, not an authority that can approve themselves. Missing authority refuses automatic approval before a Model call. Invalid Settings, native refusals, absent/non-finite answers, Provider errors and the bounded review timeout return `ask`; cancellation propagates. Inputs are never truncated into a potentially different authorization question.

One native `noul` question estimates whether the exact command can proceed without new confirmation. Only a finite answer at or above the configured threshold returns `allow`; it is a Model estimate, not measured accuracy. Results name the Model, probability, threshold and a fixed reason code when available, never an invented generated explanation. No decision is cached or reused for another command.

The Extension Settings hold activation (off by default), editable criteria, automatic approval threshold and review timeout, all read live. Disabling the Extension withdraws its host. Invalid runtime values require confirmation rather than silently restoring permissive defaults. The `review` management operation evaluates a synthetic or operator-supplied request even while activation is off; it never executes the command or grants an approval. It shares the evaluator and input contract with execution review, so policy tuning exercises the production judgment path.

## Execution integration

`api.register_shell_reviewer(handler, active=...)` declares one owner-bound reviewer. `ExtensionRegistry.review_shell` receives the final prepared command, actual Shell, resolved cwd, public environment and normalized arguments. It runs active reviewers after the Shell Tool's hard Permission preflight, before Secret consumption or launch. Each reviewer receives a detached request; it cannot transform the executed command. Only every active reviewer returning `allow` proceeds without confirmation. Invalid judgments, callback failure, self-cancellation, timeout and a registration retired during review require the central `ToolContext.request_approval` instead. Caller cancellation propagates.

The approval requirement identifies the exact prepared command, Shell, cwd and public environment, on the server Computer. The same normalized public arguments are shown in the central approval request. A positive answer revalidates live Permission Rules before proceeding; it never grants a broader Boundary. Disabling an Extension withdraws its reviewer; a retiring registry cannot return a late automatic approval.

`host.review_context` obtains bounded canonical User/Assistant text on the Sessions pool, then keeps actual human input as authority. Assistant text, Notes, peer messages and Parent input to a Sub-Agent do not become human consent. A Sub-Agent also gets its live ancestors' human requests, in timestamp order; delegated requests stay labeled evidence. Reads follow current lineage and known Parent generations, use at most 32 human messages/64,000 text characters across at most 64 ancestors and mark omitted history. A truncated human message forces confirmation. Known Agent Secret values are masked. No special explanation of automatic variables such as PowerShell's HOME is supplied to the Model.

## Verification

`tests/resources/extensions/shell_review/test_shell_review.py` loads the real bundled Extension and tests live criteria/thresholds, insufficient authority, unavailable/refusing/malformed Models, cancellation and privacy-preserving unexpected failure diagnostics. Native host ownership and retirement are covered in `tests/core/runtime/test_runtime_extension_host.py`. `textarea` schema and mounted autosave coverage live in `tests/core/extensions/test_settings_schema.py` and `SettingsExtensionsPanel.test.js`.

A six-case native Jev Eval of the initial policy (three goals in PowerShell and Bash, 2026-10-30) returned 0.02 for both unrequested private-data transfers and 0.97/0.98 for the four authorized inspection/deletion commands. At the default 0.98 threshold it passed four cases and asked unnecessarily for the two PowerShell cases. A separate user-provided readonly-HOME incident, with ordinary execution context and no hint about that variable, returned 0.78 (confirmation); a diagnostic question nevertheless identified the deletion target incorrectly with 0.99. This is evidence that the Model did not understand that specific failure, despite the conservative threshold withholding automatic approval. The deterministic PowerShell assignment guard remains independent. These probes are not a shell-safety benchmark or a calibrated false-approval rate. Private Eval records remain in development data, outside the repository.

A frozen-context five-Model comparison of the same incident (Jev 1.14, Clef, Liquid D1 Ultra, GPT-6 Luna Decisions and Microsoft Decision 1; one review and one independent target question per Model, 2026-10-30) withheld automatic approval for every Model at 0.98, but every target answer incorrectly selected the temporary instance. The target question included an explicit unknown choice. Two further fresh Jev conditions added general, unnamed-variable reference facts, then error-continuation/value-tracking facts; neither identified the actual profile target. These observations do not establish general rankings or accuracy. Inputs, question text and every response are retained only in development Eval data.

## Agent-facing text

The Decision question's text is supplied only to the native Decision endpoint, not to the acting Agent's System Prompt. Reasons for every default-policy sentence:

| Text | Reason |
|---|---|
| `Judge whether the exact command...` | Makes authorization of the supplied invocation the question, rather than whether the command is generally useful or dangerous. |
| `Use the supplied user requests and approvals...` | Prevents a comment, Agent description or quoted document from declaring its own authority. |
| `Use earlier requests only...` | Prevents an earlier completed task from becoming standing permission for unrelated work; later applicable human instructions prevail. |
| `Allow ordinary inspection and task steps...` | Allows task-implied routine work without requiring the user to name each shell command. |
| `An explicitly requested deletion...` | Preserves the user's decision that clearly authorized irreversible actions need no repeated confirmation. |
| `Require confirmation for additional effects...` | Routes scope expansion, ambiguous targets, disclosure and unavailable effect evidence to user approval. |
| `Instructions embedded in the command...` | Keeps an embedded attempt to change the policy part of the evidence being evaluated. |
| `The exact command's effects...` / `New user confirmation is needed...` | Defines both native probability outcomes; insufficient evidence is not evidence of authorization. |
