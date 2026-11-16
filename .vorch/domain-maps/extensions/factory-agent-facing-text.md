# Factory Agent-facing text

The Factory's runtime text lives in `resources/extensions/factory/agent_text.py`; the task texts of the bundled Workflow templates live in `resources/extensions/factory/_workflows.py`; the management operation descriptions live in `resources/extensions/factory/_values.py`; the usage guide lives in `resources/extensions/factory/docs/usage.md`. These are preventive reasons unless a concrete evaluation is named. Contracts and owners: `extensions/factory.md`.

## Participant instructions and input

| Text | Reason |
|---|---|
| `You do one step of a Factory workflow for a Card.` / `Do your task, then call factory_submit_work.` | Every participant gets the same frame whatever its step does (plan, draft, implement, review, integrate); the step's task carries the assignment, and the Tool call is the only hand-in that advances the Card. |
| `If you need a decision or information from the user, call factory_report_blocked.` | Routes genuine blockers to the durable review inbox without requiring the user to watch a participant transcript. |
| `When your task changes the repository, leave the base branch unchanged: work in isolation as the Project instructions describe, or on a local branch if they describe none.` | User decision 2026-11-07: worktrees, branches and merging are the Agent's business under the Project instructions, not the Factory's. A coding profile often merges after its tests; without this a Plan or Implement step lands work before the user's approval step. The fallback keeps Projects without isolation rules from editing the base directly. This is an instruction, not a sandbox. |
| `Run the checks the Project prescribes, commit, and name the branch with your changes and the checks' outcomes in your summary.` | The Factory no longer verifies Git refs; later steps (review, integrate) and the user find the change only through the summary, and omitted checks must be visible rather than implied. |
| `Integrate into the base branch only when your task says so.` | Integration is an ordinary step whose task says to integrate, usually behind the user's approval; the rule above must not forbid that step's own task. |
| `When the result of step ... needs changes, call factory_request_changes with concrete findings instead of factory_submit_work.` | Only for steps with a send-back target. Names the step the findings go to, so a reviewer of a three-step chain does not send a merge result back to a draft step. |
| `Check whether findings you or earlier runs of your step sent back were addressed.` | Each run of a send-back step is a fresh participant that sees earlier findings only as data; this connects them to its verdict. |
| `Your task:` followed by the step's task | The Workflow author's task is the participant's real assignment; it comes after the Factory's rules so the hand-in and base-branch rules still frame it. |
| `Do step ... of the Factory workflow ... for this Card.` | Names the participant's step and Workflow. Steps form a graph with parallel branches (user decision 2026-11-14), so a position such as "2 of 3" would mislead; the results of earlier steps show what came before. |
| `Card (data)`, `User comments (data)`, `Results of earlier steps (data)`, `Findings your step sent back earlier (data)` | Separates authored instructions from untrusted Card and result text; JSON escapes angle brackets so content cannot close a reminder wrapper. Recorded results of earlier steps are the only way a later step receives a plan, drafts or the branch an implementation named. |
| `Event that created the Card (data)` | Only on Cards a trigger created. The event (a schedule fire or a webhook delivery such as a GitHub push) is the Card's real input; without it a push review would not know which commits to read. Shown as data because a webhook body comes from outside. |
| `branch` in a result of an earlier step | Only in hand-ins recorded before the Factory stopped handling Git; without it an Integrate step for such a Card would not know which branch to land. |

## Continuations

