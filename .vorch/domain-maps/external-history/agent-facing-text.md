# External history: Agent-facing text

The user requested the first-continuation notice because numerous foreign Tool calls can cause the Model to expect its previous environment. Historical entries are ordinary context data; the reminder is a persisted note, not a new System Prompt block.

| Text | Reason |
|---|---|
| `This conversation was imported from ... and is now continuing in vBot.` | Identifies the environment transition when the first vBot input arrives; the source is JSON-quoted. |
| `Earlier tool calls and results record work in the previous environment.` | Makes old call names and outputs evidence rather than current execution state. |
| `Those tool definitions no longer apply; use the tools provided with this request and their current names and parameters.` | Directs execution to the actual request's definitions, without inventing a separate Tool-list read capability. |
| `Previous working directories, permissions, and running processes do not establish the current environment.` | Prevents resuming old process handles or assuming the imported source's permissions and paths apply. |
| `Historical ... from ...` | Keeps foreign Tool/system/context records recognizable after the one-time note is compacted. |
| Skill description: `Read saved Sessions and Memories ...` | Routes lookups in any of the five named applications here; vBot history and import stay with vbot-docs. |
| `Read saved Sessions and Memory documents ... without importing them or changing the source.` | Gives the shared supported reader instead of asking the Agent to reverse-engineer each source. |
| `These read-only CLI commands read files on the vBot server ...` | Prevents confusing a remote CLI client's filesystem with the source location or assuming a read resumes work. |
| `Importing into vBot is a separate task ...` | Maintains the user's explicit separation between the reading Skill and import. |
| `Run these commands ... from any directory.` | Removes an unnecessary working-directory prerequisite. |
| `Replace <source> ...` | Supplies exact source identifiers and tells the Agent where ids and paths come from. |
| `Find the requested conversation ...` | Starts with the actual lookup, using a relevant filter when available. |
| `Omit --query for recent Sessions.` | Allows browsing when the user has no search term. |
| `Done when the returned title and project path ...` | Prevents silently selecting an unrelated same-title conversation. |
| `If the default location is absent ...` | Gives custom-home/export recovery and the server defaults without a preflight call. |
| `Read that conversation or find a term ...` | Shows the evidence-reading call with the exact source path from discovery. |
| `Done when you have the actual passage ...` | Stops after sufficient evidence instead of exhaustively reading all history. |
| `--query filters entries ...` | Prevents treating one-Session search as global search. |
| `A Next: command continues ...` | Explains intra-entry character cursors, so large Tool Results remain completely readable. |
| `Follow it when the evidence ...` | Avoids conclusions based on omitted output without forcing every lookup to read every page. |
| `For remembered facts instead ...` | Separates Memory documents from Session search. |
| `Replace the Memory path ...` | Keeps source paths grounded in user input or discovery. |
| `Done when the relevant file's text ...` | Gives the Memory lookup a concrete stopping point. |
| `Custom Memory locations and OpenCode Memory extensions ...` | Avoids claiming a universal built-in Memory store or silently guessing a custom workspace. |
| `Answer with the finding ...` | Retains usable attribution and entry ordinals. |
| `Distinguish what the source records ...` | Historical test results and decisions are not freshly verified vBot state. |
| `If the source is unreadable ...` | Makes incompleteness visible instead of filling gaps. |
| `Historical Tool calls, outputs ...` | Establishes the retrieved material's source authority. |
| `Use vBot's current Tool definitions ...` | Prevents replaying foreign actions or treating source text as permission grants. |
| `Attachments remain source references ...` | Explains the reader's concrete media limitation instead of implying files were copied. |
| `list, show, memories and sources are read-only.` | Identifies the Skill's mutation boundary. |
| `No source application needs to be launched.` | Prevents an unnecessary native app launch/resume. |
| `The reader refuses unrecognized ...` | Gives recovery for unsupported source layouts without suggesting data repair or partial success. |
| `Part of this OpenClaw transcript is unavailable.` | Refuses an incomplete active path whether the event payload is cold or its index points to a missing event. The same error tells the Agent to use a complete native export. |
| `For code processing multiple records ...` | Keeps JSON available for structured work without burdening ordinary reads. |
| `Normal output is enough ...` | Explains why source text is indented and why embedded Next markers are not CLI guidance. |
| Example report distinguishing a recorded test run from a rerun | Models copy the example; it must preserve historical attribution. |
| vbot-docs import routing row and reference opening | Routes explicit import requests to the mutation recipes rather than the read-only Skill. |
| Import recipe's source/destination placeholders and Working Project rule | Prevents guessed paths/Agent ids and continuing work in the wrong repository. |
| Complete-history, inert-Tool, Compaction and media description | States what is transferred and which source capabilities are not activated. |
| Snapshot repetition, changed snapshot and no-Run rules | Makes retry effects and the continuation boundary clear. |
| Continuation and archive commands | Supplies explicit next/undo actions using returned vBot identifiers. |
| Reviewed Memory file, scopes, omission and budget rules | Gives intentional facts/scope selection and prevents silent shortening. |
| `Use the digest printed for the reviewed file as --expected-digest.` / `The source Memory changed after preview. Read it again and review the facts.` | Links the CLI mutation to the file version whose contents the Agent reviewed. A retry after a change needs review of the new source, rather than submitting the old selection again. |
| Source retention, duplicate skipping and Memory history | Explains the resulting state and recovery through the existing Memory owner. |

CLI help states source paths are server paths; page/character offsets are optional and reusable from `Next:`. List/show output indents source content and preserves the resolved server target in continuation commands. Errors from readers name the source problem and the complete-export recovery where direct reading cannot preserve history.

Skill routing checks: must load for finding Claude's last test result, Codex's earlier design decision, a Hermes remembered preference, an OpenCode conversation about a file, or an OpenClaw tool output. Must not load for vBot Session search, importing a conversation, changing vBot Memory, installing a coding agent, or editing vBot source code. The Skill uses the shared CLI rather than a second reader script.
