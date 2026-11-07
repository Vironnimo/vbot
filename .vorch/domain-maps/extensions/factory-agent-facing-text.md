# Factory Agent-facing text

The Factory's runtime text lives in `resources/extensions/factory/agent_text.py`; the usage section lives in `resources/skills/vbot-docs/references/extension-usage.md`. These are preventive reasons unless a concrete evaluation is named. Contracts and owners: `extensions/factory.md`.

## Participant instructions and input

| Text | Reason |
|---|---|
| `Work in isolation according to the Project instructions.` | Makes the Project, not a hard-coded Factory Git workflow, decide how to isolate the requested change. |
| `Do not integrate into the base branch before the user accepts the Card.` | A normal coding profile often merges automatically after its tests; Factory separates hand-in from permission to integrate. This is an instruction, not a shell sandbox. |
| `Run the checks the Project prescribes, then call factory_submit_work.` | Produces a verifiable hand-in rather than a final answer that leaves the Card working indefinitely. |
| `If you need a decision or information from the user, call factory_report_blocked.` | Routes genuine blockers to the durable review inbox without requiring the user to watch a participant transcript. |
| `Review the submitted commit range without changing files or branches.` | An independent Reviewer must judge the handed-in range, not fix it silently or move its refs. Shell availability still permits tests. |
| `Judge whether the idea is worth having and whether the implementation is correct, simple and fits the Project conventions.` | A review of compilation alone would not give the user the requested judgment of value and fit. |
| `Check whether previous findings were addressed, then call factory_submit_review.` | Fresh Reviewers receive earlier findings but not the Worker's transcript; this connects rework evidence to the verdict. |
| `Your recommendation tells the user whether to accept and what you would add or change.` | Makes the recommendation useful to a user beside the loop rather than a private exchange between two Models. |
| `Implement this Factory Card.` / `Independently review this Factory submission.` | Names the immediate task while stable rules remain in participant instructions, not duplicated in every input. |
| `Card (data)`, `Submission (data)`, `Default base branch (data)`, `User comments (data)`, `Previous review findings (data)` | Separates authored instructions from untrusted Card/review text; JSON escapes angle brackets so content cannot close a reminder wrapper. Exact commit range and check summaries give an independent Reviewer evidence without the Worker's history. |

## Continuations

| Text | Reason |
|---|---|
| `Continue implementing the Card, then call factory_submit_work.` | One successful but non-submitting turn is not a completed Card; a bounded nudge names the missing hand-in. |
| `If you need the user's decision or information, call factory_report_blocked.` | The nudge leaves a real blocker path instead of demanding endless implementation. |
| `Continue the accepted integration, then call factory_report_integration.` | Keeps the phase distinct from pre-acceptance implementation. |
| `If it requires code changes, report needs_changes and submit the changed work for review.` | Acceptance covers the reviewed head, not unreviewed conflict resolutions. |
| `Finish the independent review with factory_submit_review; an ordinary answer does not submit the verdict.` | A prose answer alone cannot update the Card; one nudge repairs that omission before the fresh-Reviewer fallback. |
| `The previous Reviewer did not submit a verdict.` | Explains a fresh attempt without representing the earlier lack of verdict as a rejection. |
| `Review the submission independently and call factory_submit_review.` | Fresh retry participants still need the same structured finish, not another informal answer. |
| `The Reviewer requested changes.` | States the event as a system continuation, not fake text from the user. |
| `Address the findings in your isolated change, run the prescribed checks, then call factory_submit_work.` | Connects findings to rework and another independent hand-in. |
| `The Card has not been accepted for integration.` | Rework must not treat Reviewer feedback or the user's send-back as merge permission. |
| `The user sent the Card back.` / `Act on their comment in your isolated change, then call factory_submit_work.` | Preserves the same Worker Session and gives the user's operational action its exact scope. |
| `vBot restarted.` / `Continue this Card from its saved phase and use the matching Factory Tool to finish the hand-in.` | Recovery reports an interruption, not a new objective or permission to integrate. |
| `The Card was discarded.` / `Remove your worktree and branch as the Project instructions say.` | Tells the existing Worker to clean up only its isolated work through the Project's method. |
| `Do not integrate the discarded work; this cleanup gets one turn.` | Discard does not authorize landing the result and cannot start a self-sustaining cleanup loop. |
| `Make the integration changes in isolation, run the prescribed checks, then call factory_submit_work.` | Resolving integration changes returns to a reviewable submission. |
| `The changed result needs independent review before integration.` | Clarifies that the old acceptance does not extend to the changed code. |
| `The user accepted.` / `Integrate the reviewed commit ... according to the Project instructions, then clean up ... then call factory_report_integration.` | Authorizes the exact reviewed head and the Project's own merge/PR method, with a structured terminal result. |
| `If integration requires code changes, report needs_changes and resubmit the changed work instead of landing it unreviewed.` | Prevents merge conflicts from becoming an unreviewed implementation path. |
| `Comment or findings (data)` | A continuation's free text is evidence; it cannot impersonate Factory instructions. |

## Tools and outcomes