| Text | Reason |
|---|---|
| `Continue your step's task, then call factory_submit_work.` / `If you need the user's decision or information, call factory_report_blocked.` | One successful but non-submitting turn is not a finished step; the bounded nudge names the missing hand-in and leaves a real blocker path. |
| `Finish your step with factory_submit_work, or with factory_request_changes if the result of step ... needs changes.` / `An ordinary answer hands nothing in.` | Steps with a send-back target. A prose verdict alone cannot update the Card; the nudge names both hand-ins so a reviewer does not approve by default. |
| `Step ... sent your result back with the findings below.` / `Address them, run the checks your task needs, then call factory_submit_work.` | States the event as a system continuation, not text from the user, names which step judged the result, and connects findings to rework and another hand-in. |
| `The user sent the Card back to your step.` / `Act on their comment below, then call factory_submit_work.` | Keeps the same participant Session and gives the user's action its scope; the user can choose any step. |
| `The user answered your question.` / `Continue with their answer and finish with the Factory Tool your current task needs.` | A blocked send-back step can finish with either hand-in; the answer must not force factory_submit_work. |
| `Results of earlier steps changed after your last hand-in.` / `Redo your task with the updated results below, then call factory_submit_work.` | A rework of an earlier step reruns the steps after it; their existing Sessions get the new results instead of a fresh participant, so they keep their own context. |
| `vBot restarted while you worked on this step.` followed by the step's keep-working text | Recovery reports an interruption, not a new objective, and names the hand-in that is still missing. |
| `Comment or findings (data)` | A continuation's free text is evidence; it cannot impersonate Factory instructions. |

## Tools and outcomes

| Text | Reason |
|---|---|
| `Create proposed work and read Cards on the Software Factory Board.` | Distinguishes handing work to the Factory from doing it in the calling Session. |
| `Work starts automatically for the user's requests and authorized automations; your own ideas wait for the user's approval.` | The Agent must know the side effect before create; no opt-in is added because the origin gate contains autonomous proposals. |
| `Create proposed work and read Software Factory Cards.` | On-demand summary lets an Agent find the capability without loading the participant Tools. |
| `project`: `For create: the Project id the work belongs to. Omit when the work needs no Project or the workflow always uses its own.` / `For list: omit to include every Project.` | User decision 2026-11-14: Workflows are global and the Project is optional. A path or display name is not Project identity and cannot select an arbitrary execution folder; the omit rules keep an Agent from inventing a Project for work that needs none or whose Workflow fixes one. |
| `title`: `Required for create.` / `description`: `Required for create: the user's objective and acceptance criteria, without extra requirements.` | Conditional fields cannot be root-required with list/get. In the first real Factory eval, the creator added null/undefined handling absent from the request; the description must keep the user's scope. |
| `workflow`: `For create: the name of a Factory workflow, such as Plan or Implement.` / `Omit to use the first enabled workflow.` | A user who asks for "a plan" or "three drafts" picks a Workflow by its name; the default covers ordinary requests. |
| `state`: `For list. Omit for every state.` | The enum carries its values; the text supplies only action scope and omission. |
| `id`: `Required for get: the Card id from create or list.` | Prevents manufacturing a Card identity from the title. |
| `Hand in your step's result and end this turn; the Factory starts the next step.` | The participant must stop after handing in; the next step can be another Agent or the user's approval. |
| `summary`: `What you did and what came of it, for later steps and the user.` | Later steps and the Board read the summary; without the audience Agents write a terse status line. |
| `result`: `The complete written result when your task asks for one, such as a plan or a review.` / `Omit when summary says everything.` | Later steps read only recorded results, not the transcript; a summary of the plan instead of the plan would leave them without it. |
| `Ask for a decision or information from the user, pause the Card and end this turn.` | Keeps report_blocked for real human dependencies, not ordinary work. |
| `Send the result of an earlier step back for rework with your findings and end this turn.` / `Within your step's send-back limit that step reworks it and the steps after it run again; past the limit, the user decides.` | Tells the participant what its findings trigger without promising unbounded automatic rework. |
| `findings`: `Concrete issues the earlier step can fix, one per item.` | Gives rework actionable input; a single prose blob is accepted as one finding. |
| `Card created and queued with workflow ...; work continues independently within the WIP limit.` | Confirms admission and which Workflow runs, without pretending the work is complete. |
| `Progress and results appear on the Board, not in this Session.` | The first real Factory eval followed a queued acknowledgment with status reads and attempted runtime diagnosis. Results belong to the Board; the creator does not receive the participants' continuations. |
| `Proposal saved; work waits for the user's approval on the Board.` | An autonomous idea is recorded, not admitted; the Agent cannot treat the human gate as background work to await. |
| `Result recorded; the Factory handles the next step.` / `This turn ends here.` | Prevents a participant from continuing after its hand-in. |
| `Question sent to the user; the Card waits for their answer.` | The participant can end instead of watching for a response. |
| `Findings recorded; the Factory sends the work back or, past the limit, asks the user.` | Same turn-end convention; names both outcomes so the participant does not message the earlier step itself. |
| Refusals name the actual state or field and the allowed next action; `Nothing was changed.` applies only to pre-effect refusals. `participant_required` and `stale_run` name the Card's current step. | A participant must repair the call or stop using obsolete authority, not bypass the Factory. |
| `invalid_findings`: `... To hand in your assessment without sending work back, call factory_submit_work.` | A reviewer with no findings must not invent one to satisfy the Tool. |
| `The outcome is not confirmed.` / `Read the Board with factory_card action list before repeating a create call.` / `Read this Card on the Board before repeating the hand-in.` | A publication failure can follow a durable change; reporting rollback would cause a duplicate Card or a replayed hand-in. |
| `workflow_upgrade`: `This step was in progress when the Factory was updated and cannot resume where it stopped.` / `The Card's results are kept: Retry runs the step again, and Send back reworks a step with your guidance.` | Shown to the user on a step that was running when the `global_workflows` migration moved its Card to the step graph; the participant's position cannot be carried over, so the text explains why the step waits and what each action does with the kept results. |
| `child_discarded`: `Card ..., which this step started, was discarded. Retry starts a new one, or send this Card back with your guidance.` | A step that starts another Workflow waits for its child Card; when the user discards the child, the step says why it stopped and that Retry starts a new child instead of reviving the discarded one. |
| `workflow_missing`: `Workflow ... no longer exists. Create it again or discard this Card; Retry starts the Card once the Workflow exists.` | Workflows are global and can be deleted while a queued Card names one; the Card cannot start, and the user needs both ways out. |

