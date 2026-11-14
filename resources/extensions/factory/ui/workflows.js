import { normalizeLimits, limitsProblem } from './limits.js';
import { t } from '../../../../webui/src/lib/i18n.js';

// One global Workflow as the page edits it. The server validates and owns the
// format (`_workflows.normalize`); this module keeps drafts in that shape,
// gives new steps and triggers ids the server keeps, keeps references between
// steps consistent while the user reorders or removes steps, and names the
// first problem before an autosave sends a Workflow the server would refuse.
//
// A Workflow is a graph of steps. A step starts once every step in `after`
// is done (all listed before it), so independent steps run in parallel. It
// can wait for the user's approval first. An `agent` step has an Agent do its
// task and with `returns_to` can send an earlier step's result back, at most
// `rounds` times; a `workflow` step starts another Workflow as a child Card.
// Triggers create Cards on a schedule or when a webhook is called.

export const MAX_STEPS = 30;
export const MAX_TRIGGERS = 10;
export const DEFAULT_ROUNDS = 2;
// A trigger's `project` that takes the Project from the event's repository.
export const REPOSITORY_PROJECT = '@repository';
const ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789';
const copy = (value) => JSON.parse(JSON.stringify(value));

// Ids match the server's `[a-z0-9]{1,24}`, so a saved draft keeps them and
// the diagram, Model keys and trigger URLs stay stable while the user edits.
export function newId(prefix, taken = []) {
  const used = new Set(taken);
  for (;;) {
    const bytes = crypto.getRandomValues(new Uint8Array(7));
    const id = prefix + [...bytes].map((byte) => ALPHABET[byte % 36]).join('');
    if (!used.has(id)) return id;
  }
}

// The server's key for a step's effective Model.
export const stepKey = (workflow, step) => `${workflow.id}/${step.id}`;

export const stepName = (step, index) =>
  step.name.trim() || t('factory.stepPosition', { position: index + 1 });

// Every step this step follows, directly or through other steps.
export function ancestors(steps, stepId) {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const found = new Set();
  const todo = [...(byId.get(stepId)?.after ?? [])];
  while (todo.length) {
    const current = todo.pop();
    if (found.has(current) || !byId.has(current)) continue;
    found.add(current);
    todo.push(...byId.get(current).after);
  }
  return found;
}

// The steps a step can send work back to: those it follows, in list order.
export const returnTargets = (steps, index) => {
  const reachable = ancestors(steps, steps[index]?.id);
  return steps.slice(0, index).filter((step) => reachable.has(step.id));
};

function uniqueStepName(steps) {
  const taken = new Set(steps.map((step) => step.name.trim().toLowerCase()));
  for (let number = steps.length + 1; ; number += 1) {
    const name = t('factory.stepPosition', { position: number });
    if (!taken.has(name.toLowerCase())) return name;
  }
}

function editableStep(step) {
  return {
    id: step.id ?? '',
    name: step.name ?? '',
    kind: step.kind === 'workflow' ? 'workflow' : 'agent',
    source: step.source === 'team' ? 'team' : 'agent',
    agent: step.agent ?? '',
    model: step.model ?? '',
    effort: step.effort ?? '',
    task: step.task ?? '',
    after: [...(step.after ?? [])],
    approval: Boolean(step.approval),
    returns_to: step.returns_to ?? '',
    rounds: step.rounds ?? DEFAULT_ROUNDS,
    limits: { ...(step.limits ?? {}) },
    workflow: step.workflow ?? '',
    wait: step.wait !== false,
  };
}

// A new last step that comes after the current last step.
export function newStep(steps) {
  return editableStep({
    id: newId(
      's',
      steps.map((step) => step.id),
    ),
    name: uniqueStepName(steps),
    after: steps.length ? [steps.at(-1).id] : [],
  });
}

// References may point only backwards: `after` to earlier steps and
// `returns_to` to a step the step follows. Reordering or removing steps
// drops references that no longer hold.
export function repairSteps(steps) {
  steps.forEach((step, index) => {
    const earlier = new Set(steps.slice(0, index).map((item) => item.id));
    step.after = step.after.filter((id) => earlier.has(id));
    if (step.returns_to && !ancestors(steps, step.id).has(step.returns_to))
      step.returns_to = '';
  });
  return steps;
}

