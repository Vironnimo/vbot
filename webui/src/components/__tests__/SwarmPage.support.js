// @vitest-environment jsdom
import { afterEach, expect, vi } from 'vitest';
const { flushSync, mount, tick, unmount } = await import('svelte');

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);

const { registerCatalog } = await import('../../lib/i18n.js');
const { default: swarmCatalog } =
  await import('../../../../resources/extensions/swarm/ui/i18n.js');
registerCatalog(swarmCatalog);
const { default: SwarmPage } =
  await import('../../../../resources/extensions/swarm/ui/SwarmPage.svelte');

let mounted;

const delivery = {
  main: { mode: 'all', wake_idle: true },
  discussion: { mode: 'all', wake_idle: true },
  ping: { mode: 'all', wake_idle: true },
  coalesce_ms: 250,
  batch_messages: 20,
  batch_chars: 24000,
};

const profile = {
  id: 'prf-a',
  revision: 1,
  schema_version: 1,
  name: 'Research',
  slug: 'research',
  participants: [{ model: 'demo/model', count: 2 }],
  working_directory: { kind: 'directory', path: 'C:/work' },
  tool_access: { mode: 'selected', allowed: [] },
  tools: {},
  allowed_skills: ['*'],
  instructions: '',
  prompt_blocks: ['core:tools', 'core:skills'],
  reminders: { delivery: true, resume: true },
  delivery,
};

const swarm = {
  id: 'swr-a',
  state: 'running',
  prompt: 'Investigate',
  profile_snapshot: { name: 'Research' },
  goal_post_id: 'pst-goal',
  effective_configuration: { cwd: 'C:/work' },
  main_discussion_id: 'dsc-main',
  settings_revision: 3,
  delivery,
  discussions: [
    { id: 'dsc-main', title: 'Main discussion' },
    { id: 'dsc-findings', title: 'Findings' },
  ],
  participants: [
    {
      id: 'prt-a',
      display_name: 'Alpha',
      discussion_ids: ['dsc-main'],
      model: 'demo/model',
      state: 'running',
    },
    {
      id: 'prt-b',
      display_name: 'Beta',
      discussion_ids: ['dsc-main', 'dsc-findings'],
      model: 'demo/fallback',
      state: 'idle',
    },
  ],
};

function button(text) {
  return [...document.querySelectorAll('button')].find((item) =>
    (item.getAttribute('aria-label') || item.textContent).includes(text),
  );
}

// The saved-profile editor's manual Save action (shared SaveButton), whose
// label follows the draft state: Save, Saving… or Saved.
function saveButton() {
  return document.querySelector('.swarm-profile-editor .save-button');
}

// `detail` replaces the Swarm fixture for every Swarm read of this bridge.
function createBridge(initialProfile = profile, detail = swarm) {
  let storedProfile = structuredClone(initialProfile);
  let autosaveParticipant;
  const invalidationListeners = new Set();
  const runListeners = new Set();
  const operation = vi.fn((name, args) => {
    if (name === 'profiles.save') {
      storedProfile = {
        ...structuredClone(args.profile),
        id: args.profile.id ?? 'prf-new',
        revision: (args.expected_revision ?? 0) + 1,
        slug: args.profile.slug ?? 'generated-shortcut',
      };
      return Promise.resolve({ profile: structuredClone(storedProfile) });
    }
    if (name === 'catalog')
      return Promise.resolve({
        catalog: {
          prompt_defaults: {
            instructions: 'test-owned editable default',
            prompt_blocks: ['core:tools', 'core:skills'],
            reminders: {
              delivery: true,
              wake: true,
              resume: true,
            },
          },
          prompt_blocks: [
            { id: 'core:runtime', text: 'test-owned runtime block' },
            { id: 'core:tools', text: 'test-owned tool style' },
            { id: 'core:skills', text: 'test-owned skills' },
            { id: 'core:working_project' },
            { id: 'extension:new', text: 'test-owned new extension' },
          ],
          reminder_texts: {
            delivery: 'test-owned delivery guidance',
            wake: 'test-owned wake guidance',
            resume: 'test-owned resume guidance',
          },
          models: [
            {
              id: 'demo/model',
              name: 'Demo',
              context_window: 128000,
              capabilities: {
                tools: true,
                reasoning: {
                  supported: true,
                  control: 'levels',
                  levels: ['low', 'high'],
                },
              },
            },
            {
              id: 'demo/plain',
              name: 'Plain',
              context_window: 128000,
              capabilities: { tools: true, reasoning: { supported: false } },
            },
          ],
          tools: [
            { name: 'read' },
            { name: 'write' },
            { name: 'browser', requires_opt_in: true },
          ],
          projects: [{ id: 'project-a', name: 'Project A', cwd: 'C:/project' }],
        },
      });
    if (name === 'profiles.list')
      return Promise.resolve({
        entries: [structuredClone(storedProfile)],
        has_more: false,
      });
    if (name === 'swarms.list')
      return Promise.resolve({
        entries: [{ ...detail, participant_count: detail.participants.length }],
        has_more: false,
      });
    if (name === 'swarms.get')
      return Promise.resolve({ swarm: structuredClone(detail) });
    if (name === 'board.list')
      return Promise.resolve({ entries: structuredClone(detail.discussions) });
    if (name === 'board.read')
      return Promise.resolve({ entries: [], has_more: false });
    if (name === 'swarms.events')
      return Promise.resolve({ entries: [], has_more: false });
    if (name === 'swarms.usage') {
      const report = (participantId) => ({
        participant_id: participantId ?? null,
        participant_count: 2,
        usage: {
          totals: {
            measured_input_tokens: 30,
            measured_output_tokens: 20,
            estimated_input_tokens: 10,
            estimated_output_tokens: 5,
          },
          models: [
            {
              provider: 'demo',
              model: participantId === 'prt-b' ? 'fallback' : 'model',
              runs: 1,
              measured_input_tokens: 30,
              measured_output_tokens: 20,
              estimated_input_tokens: 10,
              estimated_output_tokens: 5,
            },
          ],
        },
        tools: { total_calls: participantId === 'prt-b' ? 0 : 4 },
      });
      return Promise.resolve({
        usage: {
          ...report(args.participant_id),
          participants: detail.participants.map((participant) =>
            report(participant.id),
          ),
        },
      });
    }
    return Promise.resolve({ profile: structuredClone(profile) });
  });
  let context;
  return {
    operation,
    bridge: {
      operation,
      registerAutosave: vi.fn((participant) => {
        autosaveParticipant = participant;
        return () => (autosaveParticipant = null);
      }),
      notifyAutosave: vi.fn(),
      toast: vi.fn(),
      get autosave() {
        return autosaveParticipant;
      },
      // `change` is the Extension's `{resource, ids, revision}`, if any.
      invalidate(change = null) {
        for (const listener of invalidationListeners)
          listener({ reason: null, change });
      },
      openLink: (url) => operation('link.open', { url }),
      openMedia: (url) => operation('media.open', { url }),
      replaceRoute: vi.fn(),
      readHistory: vi.fn(() =>
        Promise.resolve({
          status: 'completed',
          messages: [
            {
              id: 'msg-link',
              role: 'assistant',
              content: 'See https://example.test',
              timestamp: '2026-09-08T09:00:00+00:00',
            },
          ],
        }),
      ),
      cancelToolCall: vi.fn().mockResolvedValue({ ok: true }),
      subscribeRun: vi.fn(),
      unsubscribeRun: vi.fn().mockResolvedValue({}),
      onContext(callback) {
        context = callback;
        return () => {};
      },
      onInvalidation(callback) {
        invalidationListeners.add(callback);
        return () => invalidationListeners.delete(callback);
      },
      onRunEvent(callback) {
        runListeners.add(callback);
        return () => runListeners.delete(callback);
      },
      emitRun(id, event) {
        for (const listener of runListeners) listener(id, event);
      },
      dispose: vi.fn(),
      updateContext(next) {
        context(next);
      },
      show() {
        context({ route: '' });
      },
    },
  };
}