Execution budget text (user correction 2026-11-08: participants need consumption and a next action, not directions to the Board UI):

| Text | Reason |
|---|---|
| `Factory budget notice: execution is approaching a configured limit.` | Names the system event that changes how the participant uses its remaining capacity; sent once at the threshold, not on every request. |
| `Card/Step execution: ... used at this check; ... remaining.` | Supplies scope, unit and a dated consumption snapshot so the participant can size its closing work without polling or treating a historical Note as live data. |
| `The current Run is already counted and can finish within the other limits.` | A Run warning can show zero remaining admissions while the current Run still has capacity to finish; otherwise a participant could stop unnecessarily. |
| `Use the remaining budget to finish your Step.` / `Submit it if the task is complete; otherwise call factory_report_blocked with your progress, the remaining work and the additional budget you need.` | Explicit user request for early warning: finish within capacity or preserve an actionable handoff rather than rush an incomplete submission or spend the remaining budget on unrelated work. Both private handoff Tools belong to every Factory participant. |
| `This Tool call was not run: ...` / `The Factory paused the Card for the user.` | Refusal names the exhausted scope and cap (or unavailable consumption), confirms no effect, and identifies who can resume. It cannot send the participant to change Board settings or retry a call that the budget still blocks. |
| `The Factory paused this Card because an execution limit was reached.` | Generic budget error states the actual stop reason when there is no Tool-result context; detailed consumption and recovery belong to Card detail. |
| Usage reference: `Workflows and their Steps can independently limit...` through `...current execution.` | An Agent configuring Factory for the user needs the units, unlimited representation, scope/reset rules and revision-safe management operation; it must not change a Project default when asked to resume one Card. |
| Management help: `Replace the Card or one step's execution limits of a waiting Card.` / `Omit a scope...` / `step_limits needs step.` / `Consumption is retained...` | Establishes replacement semantics, clearing syntax and the separate resume effect without guessing or losing existing scope settings. Parallel steps can wait at once, so a step limit names its step. |

