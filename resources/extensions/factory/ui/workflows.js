import { t } from '../../../../webui/src/lib/i18n.js';

// A Project setup's Workflows as the page edits them. The server validates
// and owns the format (`_workflows.normalize`); this module keeps drafts in
// that shape, gives new parts ids the server keeps, and names the first
// problem before an autosave sends a setup the server would refuse.

export const MAX_WORKFLOWS = 30;
export const MAX_STAGES = 20;
export const MAX_LANES = 10;
const ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789';
const copy = (value) => JSON.parse(JSON.stringify(value));

// Ids match the server's `[a-z0-9]{1,24}`, so a saved draft keeps them and
// lane keys of the preview stay stable while the user edits.
export function newId(prefix, taken = []) {
  const used = new Set(taken);
  for (;;) {
    const bytes = crypto.getRandomValues(new Uint8Array(7));
    const id = prefix + [...bytes].map((byte) => ALPHABET[byte % 36]).join('');
    if (!used.has(id)) return id;
  }
}

export const blankLane = () => ({
  source: 'agent',
  agent: '',
  model: '',
  effort: '',
});

export const laneKey = (workflow, stage, index) =>
  `${workflow.id}/${stage.id}/${index}`;

// "Workflow / Stage / lane n", the location the server names in its errors.
export function laneLabel(workflow, stage, index) {
  const label = t('factory.location.stage', {
    workflow: workflow.name,
    stage: stage.name,
  });
  return stage.lanes.length > 1
    ? t('factory.location.lane', { location: label, lane: index + 1 })
    : label;
}

export const workStages = (stages, before = stages.length) =>
  stages.slice(0, before).filter((stage) => stage.kind === 'work');

// The last stage that changes the repository integrates after acceptance.
export function integratorIndex(stages) {
  let found = -1;
  stages.forEach((stage, index) => {
    if (stage.kind === 'work' && stage.output === 'changes') found = index;
  });
  return found;
}

export const changesRepository = (workflow) =>
  integratorIndex(workflow?.stages ?? []) >= 0;

