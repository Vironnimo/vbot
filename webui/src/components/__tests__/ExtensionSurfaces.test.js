// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import { assistantRun, outputItem } from './timelineItems.support.js';
import { baseAgent } from './AgentsView.support.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const rpc = vi.hoisted(() => vi.fn());
vi.mock('$lib/api.js', async (original) => ({
  ...(await original()),
  ...rpcBackedApiMock(rpc),
}));

const { default: Host } =
  await import('./ContributionContextHost.support.svelte');
const { default: ChatTimelineEntry } =
  await import('../chat/ChatTimelineEntry.svelte');
const { default: ChatAssistantRun } =
  await import('../chat/ChatAssistantRun.svelte');
const { default: SessionListDrawer } =
  await import('../SessionListDrawer.svelte');
const { default: AgentEditor } = await import('../agents/AgentEditor.svelte');
const { default: ProjectsView } = await import('../ProjectsView.svelte');

let instance;
let notifyError;
const project = {
  project_id: 'demo',
  display_name: 'Demo',
  cwd: 'C:/repos/demo',
  cwd_exists: true,
  auto_load: [],
  default_agent: '',
  default_model: '',
};

async function settle() {
  for (let index = 0; index < 20; index++) {
    await Promise.resolve();
    flushSync();
  }
}

function render(component, props, entries, layout = {}) {
  instance = mount(Host, {
    target: document.body,
    props: {
      component,
      instances: [props],
      source: { entries, notifyError },
      onRegistry: (registry) =>
        registry.acceptSettings({ appearance: { ui_layout: layout } }),
    },
  });
  flushSync();
}

function action(surface, title, command, params = {}, when = {}) {
  return {
    id: `extension:example:${title}`,
    extension: 'example',
    epoch: 'one',
    kind: 'action',
    surface,
    title,
    action: { command, params },
    when,
  };
}

beforeEach(() => {
  init('en');
  document.body.innerHTML = '';
  localStorage.clear();
  notifyError = vi.fn();
  rpc.mockReset().mockImplementation(async (method) => {
    if (method === 'agent.soul.get') return { content: '', revision: null };
    if (method === 'session.list')
      return {
        sessions: [
          { id: 'one', title: 'First' },
          { id: 'two', title: 'Second' },
        ],
      };
    if (method === 'project.list') return { projects: [project] };
    if (method === 'project.show')
      return {
        project,
        scan: { team: [], report: { clean: true, findings: [] } },
      };
    if (method === 'model.list') return { models: [] };
    if (method === 'connection.list') return { connections: [] };
    if (method === 'settings.get') return { defaults: { agent: {} } };
    return {};
  });
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  vi.restoreAllMocks();
});

it.each(['user', 'assistant'])(
  'binds %s message actions to that message and lets layouts hide native content',
  async (role) => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const entry = action(
      'chat.message.actions',
      'Copy this message',
      'message.copy',
      {},
      {
        message_id: 'message-one',
        message_role: role,
        session_id: 'session-one',
      },
    );
    const uiContext = { agent_id: 'alpha', session_id: 'session-one' };
    render(
      role === 'user' ? ChatTimelineEntry : ChatAssistantRun,
      role === 'user'
        ? {
            uiContext,
            item: {
              id: 'message-one',
              type: 'message',
              message: { id: 'message-one', role, content: 'Message text' },
            },
          }
        : {
            uiContext,
            item: assistantRun('run-one', [
              outputItem('Message text', {
                id: 'phase-one',
                messageId: 'message-one',
              }),
            ]),
          },
      [entry],
      { 'chat.message': { hidden: ['core:chat.message.content'] } },
    );
    expect(document.body.textContent).not.toContain('Message text');
    const button = document.querySelector(
      'button[aria-label="Copy this message"]',
    );
    expect(button).not.toBeNull();
    button.click();
    await settle();
    expect(writeText).toHaveBeenCalledExactlyOnceWith('Message text');
    expect(notifyError).not.toHaveBeenCalled();
  },
);

it('opens only the Session of the contribution row without nesting interactive controls', async () => {
  const onSessionSelected = vi.fn();
  render(
    SessionListDrawer,
    { agentId: 'alpha', currentSessionId: 'one', onSessionSelected },
    [
      action(
        'chat.session.item',
        'Open this Session',
        'session.open',
        {},
        { session_id: 'two', session_selected: false },
      ),
      {
        id: 'extension:example:list',
        extension: 'example',
        epoch: 'one',
        kind: 'status',
        surface: 'chat.sessions',
        title: 'Custom list control',
        text: 'Custom list control',
      },
    ],
    { 'chat.sessions': { hidden: ['core:chat.sessions.new'] } },
  );
  await settle();
  expect(document.body.textContent).toContain('Custom list control');
  expect(document.querySelector('.session-drawer__new')).toBeNull();
  expect(document.querySelector('button button')).toBeNull();
  const button = document.querySelector(
    'button[aria-label="Open this Session"]',
  );
  expect(button.closest('.session-row').textContent).toContain('Second');
  button.click();
  await settle();
  expect(onSessionSelected).toHaveBeenCalledExactlyOnceWith(
    'two',
    'alpha',
    false,
  );
});

it.each(['agent', 'project'])(
  'extends the %s editor through native sections and allowlisted focus without saving',
  async (editor) => {
    const field = editor === 'agent' ? 'description' : 'display_name';
    const surface = `${editor}.editor.sections`;
    const when =
      editor === 'agent'
        ? { agent_id: 'alpha', editor_mode: 'edit' }
        : { project_id: 'demo', editor_mode: 'edit' };
    render(
      editor === 'agent' ? AgentEditor : ProjectsView,
      editor === 'agent' ? { agent: baseAgent() } : {},
      [
        action(surface, 'Focus editor field', 'editor.focus', { field }, when),
        action(
          surface,
          'Invalid editor field',
          'editor.focus',
          { field: '#arbitrary-selector' },
          when,
        ),
      ],
      {
        [surface]: {
          hidden: [
            `core:${editor}.editor.${editor === 'agent' ? 'details' : 'team'}`,
          ],
        },
      },
    );
    await settle();
    expect(
      document.querySelector(
        editor === 'agent'
          ? '#agent-detail-panel-details'
          : '#project-part-team',
      ),
    ).toBeNull();
    document.querySelector('button[aria-label="Focus editor field"]').click();
    await settle();
    expect(document.activeElement.id).toBe(
      editor === 'agent' ? 'agent-description' : 'project-edit-name',
    );
    document.querySelector('button[aria-label="Invalid editor field"]').click();
    await settle();
    expect(notifyError).toHaveBeenCalledTimes(1);
    expect(
      rpc.mock.calls.some(([method]) =>
        ['agent.update', 'project.set'].includes(method),
      ),
    ).toBe(false);
  },
);