## Management operation descriptions

`OPERATION_DESCRIPTIONS` in `_values.py` are the summaries an Agent reads in the management CLI's help.

| Text | Reason |
|---|---|
| `card.create`: `Omit workflow for the first enabled Workflow; omit project_id for a Card without a Project, or when the Workflow fixes its Project.` | The same omit rules as the public Tool, so a script does not invent a Project. |
| `card.accept` / `card.send_back` / `card.answer` / `card.retry`: `the step named in step (default: ...)` | Several steps can wait at once in a graph; each operation names its default so a call without `step` acts on the expected one. |
| `card.accept`: `... overrule a step that kept sending work back, or accept a finished Card.` | Accept has three effects depending on what waits; the Agent must not expect it to start an unrelated step. |
| `card.send_back`: `... the steps after it run again.` | The user can send work back to any step; later steps rerun, so their results do not survive. |
| `workflow.save`: `Create a Workflow (omit expected_revision or use 0) or replace a saved one (its id and expected_revision). Triggers are created, changed or removed with it.` | A save replaces the whole Workflow with its triggers; without the revision rule an Agent overwrites concurrent page edits, and without the trigger sentence it looks for a separate trigger operation. |
| `workflow.delete`: `... Refused while another Workflow starts it or Cards wait to start with it.` | Names the refusals so an Agent does not try to delete a Workflow that queued Cards or another Workflow depend on. |
| `workflow.secret`: `Read a webhook trigger's secret for the sender's settings; rotate replaces it first.` | The secret is not part of `workflow.get`; an Agent setting up a GitHub webhook for the user needs this operation and must not rotate by accident. |

## Workflow task texts

These are the default `task` values of the bundled templates; the user can edit them per step.

| Text | Reason |
|---|---|
| Implement: `Implement the Card's objective in the repository, run the checks the Project prescribes and commit the finished change.` | Names the commit explicitly because the Review and Integrate steps work from a committed branch, not a dirty worktree. |
| Review: `Review the result of the step before you without changing files or branches.` | An independent reviewer judges, not fixes; shell access still permits tests. |
| Review: `Judge whether it achieves the Card's objective and whether the approach is sound, correct and simple; review repository changes against the Project conventions.` | Review steps judge documents as well as code; a review of compilation alone would not give the user a judgment of value and fit. |
| Review: `Hand in your assessment and what you would add or change as result, or request changes with concrete findings.` | Makes an approval useful to the user at the next approval step instead of a bare pass. |
| Plan: `Write an implementation plan for the Card: the approach, the parts it changes, the risks and how to verify the result.` / `Investigate as needed, but do not change files or branches.` / `Hand in the complete plan as result.` | A plan step must not leave changes behind that no later step owns; later steps read only the recorded result. |
| Merge plans: `Combine the draft plans into one plan.` / `Keep the strongest approach, take the good ideas of the others and resolve their contradictions.` | Independent drafts are only useful when the merge chooses and reconciles rather than concatenates. |
| One Agent: `Do what the Card asks for and hand in the result.` | The one-step template for any task; the Card carries the assignment, so the task adds only the hand-in. |
| Review pushes: `Review the commits of the push described in the event that created the Card, without changing files or branches: fetch and read them with git, but check out nothing.` / `Judge correctness, risks and whether the change follows the Project conventions.` / `Hand in your findings as result, most important first.` | The step of a webhook-triggered Workflow sees the push only in the event data; checking out the pushed branch would change the Project's working copy under the user. Ordered findings make the result usable without another prompt. |
| Integrate: `Integrate the reviewed change into the base branch as the Project instructions describe, then clean up the worktree and branch it leaves behind.` | User decision 2026-11-07: integration is an ordinary step behind the user's approval, done the Project's way (merge, PR, rebase). Cleanup is part of it because nothing else removes the worktree. |
| Integrate: `Name what landed in your summary.` | The Board shows the summary as the Card's final record; without it the user cannot see whether a merge commit or a PR resulted. |