export function moveStep(steps, index, delta) {
  const moved = copy(steps);
  const [step] = moved.splice(index, 1);
  moved.splice(index + delta, 0, step);
  return repairSteps(moved);
}

// Steps that came after the removed step now come after its own
// predecessors, so removing a step in a chain keeps the chain.
export function removeStep(steps, index) {
  const removed = steps[index];
  const rest = copy(steps.filter((_, position) => position !== index));
  for (const step of rest)
    if (step.after.includes(removed.id))
      step.after = [
        ...new Set(
          step.after.flatMap((id) =>
            id === removed.id ? removed.after : [id],
          ),
        ),
      ];
  return repairSteps(rest);
}

function editableTrigger(trigger) {
  return {
    id: trigger.id ?? '',
    type: trigger.type === 'webhook' ? 'webhook' : 'schedule',
    schedule: trigger.schedule ?? '',
    enabled: trigger.enabled !== false,
    project: trigger.project ?? '',
    title: trigger.title ?? '',
    task: trigger.task ?? '',
  };
}

export function newTrigger(triggers, type) {
  return editableTrigger({
    id: newId(
      't',
      triggers.map((trigger) => trigger.id),
    ),
    type,
    schedule: type === 'schedule' ? '0 9 * * 1-5' : '',
    project: type === 'webhook' ? REPOSITORY_PROJECT : '',
  });
}

// The draft of a saved Workflow, without the server's trigger status.
export function editableWorkflow(workflow = {}) {
  return {
    id: workflow.id ?? '',
    name: workflow.name ?? '',
    description: workflow.description ?? '',
    enabled: workflow.enabled !== false,
    project_id: workflow.project_id ?? '',
    final_approval: Boolean(workflow.final_approval),
    limits: { ...(workflow.limits ?? {}) },
    steps: (workflow.steps ?? []).map(editableStep),
    triggers: (workflow.triggers ?? []).map(editableTrigger),
  };
}