| Text | Reason |
|---|---|
| `Create proposed work and read Cards on the Software Factory Board.` | Distinguishes managed work hand-in from doing the implementation in the calling Session. |
| `Work starts automatically for the user's requests and authorized automations; your own ideas wait for the user's approval.` | The Agent must know the material side effect before create; no opt-in is added because the origin gate contains autonomous proposals. |
| `Create proposed work and read Software Factory Cards.` | On-demand summary lets an Agent find the capability without loading every private participant Tool. |
| `project`: `Required for create: a Project id with an enabled Factory setup.` | A path or display name is not Project identity and cannot select an arbitrary execution folder. |
| `Omit for list to include every Project.` | The omission rule avoids inventing a Project filter. |
| `title`: `Required for create.` / `description`: `Required for create: the user's objective and acceptance criteria, without extra requirements.` | Conditional fields cannot be root-required with list/get. In the first real Factory eval, the creator added null/undefined handling absent from the request; the description must preserve the user's scope rather than invent requirements. |
| `state`: `For list. Omit for every state.` | The enum carries its values; the text supplies only action scope and omission. |
| `id`: `Required for get: the Card id from create or list.` | Prevents manufacturing a Card identity from the title or a branch. |
| `Submit your isolated change for independent review and end this turn; this does not authorize integration.` | Submission stops the Worker and explicitly does not grant a merge. |
| `branch`: `The local branch containing the finished change.` / `base`: `The branch the change is based on.` | Git verification reads local refs rather than accepting a remote URL or a diff pasted as branch text. |
| `checks`: `Which prescribed checks ran and their outcomes; name any not run.` | Makes omitted validation visible rather than inferring readiness from a generic success claim. |
| `Ask for a decision or information from the user, pause the Card and end this turn.` | Keeps report_blocked for real human dependencies, not ordinary implementation/review work. |
| `Submit your independent verdict and end this turn.` | The current Reviewer owns exactly one structured judgment. |
| `An approval sends your recommendation to the user; requested changes return to the Worker.` | Tells the Reviewer who reads each result and what its verdict triggers. |
| `recommendation`: `Required for approve: whether the idea and implementation are good, and what you would add or change.` | An approval without reasoning is insufficient for the user's acceptance decision. |
| `findings`: `Required and non-empty for changes_requested: concrete issues the Worker can address.` / `Omit when there are no findings.` | Gives rework actionable input, without manufacturing issues for a clean approval. |
| `Report the accepted change's integration and end this turn.` / `Changed commits go through independent review again.` | Names the post-acceptance result and the changed-head safety boundary. |
| `reference`: `For integrated: the merge commit or PR URL.` / `Omit for the submitted commit when no other reference exists.` | Supports generic Project integration without requiring a PR or an invented URL. |
| `note`: `For needs_changes or failed: what prevents the reviewed change from integrating.` / `Omit when nothing needs explaining.` | Supplies recovery evidence only when relevant. |
| `Card created and queued; work continues independently within the WIP limit.` | The create result confirms admission without pretending the implementation is complete. |
| `Progress and review results appear on the Board, not in this Session.` | The first real Factory eval followed a queued acknowledgment with status reads and attempted runtime diagnosis. Results belong to the durable Board and attention flow; the creator does not receive the Worker's continuation as a Session reply. |
| `Proposal saved; work waits for the user's approval on the Board.` | An autonomous idea is recorded, not admitted; the Agent cannot start implementation or treat the human gate as background work to await. |
| `Submission recorded; an independent Reviewer takes over.` / `This turn ends here.` | Prevents a successfully submitting Worker from continuing implementation or integration. |
| `Question sent to the user; the Card waits for their answer.` | The Worker can end instead of watching for a response in a loop. |
| `Review recorded; the Factory handles the next step.` | The Reviewer does not need to wake the Worker or tell the user separately. |
| `Integration recorded.` / `Integration changes require another submission and review.` / `Integration could not finish; the Card waits for the user.` | Distinguishes completion, re-review and an operational failure, all with the same turn-end convention. |
| Refusals name the actual role, state, ref, field or stale Run and the allowed next action; `Nothing was changed.` applies only to pre-effect refusals. | A participant must repair the call or stop using obsolete authority, not bypass the Factory with shell mutation. Unexpected failures need outcome-unknown guidance and an exact Card read, not an automatic replay. |
| `The outcome is not confirmed.` / `Read the Board with factory_card action list before repeating a create call.` | A publication failure can follow a durable Card creation. Reporting rollback would cause a duplicate on retry; the state read must precede another mutation. |
| `Read this Card on the Board before repeating the phase hand-in.` | A participant callback can fail after a phase transition; the old Run must not automatically replay its former authority. |

## Usage reference

| Text | Reason |
|---|---|
| `Software Factory turns work Cards into isolated changes...` / `It is bundled as factory...` | Gives the purpose, installed identity and durable user inbox without implying the user supervises every turn. |
| `In Setups, select a registered Project's Worker and Reviewer Team profiles...` / `Empty Model and effort selections inherit...` | Uses existing Team context and explains overrides without inventing another Agent type or requiring configuration of every scalar. |
| `The Project's instructions decide...` | Keeps isolation and integration policy outside the Extension. |
| `Cards the user creates...queue automatically...own idea stays a Proposal...` | Makes the origin gate discoverable to an Agent using management or the public Tool. |
| `Review changes return to the same Worker Session...` | Explains automatic rework and why each review is independent. |
| `Accept asks the Worker...Send back...Answer...Retry...Discard...` | Names the available operational actions without instructing an Agent to make a user's acceptance decision itself. |
| `Changed commits require another review...merged...reference_only...` | Stops an Agent configuring a PR workflow from promising ancestry that has not occurred. |
| `The Extension settings wip_limit...max_waiting...` | Explains idle queued work and why the review inbox does not occupy Worker slots. |
| `A waiting Card produces...` | The durable record is the Board; a transient toast is not guaranteed delivery or a second inbox. |
| `Permission requests stay in vBot's normal approval UI...` | Factory ownership is not permission to approve the participant's request or widen its rules. |
| `Page actions and scripts share management operations` and discoverable CLI examples | Routes automation through the declared owner instead of editing its database. |
| `Use Card and Project ids from those reads.` | Gives the source of opaque ids for follow-up operations. |
| `Deleting a final Card archives its participant Sessions...` | Makes cleanup's retained-history boundary visible before a destructive action. |
