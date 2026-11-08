import { normalizeLimits, limitsProblem } from './limits.js';
import { t } from '../../../../webui/src/lib/i18n.js';

// A Project setup's Workflows as the page edits them. The server validates
// and owns the format (`_workflows.normalize`); this module keeps drafts in
// that shape, gives new steps ids the server keeps, keeps references between
// steps consistent while the user reorders or removes steps, and names the
// first problem before an autosave sends a setup the server would refuse.
//
// A Workflow is a list of steps. A step runs after the steps in `after`
// (all listed before it), can wait for the user's approval, and with
// `returns_to` can send an earlier step's result back, at most `rounds` times.

export const MAX_WORKFLOWS = 30;
export const MAX_STEPS = 20;
export const DEFAULT_ROUNDS = 2;
const ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789';
const copy = (value) => JSON.parse(JSON.stringify(value));

// Ids match the server's `[a-z0-9]{1,24}`, so a saved draft keeps them and
// the diagram and Model keys stay stable while the user edits.
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

// "Workflow / Step", the location the server names in its errors.
export const stepLabel = (workflow, step) =>
  t('factory.location.step', { workflow: workflow.name, step: step.name });

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

// A new last step that comes after the current last step.
export function newStep(steps) {
  return {
    id: newId(
      's',
      steps.map((step) => step.id),
    ),
    name: uniqueStepName(steps),
    source: 'agent',
    agent: '',
    model: '',
    effort: '',
    task: '',
    after: steps.length ? [steps.at(-1).id] : [],
    approval: false,
    returns_to: '',
    rounds: DEFAULT_ROUNDS,
  };
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

function editableStep(step) {
  return {
    id: step.id ?? '',
    name: step.name ?? '',
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
  };
}

// A Workflow to add: a copy of a template or of an existing Workflow, or a
// blank one with a single step. It gets an id and name of its own.
export function newWorkflow(workflows, source = null) {
  const ids = workflows.map((workflow) => workflow.id);
  const value = source
    ? copy(source)
    : { id: '', name: t('factory.newWorkflow'), description: '', steps: [] };
  value.steps = (value.steps ?? []).map(editableStep);
  if (!value.steps.length) value.steps.push(newStep([]));
  value.id =
    value.id && /^[a-z0-9]{1,24}$/.test(value.id) && !ids.includes(value.id)
      ? value.id
      : newId('w', ids);
  value.name = uniqueName(value.name, workflows);
  value.description = value.description ?? '';
  return value;
}

export function duplicateWorkflow(workflows, workflow) {
  return newWorkflow(workflows, {
    ...copy(workflow),
    id: '',
    name: t('factory.copyName', { name: workflow.name }),
  });
}

export const editableWorkflows = (workflows) =>
  (workflows ?? []).map((workflow) => ({
    id: workflow.id ?? '',
    name: workflow.name ?? '',
    description: workflow.description ?? '',
    limits: { ...(workflow.limits ?? {}) },
    steps: (workflow.steps ?? []).map(editableStep),
    ...(workflow.sources?.length ? { sources: copy(workflow.sources) } : {}),
  }));

// The server's shape of the Workflows, in a fixed key order for comparison.
export const canonicalWorkflows = (workflows) =>
  (workflows ?? []).map((workflow) => ({
    id: workflow.id,
    name: workflow.name,
    description: workflow.description ?? '',
    limits: normalizeLimits(workflow.limits),
    steps: (workflow.steps ?? []).map((step) => ({
      ...editableStep(step),
      limits: normalizeLimits(step.limits),
      rounds:
        !step.returns_to && !wholeNumber(step.rounds, 20)
          ? DEFAULT_ROUNDS
          : Number(step.rounds),
    })),
    ...(workflow.sources?.length ? { sources: copy(workflow.sources) } : {}),
  }));

// The server trims names. A save that changed nothing else keeps the draft's
// spelling, so a name typed with a trailing space keeps it while typing on.
export function sameExceptNameSpacing(submitted, saved) {
  const trimmed = (workflows) =>
    JSON.stringify(
      canonicalWorkflows(workflows).map((workflow) => ({
        ...workflow,
        name: workflow.name.trim(),
        steps: workflow.steps.map((step) => ({
          ...step,
          name: step.name.trim(),
          agent: step.agent.trim(),
          model: step.model.trim(),
          effort: step.effort.trim(),
        })),
      })),
    );
  return trimmed(submitted) === trimmed(saved);
}

const wholeNumber = (value, maximum) =>
  Number.isInteger(Number(value)) &&
  String(value).trim() !== '' &&
  Number(value) >= 1 &&
  Number(value) <= maximum;

// The first problem the server would refuse, or ''. Agent and Model
// availability stays with the server, which names the step.
export function workflowProblem(workflows, { requireAgents = true } = {}) {
  if (!workflows.length) return t('factory.invalid.noWorkflow');
  const names = new Set();
  for (const workflow of workflows) {
    if (limitsProblem(workflow.limits)) return limitsProblem(workflow.limits);
    const name = workflow.name.trim();
    if (!name) return t('factory.invalid.workflowName');
    if (names.has(name.toLowerCase()))
      return t('factory.invalid.workflowNameTaken', { name });
    names.add(name.toLowerCase());
    if (!workflow.steps.length)
      return t('factory.invalid.noSteps', { workflow: name });
    const stepNames = new Set();
    for (const [position, step] of workflow.steps.entries()) {
      if (limitsProblem(step.limits)) return limitsProblem(step.limits);
      const label = step.name.trim();
      if (!label)
        return t('factory.invalid.stepName', {
          workflow: name,
          position: position + 1,
        });
      const location = t('factory.location.step', {
        workflow: name,
        step: label,
      });
      if (stepNames.has(label.toLowerCase()))
        return t('factory.invalid.stepNameTaken', { location });
      stepNames.add(label.toLowerCase());
      if (requireAgents && !step.agent)
        return t('factory.invalid.stepAgent', { location });
      if (!step.task.trim()) return t('factory.invalid.stepTask', { location });
      if (step.returns_to && !wholeNumber(step.rounds, 20))
        return t('factory.invalid.rounds', { location });
    }
  }
  return '';
}

export const SETUP_FIELDS = [
  'project_id',
  'enabled',
  'workflows',
  'default_workflow',
];

// A setup draft before the server answered: the first template, as
// `setup.get` offers it for a Project without setup.
export function setupDefaults(projectId, templates = []) {
  const workflow = newWorkflow([], templates[0] ?? null);
  return {
    project_id: projectId,
    enabled: true,
    workflows: [workflow],
    default_workflow: workflow.id,
  };
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