function uniqueName(name, workflows) {
  const taken = new Set(
    workflows.map((workflow) => workflow.name.trim().toLowerCase()),
  );
  if (!taken.has(name.trim().toLowerCase())) return name;
  for (let number = 2; ; number += 1) {
    const candidate = t('factory.numberedName', { name, number });
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
}

// A new Workflow from a template or a copy of a saved one, or a blank one
// with a single step. It gets an id and a name of its own.
export function newWorkflow(workflows, source = null) {
  const value = editableWorkflow(
    source ?? { name: t('factory.newWorkflow'), steps: [] },
  );
  if (!value.steps.length) value.steps.push(newStep([]));
  value.id = newId(
    'w',
    workflows.map((workflow) => workflow.id),
  );
  value.name = uniqueName(value.name || t('factory.newWorkflow'), workflows);
  return value;
}

export const duplicateWorkflow = (workflows, workflow) =>
  newWorkflow(workflows, {
    ...copy(workflow),
    name: t('factory.copyName', { name: workflow.name }),
    triggers: [],
  });

const wholeNumber = (value, maximum) =>
  Number.isInteger(Number(value)) &&
  String(value).trim() !== '' &&
  Number(value) >= 1 &&
  Number(value) <= maximum;

function canonicalStep(step) {
  const base = {
    id: step.id,
    name: step.name,
    kind: step.kind,
    after: [...step.after],
    approval: Boolean(step.approval),
  };
  if (step.kind === 'workflow')
    return {
      ...base,
      workflow: step.workflow,
      wait: step.wait !== false,
      task: step.task,
    };
  const limits = normalizeLimits(step.limits);
  return {
    ...base,
    source: step.source,
    agent: step.agent,
    model: step.model,
    effort: step.effort,
    task: step.task,
    returns_to: step.returns_to,
    rounds:
      !step.returns_to && !wholeNumber(step.rounds, 20)
        ? DEFAULT_ROUNDS
        : Number(step.rounds),
    ...(Object.keys(limits).length ? { limits } : {}),
  };
}

// The server's shape of a Workflow, in a fixed key order for comparison.
export function canonicalWorkflow(workflow) {
  const limits = normalizeLimits(workflow.limits);
  return {
    id: workflow.id,
    name: workflow.name,
    description: workflow.description ?? '',
    enabled: Boolean(workflow.enabled),
    project_id: workflow.project_id ?? '',
    final_approval: Boolean(workflow.final_approval),
    ...(Object.keys(limits).length ? { limits } : {}),
    steps: (workflow.steps ?? []).map(canonicalStep),
    triggers: (workflow.triggers ?? []).map((trigger) => ({
      ...editableTrigger(trigger),
      schedule: trigger.type === 'schedule' ? trigger.schedule.trim() : '',
    })),
  };
}

// The server trims names. A save that changed nothing else keeps the draft's
// spelling, so a name typed with a trailing space keeps it while typing on.
export function sameExceptNameSpacing(submitted, saved) {
  const trimmed = (workflow) => {
    const value = canonicalWorkflow(editableWorkflow(workflow));
    return JSON.stringify({
      ...value,
      name: value.name.trim(),
      project_id: value.project_id.trim(),
      steps: value.steps.map((step) => ({
        ...step,
        name: step.name.trim(),
        ...(step.kind === 'agent'
          ? {
              agent: step.agent.trim(),
              model: step.model.trim(),
              effort: step.effort.trim(),
            }
          : { workflow: step.workflow.trim() }),
      })),
      triggers: value.triggers.map((trigger) => ({
        ...trigger,
        title: trigger.title.trim(),
        project: trigger.project.trim(),
      })),
    });
  };
  return trimmed(submitted) === trimmed(saved);
}

// The first problem the server would refuse, or ''. Agent, Model and Project
// availability stays with the server, which names the step.
export function workflowProblem(workflow, others = []) {
  const name = workflow.name.trim();
  if (!name) return t('factory.invalid.workflowName');
  if (
    others.some(
      (item) =>
        item.id !== workflow.id &&
        item.name.trim().toLowerCase() === name.toLowerCase(),
    )
  )
    return t('factory.invalid.workflowNameTaken', { name });
  if (limitsProblem(workflow.limits)) return limitsProblem(workflow.limits);
  if (!workflow.steps.length)
    return t('factory.invalid.noSteps', { workflow: name });
  const stepNames = new Set();
  for (const [position, step] of workflow.steps.entries()) {
    const label = step.name.trim();
    if (!label)
      return t('factory.invalid.stepName', {
        workflow: name,
        position: position + 1,
      });
    if (stepNames.has(label.toLowerCase()))
      return t('factory.invalid.stepNameTaken', { step: label });
    stepNames.add(label.toLowerCase());
    if (step.kind === 'workflow') {
      if (!step.workflow)
        return t('factory.invalid.stepWorkflow', { step: label });
      continue;
    }
    if (limitsProblem(step.limits)) return limitsProblem(step.limits);
    if (!step.agent) return t('factory.invalid.stepAgent', { step: label });
    if (step.source === 'team' && !workflow.project_id)
      return t('factory.invalid.teamWithoutProject', { step: label });
    if (!step.task.trim())
      return t('factory.invalid.stepTask', { step: label });
    if (step.returns_to && !wholeNumber(step.rounds, 20))
      return t('factory.invalid.rounds', { step: label });
  }
  for (const [position, trigger] of workflow.triggers.entries())
    if (trigger.type === 'schedule' && !trigger.schedule.trim())
      return t('factory.invalid.schedule', { position: position + 1 });
  return '';
}

// Columns for the flow diagram: a step stands one column right of the
// furthest step it comes after; steps without `after` start the flow.
export function stepColumns(steps) {
  const column = new Map();
  for (const step of steps)
    column.set(
      step.id,
      Math.max(-1, ...step.after.map((id) => column.get(id) ?? -1)) + 1,
    );
  return column;
}
