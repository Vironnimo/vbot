// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

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
      state: 'waiting',
    },
  ],
};

function button(text) {
  return [...document.querySelectorAll('button')].find((item) =>
    item.textContent.includes(text),
  );
}

function createBridge() {
  const operation = vi.fn((name) => {
    if (name === 'catalog')
      return Promise.resolve({ catalog: { models: ['demo/model'] } });
    if (name === 'profiles.list')
      return Promise.resolve({
        entries: [structuredClone(profile)],
        has_more: false,
      });
    if (name === 'swarms.list')
      return Promise.resolve({
        entries: [{ ...swarm, participant_count: 2, done_count: 0 }],
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
          participant_id:
            'participant_id' in arguments ? arguments.participant_id : null,
          participant_count: 2,
          usage: {
            measured_input_tokens: 30,
            measured_output_tokens: 20,
            estimated_input_tokens: 10,
            estimated_output_tokens: 5,
            models: [
              {
                provider: 'demo',
                model:
                  arguments.participant_id === 'prt-b' ? 'fallback' : 'model',
                runs: 1,
                measured_input_tokens: 30,
                measured_output_tokens: 20,
                estimated_input_tokens: 10,
                estimated_output_tokens: 5,
              },
            ],
          },
          tools: { total_calls: 4 },
        },
      });
    return Promise.resolve({ profile: structuredClone(profile) });
  });
  let context;
  return {
    operation,
    bridge: {
      operation,
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
      unsubscribeRun: vi.fn(),
      onContext(callback) {
        context = callback;
        return () => {};
      },
      onInvalidation() {
        return () => {};
      },
      onRunEvent() {
        return () => {};
      },
      dispose: vi.fn(),
      show() {
        context({ route: '' });
      },
    },
  };
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
});

