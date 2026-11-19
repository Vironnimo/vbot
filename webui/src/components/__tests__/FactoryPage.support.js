// @vitest-environment jsdom
import { afterEach, expect, vi } from 'vitest';
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { mount, unmount, tick, flushSync } = await import('svelte');
const { registerCatalog, t } = await import('../../lib/i18n.js');
const { default: catalog } =
  await import('../../../../resources/extensions/factory/ui/i18n.js');
registerCatalog(catalog);
const { default: FactoryPage } =
  await import('../../../../resources/extensions/factory/ui/FactoryPage.svelte');
let mounted;

export const step = (id, name, extra = {}) => ({
  id,
  name,
  kind: 'agent',
  source: 'team',
  agent: 'builder',
  model: '',
  effort: '',
  task: `test-owned task for ${name}`,
  after: [],
  approval: false,
  returns_to: '',
  rounds: 2,
  ...extra,
});

export const workflow = (id, name, steps, extra = {}) => ({
  id,
  revision: 1,
  name,
  description: `test-owned ${name} workflow`,
  enabled: true,
  project_id: '',
  final_approval: false,
  steps,
  triggers: [],
  ...extra,
});

export const implementWorkflow = workflow(
  'implement',
  'Implement',
  [
    step('implement', 'Implement', { model: 'demo/worker' }),
    step('review', 'Review', {
      agent: 'critic',
      model: 'demo/reviewer',
      after: ['implement'],
      returns_to: 'implement',
    }),
    step('integrate', 'Integrate', { after: ['review'], approval: true }),
  ],
  { project_id: 'project-a' },
);

// A global Workflow without a Project of its own.
export const triageWorkflow = workflow('triage', 'Triage', [
  step('triage', 'Triage', { source: 'agent', agent: 'planner' }),
]);

export const templates = [
  workflow(
    'plan',
    'Plan',
    [
      step('plan', 'Plan', {
        source: 'agent',
        agent: '',
        task: 'test-owned plan task',
      }),
      step('review', 'Review', {
        source: 'agent',
        agent: '',
        after: ['plan'],
        returns_to: 'plan',
      }),
    ],
    { revision: undefined, final_approval: true },
  ),
];

// An Agent's own Model: Drafter has none, Critic uses the reviewer Model.
const agentModel = (agent) =>
  ({ drafter: '', critic: 'demo/reviewer' })[agent] ?? 'demo/worker';

// The server's effective Models, keyed `<workflow>/<step>`.
export function effectiveModels(workflows = []) {
  return Object.fromEntries(
    workflows.flatMap((workflow) =>
      workflow.steps.map((entry) => [
        `${workflow.id}/${entry.id}`,
        entry.model || agentModel(entry.agent),
      ]),
    ),
  );
}

export function card(id, state = 'queued', extra = {}) {
  return {
    id,
    title: `Title ${id}`,
    description: `Objective ${id}`,
    project_id: 'project-a',
    workflow_id: 'implement',
    state,
    origin: 'operator',
    waiting_reason: '',
    position: 1,
    revision: 1,
    group_id: id,
    runtime: {},
    created_at: '2026-11-07T09:00:00Z',
    ...extra,
  };
}

// A started Card as the Board projects it: its Workflow snapshot and each
// step's state, by default pending.
export function startedCard(id, state, reason, flow, steps = {}, extra = {}) {
  const states = Object.fromEntries(
    flow.steps.map((entry) => [
      entry.id,
      { status: 'pending', ...(steps[entry.id] ?? {}) },
    ]),
  );
  return card(id, state, {
    waiting_reason: reason,
    workflow_id: flow.id,
    workflow: {
      id: flow.id,
      name: flow.name,
      final_approval: flow.final_approval,
      steps: flow.steps,
    },
    steps: states,
    runtime: { workflow: flow, steps: states },
    ...extra,
  });
}