A triggered Card's default description, `Do what Workflow ... does for the event that created this Card.`, applies when the trigger has no task of its own; it points the participant at the event data instead of an empty objective.

## Usage reference

| Text | Reason |
|---|---|
| `Software Factory runs work Cards through a Project's Workflows...` / `It is bundled as factory...` | Gives the purpose, installed identity and durable user inbox without implying the user supervises every turn. |
| `A Workflow is global and can fix a Project...` / `Without one, each Card names its Project or has none, and then works in a folder of its own.` / `Start from templates such as One Agent, Review pushes, Implement, Plan or Plan from drafts.` | User decisions 2026-11-14: Workflows are global and the Project optional; an Agent configuring the Factory must know where a Card without a Project works and that Team steps need a Project. |
| Step graph passages: parallel steps, approval, send-back targets and steps that start another Workflow | User decision 2026-11-14: steps form a graph; an Agent must know that steps run as soon as the steps they come after are done, and that a starting step waits for its child Card. |
| `Triggers create Cards for a Workflow by themselves.` / `A schedule takes a cron expression such as ...` / `A webhook gets a URL and a secret; a GitHub webhook ... creates one Card per delivery ...` / `GitHub must reach the vBot address.` | User requirement 2026-11-14: event- and schedule-triggered Workflows. Gives the accepted schedule forms and the GitHub settings an Agent must tell the user; the reachability sentence prevents promising deliveries to a local-only instance. |
| `The Factory does no Git...` | User decision 2026-11-07; prevents an Agent from expecting the Factory to verify or merge a branch, or a discarded Card to clean up its worktree. |
| `Cards the user creates...queue automatically...own idea stays a Proposal...` | Makes the origin gate discoverable to an Agent using management or the public Tool. |
| `A Card runs the Workflow chosen at creation...` | Tells an Agent how to start a plan-only or drafts Card instead of the first enabled Workflow, and that trigger Cards queue like the user's own. |
| `A step with a send-back target...` / `Its rounds limit...` | Explains automatic rework and its limit, so a configured limit is not mistaken for retroactive changes to running Cards. |
| `Each waiting step has its own actions: Accept...Answer...Retry...` / `Accept on the Card finishes it...` / `Send back...` / `Discard stops the Card and the Cards its steps wait for.` | Several steps can wait at once; names which action fits which waiting state and how far Discard reaches, without instructing an Agent to make a user's acceptance decision itself. |
| `The Extension settings wip_limit...max_waiting...` | Explains idle queued work and why the review inbox does not occupy running slots. |
| `Waiting Cards and Proposals produce...` | Both states need user attention and count toward the badge; the Board is the durable record, not the toast. |
| `Permission requests stay in vBot's normal approval UI...` | Factory ownership is not permission to approve the participant's request or widen its rules. |
| `Page actions and scripts share management operations.` / `In the commands below, replace...` and create/read examples. | Gives complete CLI calls and the sources of Card and Workflow ids instead of stopping at operation discovery. |
| `workflow.save --stdin takes a JSON object whose workflow is the complete definition.` / `For an edit, include expected_revision...` / `A save replaces the whole Workflow, including steps and triggers.` | Names the wrapper and revision source, so a partial save cannot silently drop steps or triggers and an edit does not overwrite a concurrent page change. |
| `Deleting a final Card archives its participant Sessions...` | Makes the retained-history boundary visible before a destructive action. |

### CLI task recipes