// Replaces the named bridge operations; `fallback()` returns the fixture reply.
function overrideOperations(operation, handlers) {
  const original = operation.getMockImplementation();
  operation.mockImplementation((name, args) => {
    if (!Object.hasOwn(handlers, name)) return original(name, args);
    try {
      return Promise.resolve(handlers[name](args, () => original(name, args)));
    } catch (error) {
      return Promise.reject(error);
    }
  });
}

// The Swarm fixture reduced to Alpha, attached to an active lifecycle Run.
function swarmWithRun(runId, fields = {}) {
  return {
    ...structuredClone(swarm),
    participants: [
      {
        ...structuredClone(swarm.participants[0]),
        lifecycle_run_id: runId,
        run_active: true,
        ...fields,
      },
    ],
  };
}

function callsTo(operation, name) {
  return operation.mock.calls.filter(([called]) => called === name);
}

async function settle(ms = 0) {
  await new Promise((resolve) => setTimeout(resolve, ms));
  await tick();
}

function fill(id, value) {
  const input = document.getElementById(id);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

async function choose(id, text) {
  document.getElementById(id).click();
  await tick();
  const option = [...document.querySelectorAll('[role="option"]')].find((el) =>
    el.textContent.trim().startsWith(text),
  );
  expect(option).toBeDefined();
  option.click();
  await tick();
}

async function render(bridge) {
  mounted = mount(SwarmPage, {
    target: document.body,
    props: { bridgeClient: bridge },
  });
  flushSync();
  bridge.show();
  await new Promise((resolve) => setTimeout(resolve));
  await tick();
  flushSync();
}

// Opens the fixture Swarm, whose goal "Investigate" names it in the Run list.
async function openSwarm(bridge) {
  await render(bridge);
  button('Investigate').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.swarm-head')).not.toBeNull(),
  );
}

// Opens a participant's Activity from the Swarm roster.
async function openParticipant(bridge, name = 'Alpha') {
  await render(bridge);
  button('Investigate').click();
  await vi.waitFor(() => expect(button(name)).toBeDefined());
  button(name).click();
}

function historyText() {
  return document.querySelector('.history')?.textContent ?? '';
}

afterEach(async () => {
  if (mounted) mounted = await unmount(mounted);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const fixtureState = {
  get mounted() {
    return mounted;
  },
  set mounted(value) {
    mounted = value;
  },
};

export {
  SwarmPage,
  profile,
  swarm,
  button,
  saveButton,
  createBridge,
  overrideOperations,
  swarmWithRun,
  callsTo,
  settle,
  fill,
  choose,
  render,
  openSwarm,
  openParticipant,
  historyText,
  fixtureState,
};

export { flushSync, tick, unmount, mount };
