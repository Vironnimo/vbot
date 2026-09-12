// @vitest-environment jsdom
import { afterEach, expect, vi } from 'vitest';
const { flushSync, mount, tick, unmount } = await import('svelte');

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);

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
      model: 'demo/model',
      state: 'running',
    },
    {
      id: 'prt-b',
      display_name: 'Beta',
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

function createBridge(initialProfile = profile) {
  let storedProfile = structuredClone(initialProfile);
  let autosaveParticipant;
  let invalidate;
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
        entries: [{ ...swarm, participant_count: 2 }],
        has_more: false,
      });
    if (name === 'swarms.get')
      return Promise.resolve({ swarm: structuredClone(swarm) });
    if (name === 'board.list')
      return Promise.resolve({ entries: structuredClone(swarm.discussions) });
    if (name === 'board.read')
      return Promise.resolve({ entries: [], has_more: false });
    if (name === 'swarms.events')
      return Promise.resolve({ entries: [], has_more: false });
    if (name === 'swarms.usage')
      return Promise.resolve({
        usage: {
          participant_id: args.participant_id ?? null,
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
                model: args.participant_id === 'prt-b' ? 'fallback' : 'model',
                runs: 1,
                measured_input_tokens: 30,
                measured_output_tokens: 20,
                estimated_input_tokens: 10,
                estimated_output_tokens: 5,
              },
            ],
          },
          tools: { total_calls: args.participant_id === 'prt-b' ? 0 : 4 },
        },
      });
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
      invalidate() {
        invalidate?.();
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
      subscribeRun: vi.fn(),
      unsubscribeRun: vi.fn().mockResolvedValue({}),
      onContext(callback) {
        context = callback;
        return () => {};
      },
      onInvalidation(callback) {
        invalidate = callback;
        return () => {};
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
  createBridge,
  fill,
  choose,
  render,
  fixtureState,
};

export { flushSync, tick, unmount, mount };