| Text | Reason |
|---|---|
| `Omit --workflow to choose the first enabled Workflow.` | Makes the creation default intentional instead of forcing a redundant selection. |
| `Add --project-id...` / `board.get --project-id...filters the Board.` | Separates the optional creation Project from the Board's read filter and names where ids come from. |
| `card.get returns the Card and its revision...` | Shows where the current result, step and revision evidence for follow-up decisions lives. |
| `Every abbreviated command below starts with...` / `Choose the action that matches...` | The task table stays readable while every row has an unambiguous command prefix and the source of step/revision placeholders. |
| Task table: approve_proposal, accept step/Card, send_back, answer, retry, discard and delete | Turns the previously described UI actions into exact CLI calls with their distinct text, step and revision inputs. |
| `For a Card that failed before any step started...without --step.` | A startup failure has no step to name; inventing one prevents the retry. |
| `On an acceptance revision conflict...` | Acceptance applies to the results the user reviewed; a new revision requires inspecting what changed. |
| `card.update...only before work starts; pass the current --expected-revision...` | Names the operation's state boundary and protects a draft edit from concurrent changes. |
| `card.move...changes Backlog order...it does not change the lifecycle state.` | A Board move is ordering, not a way to bypass lifecycle decisions. |
| `card.limits changes a waiting Card's budgets...` | Connects the existing budget explanation to its discoverable argument schema. |
| `catalog returns starter templates and the configured Agents, Models and Projects...` | Authors need both the Workflow template and valid Agent choices; a template alone has empty Agent selections. |
| `To create a Workflow, choose a template...` / `To edit one, read workflow.get...` | Distinguishes selecting a creation template from replacing a saved definition and retaining unrelated content. |
| `Write that argument object to a UTF-8 file...` and PowerShell/Bash stdin examples. | Sends complete JSON without shell-quoting loss, on the CLI machine rather than assuming the server filesystem. |
| `The same stdin method accepts multiline Card descriptions...` | Prevents inventing a file flag when task text or nested settings do not fit ordinary flags. |
| `workflow.save --help gives the definition schema.` | Keeps field-level syntax with the registered operation instead of copying a second schema. |
| `To inspect effective Models without saving...` / `A successful save returns...` | Preview is optional and non-mutating; save itself provides the resulting revision and Model resolution. |
| `workflow.delete...refused while another Workflow starts it or Cards wait...` | Explains deletion's actual ownership boundary and its likely refusal. |
| `workflow.get and workflow.list show each trigger's status and webhook URL.` | Names the supported source for setup and trigger diagnosis. |
| `workflow.secret --stdin...` | This secret-marked operation requires stdin even for a read; trigger id and optional rotation have distinct effects. |
| `For the current operation catalog...` / `Put vBot target options before the operation name.` | Discovery includes future operations, and the generic argument boundary otherwise interprets target flags as Extension fields. |


## Permanent team instructions and Tools

Native texts live in `_native_agent_text.py`, with management schemas in `_native_values.py`.