export function newStage(kind, stages) {
  const id = newId(
    's',
    stages.map((stage) => stage.id),
  );
  const earlier = workStages(stages);
  return {
    id,
    name: kind === 'work' ? t('factory.newWorkStage') : t('factory.newReview'),
    kind,
    output: 'changes',
    target: earlier.at(-1)?.id ?? '',
    rounds: 3,
    instructions: '',
    lanes: [blankLane()],
    approval: false,
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

// A Workflow to add: a copy of a template or of an existing Workflow, or a
// blank one with a single work stage. It gets an id and name of its own.
export function newWorkflow(workflows, source = null) {
  const ids = workflows.map((workflow) => workflow.id);
  const value = source
    ? copy(source)
    : {
        id: '',
        name: t('factory.newWorkflow'),
        description: '',
        stages: [],
      };
  if (!value.stages.length) value.stages.push(newStage('work', []));
  value.id =
    value.id && /^[a-z0-9]{1,24}$/.test(value.id) && !ids.includes(value.id)
      ? value.id
      : newId('w', ids);
  value.name = uniqueName(value.name, workflows);
  value.stages = value.stages.map(editableStage);
  return value;
}

export function duplicateWorkflow(workflows, workflow) {
  return newWorkflow(workflows, {
    ...copy(workflow),
    id: '',
    name: t('factory.copyName', { name: workflow.name }),
  });
}

// Every stage in the draft holds the fields of both kinds, so switching the
// kind back and forth keeps what the user chose; `canonicalWorkflows` sends
// only those of the current kind.
function editableStage(stage) {
  return {
    output: 'changes',
    target: '',
    rounds: 3,
    instructions: '',
    approval: false,
    ...stage,
    lanes: (stage.lanes?.length ? stage.lanes : [blankLane()]).map((lane) => ({
      ...blankLane(),
      ...lane,
    })),
  };
}

export const editableWorkflows = (workflows) =>
  (workflows ?? []).map((workflow) => ({
    id: workflow.id ?? '',
    name: workflow.name ?? '',
    description: workflow.description ?? '',
    stages: (workflow.stages ?? []).map(editableStage),
  }));

function canonicalStage(stage) {
  const value = {
    id: stage.id,
    name: stage.name,
    kind: stage.kind,
    instructions: stage.instructions ?? '',
    lanes: stage.lanes.map((lane) => ({
      source: lane.source === 'team' ? 'team' : 'agent',
      agent: lane.agent ?? '',
      model: lane.model ?? '',
      effort: lane.effort ?? '',
    })),
    approval: Boolean(stage.approval),
  };
  if (stage.kind === 'work')
    value.output = stage.output === 'document' ? 'document' : 'changes';
  else {
    value.target = stage.target ?? '';
    value.rounds = Number(stage.rounds);
  }
  return value;
}

// The server's shape of the Workflows, in a fixed key order for comparison.
export const canonicalWorkflows = (workflows) =>
  (workflows ?? []).map((workflow) => ({
    id: workflow.id,
    name: workflow.name,
    description: workflow.description ?? '',
    stages: (workflow.stages ?? []).map(canonicalStage),
  }));

// The server trims names. A save that changed nothing else keeps the draft's
// spelling, so a name typed with a trailing space keeps it while typing on.
export function sameExceptNameSpacing(submitted, saved) {
  const trimmed = (workflows) =>
    JSON.stringify(
      canonicalWorkflows(workflows).map((workflow) => ({
        ...workflow,
        name: workflow.name.trim(),
        stages: workflow.stages.map((stage) => ({
          ...stage,
          name: stage.name.trim(),
          lanes: stage.lanes.map((lane) => ({
            ...lane,
            agent: lane.agent.trim(),
            model: lane.model.trim(),
            effort: lane.effort.trim(),
          })),
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
// availability stays with the server, which names the lane.
export function workflowProblem(workflows) {
  if (!workflows.length) return t('factory.invalid.noWorkflow');
  const names = new Set();
  for (const workflow of workflows) {
    const name = workflow.name.trim();
    if (!name) return t('factory.invalid.workflowName');
    if (names.has(name.toLowerCase()))
      return t('factory.invalid.workflowNameTaken', { name });
    names.add(name.toLowerCase());
    if (!workflow.stages.length)
      return t('factory.invalid.noStages', { workflow: name });
    for (const [position, stage] of workflow.stages.entries()) {
      if (!stage.name.trim())
        return t('factory.invalid.stageName', {
          workflow: name,
          position: position + 1,
        });
      const location = t('factory.location.stage', {
        workflow: name,
        stage: stage.name.trim(),
      });
      if (stage.kind === 'review') {
        const earlier = workStages(workflow.stages, position);
        if (!earlier.some((item) => item.id === stage.target))
          return t('factory.invalid.reviewTarget', { location });
        if (!wholeNumber(stage.rounds, 20))
          return t('factory.invalid.rounds', { location });
      }
      for (const [index, lane] of stage.lanes.entries())
        if (!lane.agent)
          return t('factory.invalid.laneAgent', {
            location: laneLabel(workflow, stage, index),
          });
    }
    const integrator = integratorIndex(workflow.stages);
    if (integrator >= 0 && workflow.stages[integrator].lanes.length !== 1)
      return t('factory.invalid.integratorLanes', {
        location: t('factory.location.stage', {
          workflow: name,
          stage: workflow.stages[integrator].name.trim(),
        }),
      });
  }
  return '';
}

export const turnBudgetValid = (value) => wholeNumber(value, 1000);

export const SETUP_FIELDS = [
  'project_id',
  'enabled',
  'workflows',
  'default_workflow',
  'turn_budget',
  'base_branch',
  'integration_check',
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
    turn_budget: 30,
    base_branch: 'main',
    integration_check: 'merged',
  };
}
