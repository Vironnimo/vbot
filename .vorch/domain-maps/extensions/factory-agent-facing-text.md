# Factory Agent-facing text

The Factory's runtime text lives in `resources/extensions/factory/agent_text.py`; the task texts of the bundled Workflow templates live in `resources/extensions/factory/_workflows.py`; the usage section lives in `resources/skills/vbot-docs/references/extension-usage.md`. These are preventive reasons unless a concrete evaluation is named. Contracts and owners: `extensions/factory.md`.

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
| `Do step ... (n of m) of the Factory workflow ... for this Card.` | Names the participant's place in the Workflow so it knows what came before and that later steps build on its result. |
| `Card (data)`, `User comments (data)`, `Results of earlier steps (data)`, `Findings your step sent back earlier (data)` | Separates authored instructions from untrusted Card and result text; JSON escapes angle brackets so content cannot close a reminder wrapper. Recorded results of earlier steps are the only way a later step receives a plan, drafts or the branch an implementation named. |
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
| `project`: `Required for create: a Project id with an enabled Factory setup.` / `Omit for list to include every Project.` | A path or display name is not Project identity and cannot select an arbitrary execution folder; the omission rule avoids inventing a filter. |
| `title`: `Required for create.` / `description`: `Required for create: the user's objective and acceptance criteria, without extra requirements.` | Conditional fields cannot be root-required with list/get. In the first real Factory eval, the creator added null/undefined handling absent from the request; the description must keep the user's scope. |
| `workflow`: `For create: the name of one of the Project's Factory workflows, such as Plan or Implement.` / `Omit to use the Project's default workflow.` | A user who asks for "a plan" or "three drafts" picks a Workflow by its name; the default covers ordinary requests. |
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
| `workflow_upgrade`: `This Card started under an earlier Factory version and cannot resume where it stopped.` / `Its results are kept: Accept continues with its next step or finishes it, and Send back reworks a step with your guidance.` | Shown to the user on Cards that ran under role- or stage-based Workflows; it explains why the Card waits and what each action does with the kept results. |

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
| Document step converted from an older setup: `Do the work the Card asks for without changing files or branches and hand in the complete document as result.` | Older document stages had their boundary in Factory rules; the converted step carries it in its task. |
| Integrate: `Integrate the reviewed change into the base branch as the Project instructions describe, then clean up the worktree and branch it leaves behind.` | User decision 2026-11-07: integration is an ordinary step behind the user's approval, done the Project's way (merge, PR, rebase). Cleanup is part of it because nothing else removes the worktree. |
| Integrate: `Name what landed in your summary.` | The Board shows the summary as the Card's final record; without it the user cannot see whether a merge commit or a PR resulted. |

## Usage reference

| Text | Reason |
|---|---|
| `Software Factory runs work Cards through a Project's Workflows...` / `It is bundled as factory...` | Gives the purpose, installed identity and durable user inbox without implying the user supervises every turn. |
| `In Setups, give a registered Project one or more Workflows...` / `A Workflow is a list of steps...` / `Steps run one at a time in list order. A step receives the results of the steps it comes after...` | User decision 2026-11-07: Factory work differs per Project and task, so an Agent configuring or explaining it must know steps, their start conditions, approval, send-back targets and both Agent sources. |
| `The Factory does no Git...` | User decision 2026-11-07; prevents an Agent from expecting the Factory to verify or merge a branch, or a discarded Card to clean up its worktree. |
| `Cards the user creates...queue automatically...own idea stays a Proposal...` | Makes the origin gate discoverable to an Agent using management or the public Tool. |
| `A Card runs the Workflow chosen at creation...` | Tells an Agent how to start a plan-only or drafts Card instead of the default. |
| `A step with a send-back target...` / `Its rounds limit...` | Explains automatic rework and its limit, so a configured limit is not mistaken for retroactive changes to running Cards. |
| `Accept...Send back...Answer...Retry...Discard...` | Names the available actions without instructing an Agent to make a user's acceptance decision itself. |
| `The Extension settings wip_limit...max_waiting...` | Explains idle queued work and why the review inbox does not occupy running slots. |
| `Waiting Cards and Proposals produce...` | Both states need user attention and count toward the badge; the Board is the durable record, not the toast. |
| `Permission requests stay in vBot's normal approval UI...` | Factory ownership is not permission to approve the participant's request or widen its rules. |
| `Page actions and scripts share management operations` and CLI examples / `Use Card and Project ids from those reads.` | Routes automation through the declared owner instead of editing its database, and names the source of opaque ids. |
| `Deleting a final Card archives its participant Sessions...` | Makes the retained-history boundary visible before a destructive action. |
