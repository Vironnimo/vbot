// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    listTaskModelTargets: (taskType) =>
      rpcMock('task_model.list_targets', { task_type: taskType }),
    getTaskModelOptions: (taskType, target) =>
      rpcMock('task_model.options', { task_type: taskType, target }),
    updateTaskModelSettings: (modelTasks) =>
      rpcMock('task_model.update', { model_tasks: modelTasks }),
  }),
);

const { default: SettingsLiveVoicePanel } =
  await import('../settings/SettingsLiveVoicePanel.svelte');

const LIVE_TOOLS = ['send_message', 'end_call'];
const TOOLS = [
  { name: 'web_search', family: 'web', activation: 'configurable' },
  { name: 'memory', family: null, activation: 'memory_mode' },
  ...LIVE_TOOLS.map((name) => ({
    name,
    family: 'live',
    activation: 'configurable',
    requires_opt_in: true,
    constraints: ['live_call'],
  })),
];
const LIVE_POLICY = Object.freeze({
  mode: 'selected',
  allowed: LIVE_TOOLS,
  granted: LIVE_TOOLS,
});
const AGENTS = Object.freeze({
  'live-voice': {
    id: 'live-voice',
    name: 'Live voice',
    builtin: 'live_voice',
    tool_access: LIVE_POLICY,
    effective: {},
  },
  'live-backend': {
    id: 'live-backend',
    name: 'Live backend',
    builtin: 'live_backend',
    tool_access: LIVE_POLICY,
    effective: {},
  },
});

function serve({ sessions = [] } = {}) {
  rpcMock.mockImplementation(async (method, params) => {
    if (method === 'model.list') return { models: [] };
    if (method === 'connection.list') return { connections: [] };
    if (method === 'tool.list') return { tools: TOOLS };
    if (method === 'agent.get') return AGENTS[params.id];
    if (method === 'agent.update') return { ...AGENTS[params.id], ...params };
    if (method === 'session.list') return { sessions, next_cursor: null };
    if (method === 'task_model.list_targets') return { targets: [] };
    throw new Error(`Unexpected RPC method: ${method}`);
  });
}

describe('SettingsLiveVoicePanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  function render(props = {}) {
    mountedComponent = mount(SettingsLiveVoicePanel, {
      target: document.body,
      props: { settings: { model_tasks: {} }, ...props },
    });
    flushSync();
  }

  const calls = (method) =>
    rpcMock.mock.calls
      .filter(([name]) => name === method)
      .map(([, params]) => params);

  it('saves the Tools of the voice and backend Agents with agent.update', async () => {
    serve();
    render();
    const editors = () => [
      ...document.body.querySelectorAll('.tool-access-editor'),
    ];
    await waitForCondition(() => editors().length === 2);
    const [voiceTools, backendTools] = editors();
    const chip = (editor, name) =>
      editor.querySelector(`[data-tool-name="${name}"]`);

    // A Live Agent lists the Live call Tools but no automatic Tool.
    expect(chip(voiceTools, 'send_message')).not.toBeNull();
    expect(chip(voiceTools, 'memory')).toBeNull();
    // The backend Agent's Model is set here too.
    expect(
      document.getElementById('settings-live-backend-model'),
    ).not.toBeNull();

    chip(voiceTools, 'end_call').click();
    chip(backendTools, 'web_search').click();
    flushSync();
    document.body.querySelector('.save-status button').click();
    await waitForCondition(() => calls('agent.update').length === 2);

    expect(calls('agent.update')).toEqual([
      {
        tool_access: {
          mode: 'selected',
          allowed: ['send_message'],
          granted: ['send_message'],
        },
        id: 'live-voice',
      },
      {
        tool_access: {
          mode: 'selected',
          allowed: [...LIVE_TOOLS, 'web_search'],
          granted: LIVE_TOOLS,
        },
        id: 'live-backend',
      },
    ]);
  });

  it('lists the recent calls and opens the voice Session of one', async () => {
    serve({
      sessions: [
        { id: 'call-2', title: 'Live call · 2026-10-07 09:30' },
        { id: 'call-1', auto_title: 'Live call · 2026-10-06 18:00' },
      ],
    });
    const onOpenSession = vi.fn();
    render({ onOpenSession });
    await waitForCondition(
      () => document.body.querySelectorAll('[data-live-call]').length === 2,
    );

    expect(calls('session.list')).toEqual([
      { agent_id: 'live-voice', limit: 10 },
    ]);
    const row = document.body.querySelector('[data-live-call="call-1"]');
    expect(row.querySelector('.s-row-label').textContent).toBe(
      'Live call · 2026-10-06 18:00',
    );
    row.querySelector('button').click();
    expect(onOpenSession).toHaveBeenCalledWith('live-voice', 'call-1');
  });
});

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    if (check()) {
      return;
    }
  }
  throw new Error('Timed out waiting for condition.');
}
