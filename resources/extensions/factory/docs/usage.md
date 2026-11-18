# Software Factory

Software Factory keeps a permanent team, native tasks and shared discussions for a registered Project. Select a Project and message its Leader in the overview. The first message creates a permanent Leader using the Project's default Model or the global Agent default; the setup dialog can select an existing Leader or an explicit Model. Team members also have direct conversations. A member can take work requested in its own conversation, and the task opens with the discussion, questions and result together. Existing Agents keep their identity, personal Memory and private Skills; membership instructions and configuration apply locally to the Factory.

Project work shows team tasks and Workflow runs together, with decisions first. Members can use `factory_tasks` with `action: history` and a title query, then `get` for a result. Native members use `factory_tasks` to organize assigned work and submit outcomes, and `factory_topics` to consult teammates directly. The Leader also receives member/settings Tools. Completing a task needs an outcome and evidence addressing its conditions; child tasks and prerequisites must finish first. Independent review and user approval apply only when configured, and both approvals are retained separately when both are required.

The user sets the Factory remit, allowed configuration and budgets. An Agent can create/assign children under its own task; unrelated initiative remains an unassigned backlog proposal until authorized. Shared discussion is attributed content, not a new user authorization. Assignment starts eligible work automatically; pause, cancellation and Factory archive stop admission while retaining history and spending. Reassignment fences old execution before the successor continues.

## CLI: native teams, tasks and discussions

Use `vbot extensions run factory <operation> --help` for the current flat argument schema. `catalog` supplies Project, Agent and Model ids; resource reads supply canonical ids and revisions. Lists are bounded and return `next_after` for the next `--after` page. Native task detail includes children, results, events, topics and canonical Session addresses.

```bash
vbot extensions run factory factory.create --project-id <project-id> --leader-agent-id <agent-id> --remit "Build the agreed product"
vbot extensions run factory member.add --factory-id <factory-id> --agent-id <agent-id> --role Reviewer
vbot extensions run factory task.create --factory-id <factory-id> --title "Implement feature" --objective "Requested behavior" --conditions "Agreed checks pass" --assignee-id <member-id>
vbot extensions run factory task.get <task-id>
vbot extensions run factory topic.create --factory-id <factory-id> --title "Design discussion"
vbot extensions run factory topic.post <topic-id> --text "The question or answer"
```

For a new Leader, omit `leader_agent_id` and supply `leader_name`, `leader_model` and a stable `request_id`. `member.create` takes `factory_id`, `name` and a stable `request_id`; an omitted Model inherits the Leader's Model. Keep the same request id and payload when retrying an interrupted creation so it reuses its reserved Identity Agent.

Use `task.update`, `assign`, `pause`, `resume`, `cancel`, `reopen`, `complete` or `review` with the record's current `expected_revision`. `complete` takes `outcome` and `evidence`; `review` takes `approve` and findings when requesting changes. `topic.invite` takes member ids and optional question text; `topic.get` returns bounded posts, with integer `after` selecting posts beyond that sequence. `factory.update`, `member.update`, `member.retire`, `topic.invite`, `topic.close` and `factory.archive` also require the observed revision. On conflict, inspect the latest record before applying the intended change.

Use `factory.update` or `member.update` `clear_fields` to remove named configuration overrides and restore inherited settings; an explicit empty selection keeps no entries. Model and thinking effort resolve from the member override, then the Factory default, then the Identity Agent and its inherited defaults. Member Model/effort overrides and `allowed_skills`, `allowed_tools` and `denied_tools` narrow local configuration within the operator's Factory ceiling. `member.add` preserves the linked Identity Agent's global settings. Explicit `identity_config` edits are supported only for Factory-created identities; ordinary Agents settings own shared identities. Retiring a member retains history, and archiving a Factory retains records while archiving its managed Sessions.

## Optional global Workflows

Workflows remain an independent way to run reusable step graphs. `factory_card` creates Workflow Cards; native members use native task/topic Tools. A native task can select `workflow_id`, which links a Workflow execution as evidence without completing the responsible owner's task by itself.

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

## CLI: create, inspect and control Cards