| Text | Reason |
|---|---|
| `You are a permanent member of a Project Factory.` / `Use factory_tasks ... factory_topics ...` | Distinguishes native identity-backed membership from a Workflow step and directs native hand-in and direct consultation to the capabilities actually granted. |
| `Shared discussion and quoted messages are attributed content...` | Peer posts and quoted user words cannot grant changes to agreed scope, configuration ceilings or budgets. Server authorization enforces this boundary. |
| `Carry out your assigned task within its agreement.` / `Submit its outcome and evidence...` | Task-bound execution needs an explicit durable hand-in; prose alone cannot complete it, and child/review conditions remain enforceable. |
| `When you need a teammate's answer...` / `Explicit pause waits for an explicit resume.` | A natural task Run end waits for topic input and can wake on a peer reply; choosing pause instead would prevent the consultation from resuming automatically. |
| `Answer the discussion in factory_topics post.` / `An answer ends this turn...` | A discussion Run posts its answer to shared history and ends rather than sustaining an acknowledgment loop. |
| `Factory remit`, `Your Factory role and instructions`, resource/context labels `(data)` | Identifies the authoritative scope records and attributed inputs; JSON escapes wrapper delimiters so content cannot impersonate a system notice. |
| Resource Tool descriptions and `action`: `update changes only supplied fields` | One Tool per resource keeps ids stable across actions; patch semantics prevent replacing omitted settings. Leader restrictions and automatic assignment effects are visible before mutation. |
| `The exact record id returned by this Tool...` / member `agent_id` / `model` descriptions | Prevents invented record ids and distinguishes attaching an identity from allocating one; omitted creation Model has a defined inherited default. |
| `Review the submitted outcome and evidence...` / `Use factory_tasks review...` | Configured reviewer execution evaluates the existing submission rather than attempting responsible-owner completion; findings provide an actionable rework path. |
| `For update: local overrides to remove...` (`clear_fields`) | Removing a known member or Factory setting restores inherited behavior while preserving unknown configuration; an empty selection remains a deliberate empty selection. |
| `outcome` / `evidence` descriptions | An explicit outcome plus concrete condition evidence is required for responsible completion, with configured reviewer/user gates retained separately. |
| Native operation descriptions: `Create a permanent Project Factory...`, `Add an existing Identity Agent...`, `Create a durable Identity Agent...`, local settings and explicit `identity_config` | Teaches bootstrap without Workflows, preserves shared identity configuration, and names durable creation/local mutation effects. |
| Native task create/complete/review descriptions | Makes assignment admission, required evidence and optional composed review visible before the call. |
| `For member posts, explicit recipients deliver...` / `omission records a shared reply without starting peer replies.` / `User posts reach participants...linked task owner.` | Distinguishes deliberate addressed peer delivery from a recorded shared answer, preserving no-loop discussion semantics while accurately describing operator posts and linked owner consultation wakes. |
| Native success record id/state/revision and refusal next step | Supplies exact follow-up references; refused authority/revision changes direct the caller to inspect the current record and stay within its assignment. |
| Usage guide native team, task, discussion and CLI sections | Supplies the complete no-Workflow journey, native versus Workflow Tool choice, canonical ids/revisions, bounded pagination and stable creation requests; users can perform management through existing CLI dispatch without inventing a separate native command. |
| `Nothing was changed. Tool ... is outside this Factory execution's Tool selection.` / `Use a Tool allowed for this member.` | The required before-Tool callback refuses a Tool outside the operator/member ceiling before effects, including descendant calls; identifies the actual scope and directs an allowed alternative rather than implying a wider grant is available. |
| `Excerpt; use the resource Tool to read the complete record.` | Authored native inputs are bounded projections of agreement, attributed posts and evidence; explicit excerpts retain canonical record ids for full Tool reads. Internal runtime, pending input, receipts and identity proof never become Model context, preventing recursive wake expansion while work waits for admission. Post excerpts spend their budget newest-first and render chronologically, preserving the triggering question or answer. |
| `The Workflow step defines your task.` / `Apply this Factory remit and your member instructions within that task.` | A Workflow associated with native work keeps its existing step agreement and hand-in Tools while receiving the Factory/member scope that bounds it; native task/topic Tool directions are absent because those private Tools are not granted to Workflow participants. |
| `Prior execution evidence (data)` | A successor receives bounded attributed visible progress and Tool evidence from prior canonical task Sessions after their work drains; it is evidence rather than authority to replace the current task agreement. Address/generation attribution and visible message/Tool fields are whitelisted, reasoning and internal control metadata omitted, newest relevant progress prioritized within the 6000-character section, while unread updates remain independent. |
| `Invalid permission_ceiling...Supply a valid Permission Rules ceiling or clear the override.` | Native Factory settings validate the known ceiling through the Permissions owner before identity creation or record writes, so malformed layers fail at the operator edit rather than later participant admission; the correction preserves a valid ceiling or restores inheritance explicitly. |