const clone = (value) => JSON.parse(JSON.stringify(value));
// A saved Workflow as the server lists it, with each trigger's status.
const withStatus = (saved) => ({
  ...clone(saved),
  triggers: saved.triggers.map((trigger) => ({
    ...clone(trigger),
    status: {
      status: trigger.enabled ? 'active' : 'paused',
      next_fire_at: trigger.type === 'schedule' ? '2026-11-09T09:00:00Z' : null,
      last_fired_at: null,
      last_outcome: null,
      last_error: null,
      webhook:
        trigger.type === 'webhook'
          ? {
              url: `https://vbot.test/hooks/${saved.id}/${trigger.id}`,
              path: `/hooks/${saved.id}/${trigger.id}`,
            }
          : null,
    },
  })),
});
export function createBridge(initialCards = [card('fct-a')]) {
  const state = {
    cards: clone(initialCards),
    workflows: [clone(implementWorkflow), clone(triageWorkflow)],
    secrets: {},
    details: {},
    handlers: {},
  };
  const contexts = new Set();
  const invalidations = new Set();
  const layers = [];
  let autosave;
  let context = null;
  const conflict = () =>
    Object.assign(new Error('test-owned conflict'), {
      code: 'revision_conflict',
    });
  const operation = vi.fn(async (name, args) => {
    if (state.handlers[name]) return state.handlers[name](args);
    const current = state.cards.find((entry) => entry.id === args.id);
    if (name === 'catalog')
      return {
        catalog: {
          permission_profiles: ['full_access', 'restricted', 'Team limit'],
          projects: ['a', 'b'].map((id) => ({
            id: `project-${id}`,
            name: `Project ${id.toUpperCase()}`,
            cwd: `C:/project-${id}`,
            pending_factory_agents:
              state.pendingAgents?.[`project-${id}`] ?? [],
            agent_profiles: [
              { id: 'builder', name: 'Builder', source: 'agents' },
              { id: 'critic', name: 'Critic', source: 'agents' },
            ],
          })),
          agents: [
            { id: 'planner', name: 'Planner', description: 'Plans work' },
            { id: 'drafter', name: 'Drafter', description: 'Has no Model' },
          ],
          models: ['worker', 'reviewer'].map((id) => ({
            id: `demo/${id}`,
            name: `Demo ${id}`,
            context_window: 128000,
            capabilities: {
              tools: true,
              reasoning: {
                supported: true,
                control: 'levels',
                levels: ['low', 'high'],
              },
            },
          })),
        },
        templates: clone(templates),
      };
    if (name === 'conversation.get') return { topic: null, posts: [] };
    if (name === 'work.list') return { work: [], next_after: '' };
    if (name === 'board.get')
      return {
        cards: clone(
          state.cards.filter(
            (entry) => !args.project_id || entry.project_id === args.project_id,
          ),
        ),
        wip: {
          running: state.cards.filter((entry) => entry.state === 'working')
            .length,
          limit: 2,
        },
        revision: 1,
      };
    if (name === 'workflow.list')
      return { workflows: state.workflows.map(withStatus) };
    if (name === 'workflow.preview')
      return { effective_models: effectiveModels([args.workflow]) };
    if (name === 'workflow.get') {
      const saved = state.workflows.find((entry) => entry.id === args.id);
      if (!saved)
        throw Object.assign(new Error('test-owned missing Workflow'), {
          code: 'workflow_not_found',
        });
      return {
        workflow: withStatus(saved),
        effective_models: effectiveModels([saved]),
      };
    }
    if (name === 'workflow.save') {
      const previous = state.workflows.find(
        (entry) => entry.id === args.workflow.id,
      );
      if ((args.expected_revision ?? 0) !== (previous?.revision ?? 0))
        throw conflict();
      const next = {
        ...clone(args.workflow),
        revision: (previous?.revision ?? 0) + 1,
      };
      state.workflows = previous
        ? state.workflows.map((entry) => (entry === previous ? next : entry))
        : [...state.workflows, next];
      return {
        workflow: withStatus(next),
        effective_models: effectiveModels([next]),
      };
    }
    if (name === 'workflow.delete') {
      state.workflows = state.workflows.filter((entry) => entry.id !== args.id);
      return { deleted: true, id: args.id };
    }
    if (name === 'workflow.secret') {
      const key = `${args.id}/${args.trigger}`;
      if (args.rotate || !state.secrets[key])
        state.secrets[key] =
          `test-owned-secret-${Object.keys(state.secrets).length + 1}`;
      return { secret: state.secrets[key] };
    }
    if (name === 'card.get')
      return {
        card: clone(current),
        events: [],
        outputs: [],
        comments: [],
        participants: [],
        runs: [],
        approvals: [],
        usage: {
          activity: {
            totals: {
              calls: 3,
              input_tokens: 100,
              output_tokens: 50,
              cost_usd: 0.125,
            },
            tool_calls: 2,
          },
        },
        ...clone(state.details[args.id] ?? {}),
      };
    if (name === 'card.create') {
      const created = card('fct-new', 'queued', {
        ...args,
        position: state.cards.length + 1,
      });
      state.cards.push(created);
      return { card: clone(created) };
    }
    if (name === 'card.update') {
      if (current.revision !== args.expected_revision) throw conflict();
      for (const field of ['title', 'description'])
        if (field in args) current[field] = args[field];
      current.revision += 1;
      return { card: clone(current) };
    }
    if (name === 'card.limits') {
      if (current.revision !== args.expected_revision) throw conflict();
      if ('card_limits' in args)
        current.runtime.card_limits = clone(args.card_limits);
      if ('step_limits' in args)
        current.runtime.steps[args.step].limits = clone(args.step_limits);
      current.revision += 1;
      return { card: clone(current) };
    }
    if (name === 'card.move') {
      const target = state.cards.findIndex(
        (entry) => entry.position === args.position,
      );
      state.cards.splice(state.cards.indexOf(current), 1);
      state.cards.splice(target, 0, current);
      state.cards.forEach((entry, index) => {
        entry.position = index + 1;
      });
      return { card: clone(current) };
    }
    if (name === 'card.delete') {
      state.cards = state.cards.filter((entry) => entry.id !== args.id);
      return { deleted: true };
    }
    if (name.startsWith('card.')) {
      current.state = {
        'card.accept':
          current.waiting_reason === 'finished' ? 'done' : 'working',
        'card.approve_proposal': 'queued',
        'card.send_back': 'working',
        'card.answer': 'working',
        'card.retry': 'working',
        'card.discard': 'discarded',
      }[name];
      current.waiting_reason = '';
      current.revision += 1;
      return { card: clone(current) };
    }
    throw new Error(`Unexpected test operation ${name}`);
  });
  const goTo = (route) => {
    context = { ...context, route, locale: 'en', timezone: 'UTC', theme: {} };
    for (const callback of contexts) callback(context);
    return Promise.resolve({});
  };
  const bridge = {
    operation,
    get context() {
      return context;
    },
    show: (route = '') => goTo(route),
    goTo,
    // The real route capability enters the App navigator's autosave gate.
    pushRoute: vi.fn(async (route) => {
      if (!autosave || (await autosave.flush())) await goTo(route);
      return {};
    }),
    replaceRoute: vi.fn(goTo),
    onContext(callback) {
      contexts.add(callback);
      return () => contexts.delete(callback);
    },
    onInvalidation(callback) {
      invalidations.add(callback);
      return () => invalidations.delete(callback);
    },
    invalidate(change = null) {
      for (const callback of invalidations) callback({ reason: null, change });
    },
    registerLayer(layer) {
      layers.push(layer);
      return () => {
        const index = layers.indexOf(layer);
        if (index >= 0) layers.splice(index, 1);
      };
    },
    back: () => layers.at(-1)?.close(),
    registerAutosave: vi.fn((participant) => {
      autosave = participant;
      return () => {
        if (autosave === participant) autosave = null;
      };
    }),
    get autosave() {
      return autosave;
    },
    notifyAutosave: vi.fn(),
    openSession: vi.fn().mockResolvedValue({}),
    openAgent: vi.fn().mockResolvedValue({}),
    openLink: vi.fn().mockResolvedValue({}),
    readModelFavorites: vi.fn().mockResolvedValue({ models: [] }),
    setModelFavorite: vi.fn().mockResolvedValue({ models: [] }),
    mountSession: vi.fn(() => vi.fn()),
    dispose: vi.fn(),
  };
  return { bridge, operation, state };
}