describe('SwarmPage', () => {
  it('leaves loading after the catalog and retained lists resolve', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    expect(document.body.textContent).toContain('New profile');
    expect(document.body.textContent).toContain('Retained Swarms');
    expect(operation).toHaveBeenCalledWith('profiles.list', { limit: 100 });
  });

  it('saves a new profile through the bridge', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('New profile').click();
    await tick();
    flushSync();
    const inputs = document.querySelectorAll('input');
    inputs[0].value = 'New profile';
    inputs[0].dispatchEvent(new Event('input', { bubbles: true }));
    inputs[1].value = 'new-profile';
    inputs[1].dispatchEvent(new Event('input', { bubbles: true }));
    const model = document.querySelectorAll('select')[0];
    model.value = 'demo/model';
    model.dispatchEvent(new Event('change', { bubbles: true }));
    const directory = document.querySelector('input[placeholder]');
    directory.value = 'C:/work';
    directory.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    button('Save profile').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await tick();
    expect(document.body.textContent).not.toContain('Enter a name');
    expect(operation).toHaveBeenCalledWith(
      'profiles.save',
      expect.objectContaining({
        expected_revision: null,
        profile: expect.objectContaining({
          name: 'New profile',
          slug: 'new-profile',
        }),
      }),
    );
  });

  it('posts a Board reply with explicit public recipients', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('swr-a').click();
    await tick();
    await tick();
    flushSync();
    button('Write post').click();
    await tick();
    const textarea = document.querySelector('textarea');
    textarea.value = 'Finding';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    const options = document.querySelectorAll('.post-options input');
    options[0].value = 'pst-parent';
    options[0].dispatchEvent(new Event('input', { bubbles: true }));
    options[1].value = 'prt-a, prt-b';
    options[1].dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    button('Post').click();
    await tick();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'board.post',
      expect.objectContaining({
        swarm_id: 'swr-a',
        discussion_id: 'dsc-main',
        text: 'Finding',
        reply_to: 'pst-parent',
        recipients: ['prt-a', 'prt-b'],
        request_id: expect.any(String),
      }),
    );
  });

  it('changes Board discussion without treating a human read as a mutation', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('swr-a').click();
    await tick();
    await tick();
    await new Promise((resolve) => setTimeout(resolve));
    const discussion = document.querySelector('#swarm-discussion');
    discussion.value = 'dsc-findings';
    discussion.dispatchEvent(new Event('change', { bubbles: true }));
    await tick();
    expect(operation).toHaveBeenCalledWith('board.read', {
      swarm_id: 'swr-a',
      discussion_id: 'dsc-findings',
      limit: 100,
    });
    expect(operation).not.toHaveBeenCalledWith('board.post', expect.anything());
  });

  it('renders canonical per-participant Model usage and tool-call totals', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('swr-a').click();
    await new Promise((resolve) => setTimeout(resolve));
    await tick();
    button('Usage').click();
    await tick();
    expect(document.body.textContent).toContain('Measured tokens');
    expect(document.body.textContent).toContain('Tool Calls');
    expect(document.body.textContent).toContain('Alpha');
    expect(document.body.textContent).toContain('demo/model');
    expect(operation).toHaveBeenCalledWith('swarms.usage', {
      swarm_id: 'swr-a',
      participant_id: 'prt-a',
    });
  });

  it('renders canonical UsageSection totals and participant Model rows', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('swr-a').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Usage').click();
    await tick();
    expect(document.body.textContent).toContain('50');
    expect(document.body.textContent).toContain('15');
    expect(document.body.textContent).toContain('4');
    expect(document.body.textContent).toContain('Alpha');
    expect(document.body.textContent).toContain('demo/model');
    expect(operation).toHaveBeenCalledWith('swarms.usage', {
      swarm_id: 'swr-a',
      participant_id: 'prt-a',
    });
  });

  it('opens Activity links through the extension bridge', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('swr-a').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Alpha').click();
    await tick();
    button('Activity').click();
    await tick();
    const link = document.querySelector('.history a[href]');
    expect(link).not.toBeNull();
    link.click();
    await tick();
    expect(operation).toHaveBeenCalledWith('link.open', {
      url: 'https://example.test/',
    });
  });

  it('offers explicit Resume beside Stop when a participant is waiting', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('swr-a').click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(button('Stop')).toBeDefined();
    button('Resume').click();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.resume',
      expect.objectContaining({ swarm_id: 'swr-a' }),
    );
  });

  it.each([
    ['cancelled', 'cancelled'],
    ['interrupted', 'interrupted'],
  ])(
    'offers Resume for an unfinished %s Swarm participant',
    async (swarmState, participantState) => {
      const { bridge, operation } = createBridge();
      const originalGet = operation.getMockImplementation();
      operation.mockImplementation((name, arguments_) => {
        if (name === 'swarms.get') {
          return Promise.resolve({
            swarm: {
              ...structuredClone(swarm),
              state: swarmState,
              participants: [
                {
                  ...structuredClone(swarm.participants[0]),
                  state: participantState,
                },
              ],
            },
          });
        }
        return originalGet(name, arguments_);
      });
      await render(bridge);
      button('swr-a').click();
      await new Promise((resolve) => setTimeout(resolve));
      expect(button('Resume')).toBeDefined();
      button('Resume').click();
      await tick();
      expect(operation).toHaveBeenCalledWith(
        'swarms.resume',
        expect.objectContaining({ swarm_id: 'swr-a' }),
      );
    },
  );

  it('does not autosave delivery changes and stops only on an explicit click', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('swr-a').click();
    await tick();
    await tick();
    flushSync();
    button('Change communication settings').click();
    flushSync();
    const select = document.querySelector('.communication select');
    select.value = 'pull';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await tick();
    expect(operation).not.toHaveBeenCalledWith(
      'swarms.settings',
      expect.anything(),
    );
    expect(document.body.textContent).toContain('Proposed changes');
    button('Apply changes').click();
    await new Promise((resolve) => setTimeout(resolve));
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.settings',
      expect.objectContaining({
        swarm_id: 'swr-a',
        expected_revision: 3,
        delivery: expect.objectContaining({
          main: expect.objectContaining({ mode: 'pull' }),
        }),
      }),
    );
    button('Stop').click();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.stop',
      expect.objectContaining({
        swarm_id: 'swr-a',
        request_id: expect.any(String),
      }),
    );
  });
});