Page actions and scripts share management operations. In the commands below, replace `<workflow-id>` with an id from `workflow.list`, `<card-id>` with an id from `board.get` or `card.create`, and `<title>` and `<objective>` with the requested work:

```bash
vbot extensions run factory workflow.list
vbot extensions run factory board.get
vbot extensions run factory card.create --workflow <workflow-id> --title "<title>" --description "<objective>"
vbot extensions run factory card.get <card-id>
```

Omit `--workflow` to choose the first enabled Workflow. Add `--project-id <project-id>` to create work in a Project when the Workflow does not fix one; Project ids come from `catalog`. `board.get --project-id <project-id>` filters the Board. `card.get` returns the Card and its revision, step state, recorded outputs, comments, participant Sessions, approvals and usage.

Every abbreviated command below starts with `vbot extensions run factory`. Choose the action that matches the user's decision and the Card's state; `<step-id>` comes from `card.get`, and `<revision>` is its `card.revision`:

| Task | Command |
|---|---|
| Queue a Proposal the user approved | `card.approve_proposal <card-id>` |
| Accept a waiting step | `card.accept <card-id> --step <step-id> --expected-revision <revision>` |
| Accept the completed Card | `card.accept <card-id> --expected-revision <revision>` |
| Return work with the user's feedback | `card.send_back <card-id> --step <step-id> --comment "<feedback>"` |
| Answer a blocked step's question | `card.answer <card-id> --step <step-id> --text "<answer>"` |
| Resume a failed, interrupted or paused step | `card.retry <card-id> --step <step-id>` |
| Discard unfinished work | `card.discard <card-id>` |
| Delete a done or discarded Card | `card.delete <card-id>` |

For a Card that failed before any step started, use `card.retry <card-id>` without `--step`. On an acceptance revision conflict, read the current Card and its results before accepting that revision. Deleting a final Card archives its participant Sessions; done and discarded Cards otherwise keep readable Sessions on the Board.

`card.update <card-id>` changes `--title` or `--description` only before work starts; pass the current `--expected-revision` to protect the edit. `card.move <card-id> --position <number>` changes Backlog order using the Board's positions; it does not change the lifecycle state. `card.limits` changes a waiting Card's budgets as described above; its `--help` gives the limit fields.

## CLI: create and edit Workflows

`catalog` returns starter `templates` and the configured Agents, Models and Projects in `catalog`. To create a Workflow, choose a template, set its name and each Agent step's `agent` from the catalog, and adjust its tasks, dependencies, Project and triggers for the request. To edit one, read `workflow.get <workflow-id>` and keep its id and all fields outside the requested change.

`workflow.save --stdin` takes a JSON object whose `workflow` is the complete definition. For an edit, include `expected_revision` with the `workflow.revision` from the read; for creation, omit it or use `0`. A save replaces the whole Workflow, including steps and triggers. Write that argument object to a UTF-8 file `<arguments-file>` on the CLI's computer, then send it through stdin:

```powershell
Get-Content -Raw -Encoding utf8 "<arguments-file>" | vbot extensions run factory workflow.save --stdin
```

```bash
vbot extensions run factory workflow.save --stdin < "<arguments-file>"
```

The same stdin method accepts multiline Card descriptions and other structured operation arguments. `workflow.save --help` gives the definition schema. To inspect effective Models without saving, send just the `workflow` object wrapper to `workflow.preview --stdin`; the result maps each Agent step to its resolved Model, with an empty value when unresolved. A successful save returns the saved Workflow, revision and effective Models.

`workflow.delete <workflow-id>` removes the Workflow and its triggers; it is refused while another Workflow starts it or Cards wait to start with it. `workflow.get` and `workflow.list` show each trigger's status and webhook URL. `workflow.secret --stdin` reads a webhook secret using `{"id":"<workflow-id>","trigger":"<trigger-id>"}`; take the trigger id from that Workflow and add `"rotate":true` only to replace its current secret.

For the current operation catalog, use `vbot extensions operations factory`; append `--help` to an operation for its exact arguments. Put vBot target options before the operation name.
