# Software Factory

Software Factory runs work Cards through Workflows - plans, drafts, changes, reviews and integration, each done by an Agent you choose - and the user accepts the result. It is bundled as `factory`; the `Software Factory` page holds the Board with its `Waiting for you` review inbox, and the Workflows page.

A Workflow is global and can fix a Project: then all its Cards work there and its steps can use the Project's Team. Without one, each Card names its Project or has none, and then works in a folder of its own. Start from templates such as One Agent, Review pushes, Implement, Plan or Plan from drafts. Each step has a name, an Agent (any of your Agents, or a member of the Project's Team, with optional Model and thinking effort overrides) and a task, or it starts another Workflow as a new Card and can wait until that Card is done. A step starts once the steps it comes after are done, so steps without a common predecessor run in parallel. A step receives the results of the steps it comes after, and of theirs, and nothing else. A step can wait for the user's approval before it starts, and the Workflow can wait for the user's acceptance when all steps are done. A step with a send-back target can return that step's result with findings instead of passing it on.

Triggers create Cards for a Workflow by themselves. A schedule takes a cron expression such as `0 9 * * 1-5`, `every 2h`, `in 30m` or an ISO time. A webhook gets a URL and a secret; a GitHub webhook (content type `application/json`, the secret, events such as push) creates one Card per delivery with the event's details, and its Project can be the Project whose Git remote matches the repository. GitHub must reach the vBot address.

Workflows and their Steps can independently limit Runs, Tool calls, input plus output Tokens and Model cost in USD; blank or 0 means unlimited, the default. A Step gets a new budget on rework; the Card keeps its total. Pause and Retry retain consumption. The Agent receives a budget notice near the limit to finish or report progress and the additional budget needed. The first reached limit pauses that Step; a running Model request can overshoot a Token or cost limit. On a waiting Card, change its limits and Retry the Step to resume. Through management, `card.get` provides its revision and consumption per Step; `card.limits` takes that `expected_revision` and replacements for `card_limits`, or `step_limits` together with `step`. Omit a scope to keep it, or pass `{}` to remove its limits.

- The Factory does no Git. Participants work in isolation, commit, merge and clean up as the Project's instructions describe; the Implement template ends with an Integrate step that waits for the user's approval and lands the reviewed change.
- A Card runs the Workflow chosen at creation (`factory_card` `workflow`, or the first enabled Workflow); running Cards keep the Workflow they started with. Cards the user creates on the page or through management operations, and Cards from triggers, queue automatically; `factory_card` queues direct user requests and currently authorized automations, while an Agent's own idea stays a Proposal until the user approves it.
- A step with a send-back target runs as a fresh participant each time. Its findings return to the target step's Session, and every step after the target runs again. Its rounds limit (default 2) is the number of automatic send-backs; past it, or when a participant is blocked, that step waits for the user with the result and the reason while parallel steps continue.
- Each waiting step has its own actions: Accept starts a step waiting for approval or overrules a step that exhausted its rounds, Answer resolves a blocked question, Retry resumes a failed, interrupted or paused step. Accept on the Card finishes it when it waits for acceptance. Send back supplies a comment and can pick the step to rework, and Discard stops the Card and the Cards its steps wait for. Neither Send back nor Discard reverts anything a participant already changed.
- The Extension settings `wip_limit` (default 2) and `max_waiting` (default 0, unlimited) control admission; waiting Cards do not occupy running slots, and accepted work waits in Backlog when all slots are busy.
- Waiting Cards and Proposals produce a WebUI/tray notification and contribute to the page badge; the Board, not the transient toast, keeps the durable record.
- Permission requests stay in vBot's normal approval UI; open the linked Session from the Card rather than changing Permission Rules or granting the request yourself.

Page actions and scripts share management operations; each prints its arguments with `--help`, and `--stdin` accepts a JSON argument object without shell quoting:

```bash
vbot extensions operations factory
vbot extensions run factory workflow.list
vbot extensions run factory workflow.save --help
vbot extensions run factory board.get
vbot extensions run factory card.create --help
vbot extensions run factory card.get --help
```

Use Card, Workflow and Project ids from those reads. `workflow.save` replaces the whole Workflow: read it with `workflow.get` first and pass its `expected_revision`; omit `expected_revision` to create one. `workflow.secret` shows a webhook's secret for the sender's settings. Deleting a final Card archives its participant Sessions; done and discarded Cards otherwise keep readable Sessions on the Board.