export async function settle() {
  await vi.dynamicImportSettled();
  await tick();
  flushSync();
  await Promise.resolve();
  await tick();
  flushSync();
}

export async function render(bridge, route = '/cards') {
  mounted = mount(FactoryPage, {
    target: document.body,
    props: { bridgeClient: bridge },
  });
  flushSync();
  bridge.show(route);
  await settle();
}

export function button(label, root = document) {
  return [...root.querySelectorAll('button')].find(
    (node) =>
      (node.getAttribute('aria-label') || node.textContent).trim() === label,
  );
}

export function fill(id, value) {
  const input = document.getElementById(id);
  expect(input).not.toBeNull();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  return input;
}

export async function choose(id, text) {
  document.getElementById(id).click();
  await tick();
  const option = [...document.querySelectorAll('[role="option"]')].find(
    (node) => node.textContent.trim().startsWith(text),
  );
  expect(option).toBeDefined();
  option.click();
  await tick();
}

export const calls = (operation, name) =>
  operation.mock.calls.filter(([called]) => called === name);
export const actionButtons = () =>
  [...document.querySelectorAll('.factory-card-actions button')].map((node) =>
    node.textContent.trim(),
  );
// The buttons of the steps that wait for the user, by step id.
export const stepButtons = () =>
  Object.fromEntries(
    [...document.querySelectorAll('[data-waiting-step]')].map((node) => [
      node.dataset.waitingStep,
      [...node.querySelectorAll('button')].map((item) =>
        item.textContent.trim(),
      ),
    ]),
  );
export { t, tick, flushSync };
afterEach(async () => {
  if (mounted) {
    await unmount(mounted);
    mounted = null;
  }
  document.body.innerHTML = '';
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
