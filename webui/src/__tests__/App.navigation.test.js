// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import {
  baseAgent,
  createAgentsRpcMock,
} from '../components/__tests__/AgentsView.support.js';
import { AUTOSAVE_STILL_SAVING_MS } from '../lib/autosave.js';
import { t } from '../lib/i18n.js';
import * as lazyContent from '../lib/lazyContent.js';
import * as liveUiActions from '../app/liveUiActions.js';
import {
  App,
  assistantHistory,
  buttonWithText,
  cleanupAppHarness,
  createAppRpcMock,
  createSettingsRpcMock,
  createSubAgentNavigationRpcMock,
  listSessionActivityMock,
  resetAppHarness,
  returnToCurrentSessionButton,
  rpcMock,
  uploadAttachmentMock,
  selectPersonalAgent,
  selectedAgentName,
  sendConnectionReady,
  settingsPanelButton,
  subscribeServerEventsMock,
  sidebarNavButton,
  viewSessionButton,
  waitForCondition,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

const FIXTURE_EXTENSION_ROUTE = 'extension:fixture:main';
const FIXTURE_EXTENSION_PAGE = {
  extension: 'fixture',
  page: 'main',
  title: 'Fixture page',
  route: FIXTURE_EXTENSION_ROUTE,
  entry_url: '/fixture-page.html',
  epoch: 'fixture-epoch',
};
const TRANSITION_FAILURE_DIALOG =
  '[role="dialog"][aria-labelledby="autosave-transition-failure-title"]';
const STILL_SAVING_DIALOG =
  '[role="dialog"][aria-labelledby="autosave-transition-slow-title"]';

const isCurrent = (viewId) =>
  sidebarNavButton(viewId)?.getAttribute('aria-current') === 'page';
const logsShown = () => Boolean(document.querySelector('#logs-title'));
const depthInput = () =>
  document.querySelector(
    `input[aria-label="${t('settings.subagents.maxDepth')}"]`,
  );
const settingsUpdates = () =>
  rpcMock.mock.calls.filter(([method]) => method === 'settings.update');
const agentButton = (agentId) =>
  document.querySelector(
    `.agent-list [data-sortable-key="${agentId}"] button.agent-item`,
  );
const agentShown = (agentId) =>
  agentButton(agentId)?.classList.contains('active') === true;
const labelledButton = (key) =>
  document.querySelector(`button[aria-label="${t(key)}"]`);

// The Agents view's reads plus the Chat startup reads.
function agentsAppRpc(options) {
  const agentsRpc = createAgentsRpcMock(options);
  const chatRpc = createAppRpcMock();
  return (method, params) =>
    ['chat.commands', 'chat.timeline', 'chat.queue_list'].includes(method)
      ? chatRpc(method, params)
      : agentsRpc(method, params);
}

function typeInto(input, value) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

// Settings whose `settings.update` fails for the first `failures` saves.
function failingSettingsRpc(failures) {
  const settingsRpc = createSettingsRpcMock();
  let attempt = 0;
  return (method, params) =>
    method === 'settings.update' && attempt++ < failures
      ? Promise.reject(new Error('save unavailable'))
      : settingsRpc(method, params);
}

describe('App navigation', () => {
  let mountedComponent;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  function mountApp() {
    mountedComponent = mount(App, { target: document.body });
    flushSync();
  }

  async function openSettingsTools() {
    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(settingsPanelButton('tools')).toBeTruthy(),
    );
    settingsPanelButton('tools').click();
    flushSync();
    await vi.dynamicImportSettled();
    await waitForCondition(() => expect(depthInput()).toBeTruthy());
  }

  async function openSubAgentSession() {
    await waitForCondition(() => {
      expect(document.body.textContent).toContain('Inspect again');
    });
    viewSessionButton().click();
    flushSync();
    await waitForCondition(() => {
      expect(document.body.textContent).toContain('Sub-agent response');
    });
  }

  it('routes a Settings search result to shared Agent defaults', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountApp();
    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('#settings-section-appearance'),
      ).toBeTruthy(),
    );
    typeInto(document.querySelector('.settings-search-input'), 'thinking');
    Array.from(document.querySelectorAll('.settings-search-result'))
      .find((button) => button.textContent.includes(t('agents.shared.title')))
      .click();
    await waitForCondition(() => {
      expect(document.querySelector('#settings-defaults-model')).toBeTruthy();
      expect(isCurrent('agents')).toBe(true);
      expect(window.location.hash).toBe('#agents/~defaults');
    });
  });

  it('opens /btw beside a running Project Session and promotes the exact side conversation to Chat', async () => {
    const owner = 'builder@work';
    const source = { agent_id: owner, session_id: 'source' };
    const side = { agent_id: owner, session_id: 'side' };
    const parentHistory = assistantHistory('source-reply', 'Main conversation');
    const sideHistory = assistantHistory('side-reply', 'Separate answer');
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [baseAgent()],
        history: ({ session_id, include_inherited, before, after }) => {
          if (session_id === 'source')
            return {
              ...parentHistory,
              active_run: {
                ...source,
                run_id: 'working',
                status: 'running',
                events: [],
              },
            };
          if (include_inherited !== false)
            return {
              items: [...parentHistory.items, ...sideHistory.items],
              messages: [...parentHistory.messages, ...sideHistory.messages],
            };
          if (before)
            return assistantHistory('side-earlier', 'Earlier side reply');
          if (after)
            return {
              incremental: true,
              next_after: 'current',
              items: [],
              messages: [],
            };
          return {
            ...sideHistory,
            next_before: 'older',
            next_after: 'current',
            has_more: true,
          };
        },
        methods: {
          'chat.commands': () => ({
            items: [
              {
                name: 'btw',
                type: 'command',
                description: 'Open a side conversation',
                argument: 'optional',
              },
              { name: 'help', type: 'command', argument: 'none' },
              { name: 'explain', type: 'command', argument: 'required' },
            ],
          }),
          'chat.stream': ({ session_id, content }) =>
            !content.startsWith('/btw')
              ? { accepted: true, session_available: true, session_id: 'side' }
              : {
                  command_handled: true,
                  output: 'action',
                  data: {
                    navigation: {
                      kind: 'open_side_chat',
                      agent_id: 'builder',
                      project_id: 'work',
                      session_id:
                        session_id === 'source' ? 'side' : 'nested-side',
                    },
                  },
                },
          'session.get': ({ agent_id, session_id }) => ({
            session: { agent_id, id: session_id },
          }),
        },
      }),
    );
    window.history.replaceState(null, '', '#chat/builder@work/source');
    mountApp();
    await waitForCondition(() =>
      expect(labelledButton('chat.cancelRun')).toBeTruthy(),
    );
    const main = document.querySelector('.chat-view');
    const submit = (area, message) => {
      typeInto(area.querySelector('.msg-input'), message);
      flushSync();
      area.querySelector('button[type="submit"]').click();
    };
    for (const text of ['/help please', '/explain']) {
      typeInto(main.querySelector('.msg-input'), text);
      flushSync();
      expect(labelledButton('chat.queueMessage')).toBeTruthy();
    }
    typeInto(main.querySelector('.msg-input'), '/btw Side question');
    flushSync();
    expect(labelledButton('chat.sendMessage')).toBeTruthy();
    main.querySelector('button[type="submit"]').click();
    await waitForCondition(() =>
      expect(document.querySelector('.session-popup')?.textContent).toContain(
        'Separate answer',
      ),
    );
    expect(rpcMock).toHaveBeenCalledWith(
      'chat.stream',
      expect.objectContaining({ ...source, content: '/btw Side question' }),
    );
    expect(window.location.hash).toBe('#chat/builder@work/source');
    expect(main.textContent).toContain('Main conversation');
    expect(main.querySelector('.composer-stop-run')).toBeTruthy();
    const popup = document.querySelector('.session-popup');
    expect(popup.textContent).not.toContain('Main conversation');
    const messages = popup.querySelector('.messages');
    Object.defineProperties(messages, {
      scrollHeight: { configurable: true, value: 1000 },
      offsetHeight: { configurable: true, value: 500 },
    });
    messages.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
    messages.dispatchEvent(new Event('scroll'));
    await waitForCondition(() =>
      expect(popup.textContent).toContain('Earlier side reply'),
    );
    submit(popup, 'Follow up in the side conversation');
    await waitForCondition(() =>
      expect(rpcMock).toHaveBeenCalledWith(
        'chat.stream',
        expect.objectContaining({
          ...side,
          content: 'Follow up in the side conversation',
        }),
      ),
    );
    expect(rpcMock).not.toHaveBeenCalledWith('chat.cancel', expect.anything());
    await waitForCondition(() =>
      expect(rpcMock).toHaveBeenCalledWith(
        'chat.timeline',
        expect.objectContaining({
          ...side,
          after: 'current',
          include_inherited: false,
        }),
      ),
    );
    submit(popup, '/btw Nested question');
    await waitForCondition(() => expect(popup.hidden).toBe(true));
    buttonWithText(
      '.session-popup:not([hidden]) button',
      t('appChat.openFull'),
    ).click();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat/builder@work/nested-side');
      expect(document.querySelector('.session-popup')).toBeNull();
      expect(main.textContent).toContain('Main conversation');
      expect(main.textContent).toContain('Separate answer');
    });
  });

  it('starts a side draft on its first input and sends attachments and retries only to its child', async () => {
    const source = { agent_id: 'builder@work', session_id: 'source' };
    const child = {
      agent_id: 'builder',
      project_id: 'work',
      session_id: 'side',
      generation_id: 'side-gen',
    };
    const launches = [];
    const requests = [];
    let hasChild = false;
    const attachment = {
      attachment_id: 'file-1',
      filename: 'note.txt',
      media_type: 'text/plain',
    };
    uploadAttachmentMock.mockResolvedValue(attachment);
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [baseAgent()],
        history: () => ({ items: [], messages: [] }),
        methods: {
          'session.get': ({ session_id }) => ({ session: { id: session_id } }),
          'files.list': () => ({ files: ['README.md'] }),
          'session.create_side_chat': (params) => {
            launches.push(params);
            hasChild = true;
            return child;
          },
          'chat.stream': (params) => {
            if (params.content === '/btw')
              return {
                command_handled: true,
                output: 'action',
                data: {
                  navigation: hasChild
                    ? { ...child, kind: 'open_side_chat' }
                    : {
                        agent_id: 'builder',
                        project_id: 'work',
                        session_id: 'source',
                        source_generation_id: 'source-gen',
                        kind: 'open_side_chat_draft',
                      },
                },
              };
            requests.push(params);
            if (requests.length <= 2)
              throw Object.assign(new Error('Missing acknowledgement'), {
                code: 'network_error',
              });
            return { accepted: true, session_available: true };
          },
        },
      }),
    );
    window.history.replaceState(null, '', '#chat/builder@work/source');
    mountApp();
    await waitForCondition(() =>
      expect(
        document.querySelector('.chat-view textarea:enabled'),
      ).toBeTruthy(),
    );
    const main = document.querySelector('.chat-view');
    typeInto(main.querySelector('textarea'), '/btw');
    main.querySelector('button[type="submit"]').click();
    await waitForCondition(() =>
      expect(document.querySelector('.session-popup .msg-input')).toBeTruthy(),
    );
    let popup = document.querySelector('.session-popup');
    const input = popup.querySelector('textarea');
    typeInto(input, '/secret');
    popup.querySelector('button[type="submit"]').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('.modal-dialog')?.textContent ??
          document.body.textContent,
      ).toContain(t('chat.secretForm.title')),
    );
    expect(launches).toHaveLength(0);
    buttonWithText('button', t('common.cancel')).click();
    await tick();
    const picker = popup.querySelector('input[type="file"]');
    Object.defineProperty(picker, 'files', {
      value: [new File(['note'], 'note.txt', { type: 'text/plain' })],
    });
    picker.dispatchEvent(new Event('change', { bubbles: true }));
    typeInto(input, 'Question about @README.md');
    await waitForCondition(() =>
      expect(popup.querySelector('button[type="submit"]').disabled).toBe(false),
    );
    expect(launches).toEqual([]);
    expect(popup.querySelector('.chat-view')).toBeNull();
    popup.querySelector('button[type="submit"]').click();
    await waitForCondition(() => {
      expect(popup.textContent).toContain('Missing acknowledgement');
      expect(input.value).toBe('Question about @README.md');
    });
    // The fork succeeded. A fresh /btw now opens the actual Session, whose
    // composer must recover the same draft and retry the original envelope.
    const reopen = async () => {
      popup.querySelector(`button[aria-label="${t('common.close')}"]`).click();
      await tick();
      typeInto(main.querySelector('textarea'), '/btw');
      main.querySelector('button[type="submit"]').click();
      await waitForCondition(() => {
        popup = document.querySelector('.session-popup');
        expect(popup?.querySelector('textarea:enabled')).toBeTruthy();
      });
    };
    await reopen();
    expect(popup.querySelector('.chat-view textarea').value).toBe(
      'Question about @README.md',
    );
    popup.querySelector('button[type="submit"]').click();
    await waitForCondition(() =>
      expect(popup.querySelector('.chat-view textarea:enabled')?.value).toBe(
        '',
      ),
    );
    expect(launches).toEqual([
      {
        ...source,
        source_generation_id: 'source-gen',
        launch_id: expect.any(String),
      },
    ]);
    expect(requests).toHaveLength(3);
    expect(requests).toEqual(
      Array(3).fill({
        agent_id: 'builder@work',
        session_id: 'side',
        input_id: expect.any(String),
        expected_generation_id: 'side-gen',
        file_mentions: ['README.md'],
        content: [
          { type: 'text', text: 'Question about @README.md' },
          { type: 'file', ...attachment },
        ],
      }),
    );
    expect(new Set(requests.map((request) => request.input_id)).size).toBe(1);
    expect(popup.querySelector('.chat-view textarea').value).toBe('');
    expect(popup.querySelector('.attachment-name')).toBeNull();
    typeInto(popup.querySelector('textarea'), 'Follow up');
    popup.querySelector('button[type="submit"]').click();
    await waitForCondition(() =>
      expect(requests.at(-1)).toMatchObject({
        agent_id: 'builder@work',
        session_id: 'side',
        content: 'Follow up',
      }),
    );
    expect(launches).toHaveLength(1);
    expect(document.querySelectorAll('.session-popup')).toHaveLength(1);
    expect(window.location.hash).toBe('#chat/builder@work/source');
    hasChild = false; // The previous fork was archived independently.
    await reopen();
    expect(popup.querySelector('.chat-view')).toBeNull();
    expect(launches).toHaveLength(1);
    await subscribeServerEventsMock.mock.calls[0][0].onEvent({
      type: 'resource_changed',
      sequence: 1,
      payload: {
        kind: 'sessions',
        scope: {
          agent_id: 'builder',
          project_id: 'work',
          deleted_session_id: 'source',
        },
      },
    });
    await waitForCondition(() =>
      expect(document.querySelector('.session-popup')).toBeNull(),
    );
  });

  it.each([
    ['Please check this detail', true],
    ['Check ' + 'sk-proj-' + 'x'.repeat(48), false],
  ])(
    'keeps a rejected side question editable and shows the reason (%s)',
    async (question, mayStore) => {
      rpcMock.mockImplementation(
        createAppRpcMock({
          agents: [baseAgent()],
          methods: {
            'chat.stream': () => ({
              command_handled: true,
              output: 'action',
              reply: 'No usable Model',
              data: {
                question_draft: question,
                navigation: {
                  kind: 'open_side_chat',
                  agent_id: 'alpha',
                  project_id: null,
                  session_id: mayStore ? 'failed-question' : 'private-question',
                },
              },
            }),
          },
        }),
      );
      mountApp();
      const input = () => document.querySelector('.chat-view .msg-input');
      await waitForCondition(() => expect(input()?.disabled).toBe(false));
      const original = input();
      typeInto(original, '/btw ' + question);
      flushSync();
      original.closest('form').querySelector('button[type="submit"]').click();
      await waitForCondition(() => {
        expect(
          document.querySelector('.session-popup .chat-view__command-toast')
            ?.textContent,
        ).toContain('No usable Model');
        expect(document.querySelector('.session-popup .msg-input')?.value).toBe(
          mayStore ? question : '',
        );
        expect(original.value).toBe(mayStore ? '' : '/btw ' + question);
      });
      if (!mayStore) {
        expect(JSON.stringify(window.localStorage)).not.toContain(question);
        expect(JSON.stringify(window.sessionStorage)).not.toContain(question);
      }
    },
  );

  it('resumes the App Agent in Settings, sends the current field and expands its exact Session', async () => {
    const settingsRpc = createSettingsRpcMock();
    let address;
    let opens = 0;
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'agent.list') return { agents: [baseAgent()] };
      if (method === 'session.create_app_chat') {
        opens++;
        expect(params.profile).toBe('app');
        if (!address) {
          address = {
            agent_id: 'app-assistant',
            session_id: params.session_id,
          };
          throw new Error('Lost the response');
        }
        expect(params.session_id).toBe(address.session_id);
        return address;
      }
      if (method === 'session.get')
        return { session: { ...address, title: 'Settings' } };
      if (method === 'chat.stream' && params.content === '/btw') {
        return {
          command_handled: true,
          output: 'action',
          data: {
            navigation: {
              kind: 'open_side_chat',
              agent_id: 'app-assistant',
              project_id: null,
              session_id: 'settings-side',
            },
          },
        };
      }
      if (method === 'chat.timeline' && params.agent_id === 'app-assistant')
        return {
          ...assistantHistory('greeting', 'How can I help?'),
          agent_name: 'App Agent',
        };
      if (method === 'chat.stream')
        return { ...address, run_id: 'run-settings' };
      return settingsRpc(method, params);
    });
    mountApp();
    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(labelledButton('appChat.askSettings')).toBeTruthy(),
    );
    labelledButton('appChat.askSettings').click();
    await waitForCondition(() =>
      expect(document.querySelector('.session-popup textarea')).toBeTruthy(),
    );
    expect(opens).toBe(0);
    expect(
      buttonWithText('.session-popup button', t('appChat.openFull')),
    ).toBeFalsy();
    document
      .querySelector(
        '.session-popup button[aria-label="' + t('common.close') + '"]',
      )
      .click();
    await tick();
    labelledButton('appChat.askSettings').click();
    settingsPanelButton('system').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('[data-setting-path="server.keep_awake"]'),
      ).toBeTruthy(),
    );
    const control = document.querySelector(
      '[data-setting-path="server.keep_awake"] [role="switch"]',
    );
    control.focus();
    const draft = document.querySelector('.session-popup textarea');
    typeInto(draft, 'What does this do?');
    expect(opens).toBe(0);
    document.querySelector('.session-popup button[type="submit"]').click();
    await waitForCondition(() => {
      expect(document.querySelector('.session-popup')?.textContent).toContain(
        'Lost the response',
      );
      expect(draft.value).toBe('What does this do?');
    });
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'chat.stream'),
    ).toHaveLength(0);
    document.querySelector('.session-popup button[type="submit"]').click();
    await waitForCondition(() =>
      expect(rpcMock).toHaveBeenCalledWith(
        'chat.stream',
        expect.objectContaining({
          ...address,
          ui_context: {
            view: 'settings',
            docs: 'references/configuration.md',
            page: 'system',
            section: 'server',
            field: 'server.keep_awake',
            options: ['true', 'false'],
          },
        }),
      ),
    );
    expect(opens).toBe(2);
    expect(window.location.hash).toBe('#settings/system');
    await waitForCondition(() =>
      expect(
        document.querySelector('.session-popup .chat-view textarea:enabled'),
      ).toBeTruthy(),
    );
    const settingsPopup = document.querySelector('.session-popup');
    const input = settingsPopup.querySelector('textarea');
    function openNested() {
      input.focus();
      typeInto(input, '/btw');
      flushSync();
      settingsPopup.querySelector('button[type="submit"]').click();
    }
    openNested();
    const nested = () => document.querySelector('.session-popup:not([hidden])');
    await waitForCondition(() =>
      expect(nested()?.getAttribute('aria-label')).toBe(t('appChat.sideTitle')),
    );
    expect(settingsPopup.hidden).toBe(true);
    const closeNested = nested().querySelector(
      'button[aria-label="' + t('common.close') + '"]',
    );
    closeNested.focus();
    closeNested.click();
    await waitForCondition(() => {
      expect(nested()).toBe(settingsPopup);
      expect(document.activeElement).toBe(input);
    });
    openNested();
    await waitForCondition(() => expect(settingsPopup.hidden).toBe(true));
    sidebarNavButton('agents').click();
    await waitForCondition(() =>
      expect(document.querySelector('.session-popup')).toBeNull(),
    );
    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(labelledButton('appChat.askSettings')).toBeTruthy(),
    );
    expect(document.querySelector('.session-popup')).toBeNull();
    labelledButton('appChat.askSettings').click();
    await waitForCondition(() =>
      expect(nested()?.querySelector('.msg-input')).toBeTruthy(),
    );
    expect(opens).toBe(2);
    buttonWithText('.session-popup button', t('appChat.openFull')).click();
    await waitForCondition(() => {
      expect(document.querySelector('.session-popup')).toBeNull();
      expect(window.location.hash).toBe(
        `#chat/app-assistant/${address.session_id}`,
      );
      expect(
        document.querySelector('.agent-pill--current .agent-pill__name')
          ?.textContent,
      ).toBe('App Agent');
    });
  });

  it('keeps the first command identity when an uncertain Settings popup is closed and reopened', async () => {
    const settingsRpc = createSettingsRpcMock();
    const commands = [];
    rpcMock.mockImplementation((method, params) => {
      if (method === 'session.create_app_chat')
        return {
          agent_id: 'app-assistant',
          session_id: params.session_id,
          generation_id: 'app-gen',
        };
      if (method === 'chat.stream') {
        commands.push(params);
        if (commands.length === 1)
          throw Object.assign(new Error('Missing command reply'), {
            code: 'network_error',
          });
        return {
          command_handled: true,
          output: 'notice',
          reply: 'Already admitted',
        };
      }
      return settingsRpc(method, params);
    });
    mountApp();
    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(labelledButton('appChat.askSettings')).toBeTruthy(),
    );
    labelledButton('appChat.askSettings').click();
    const popup = () => document.querySelector('.session-popup');
    await waitForCondition(() =>
      expect(popup()?.querySelector('textarea')).toBeTruthy(),
    );
    typeInto(popup().querySelector('textarea'), '/btw Please clarify');
    popup().querySelector('button[type="submit"]').click();
    await waitForCondition(() =>
      expect(popup().textContent).toContain('Missing command reply'),
    );
    popup()
      .querySelector(`button[aria-label="${t('common.close')}"]`)
      .click();
    await tick();
    labelledButton('appChat.askSettings').click();
    await waitForCondition(() =>
      expect(popup()?.querySelector('textarea:enabled')?.value).toBe(
        '/btw Please clarify',
      ),
    );
    popup().querySelector('button[type="submit"]').click();
    await waitForCondition(() =>
      expect(popup().textContent).toContain('Already admitted'),
    );
    expect(commands).toHaveLength(2);
    expect(commands[0]).toMatchObject({
      expected_generation_id: 'app-gen',
      content: '/btw Please clarify',
    });
    expect(commands[1]).toEqual(commands[0]);
    expect(
      rpcMock.mock.calls.filter(
        ([method]) => method === 'session.create_app_chat',
      ),
    ).toHaveLength(1);
  });

  it('returns to shared defaults from the Main menu and stays there on a second click', async () => {
    const settingsRpc = createSettingsRpcMock();
    const agentsRpc = agentsAppRpc({ agents: [baseAgent()] });
    rpcMock.mockImplementation((method, params) =>
      method === 'settings.get'
        ? settingsRpc(method, params)
        : agentsRpc(method, params),
    );
    mountApp();
    // Open defaults within Agents; the search link is covered above.
    sidebarNavButton('agents').click();
    await waitForCondition(() => expect(agentShown('alpha')).toBe(true));
    document.querySelector('.agent-list-defaults button').click();
    const defaultsShown = () => {
      expect(isCurrent('agents')).toBe(true);
      expect(window.location.hash).toBe('#agents/~defaults');
      expect(document.querySelector('#settings-defaults-model')).toBeTruthy();
    };
    await waitForCondition(defaultsShown);

    sidebarNavButton('chat').click();
    await waitForCondition(() => expect(isCurrent('chat')).toBe(true));
    // The Main menu returns to the place Agents showed last.
    sidebarNavButton('agents').click();
    await waitForCondition(defaultsShown);

    // Choosing the shown view again keeps its place and adds no step.
    const entries = window.history.length;
    sidebarNavButton('agents').click();
    flushSync();
    await Promise.resolve();
    defaultsShown();
    expect(window.history.length).toBe(entries);
  });

  it('records each chosen record as a step and restores it with Back and Forward', async () => {
    rpcMock.mockImplementation(
      agentsAppRpc({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
      }),
    );
    mountApp();
    sidebarNavButton('agents').click();
    await waitForCondition(() => expect(agentButton('bravo')).toBeTruthy());

    agentButton('bravo').click();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#agents/bravo');
      expect(agentShown('bravo')).toBe(true);
    });

    agentButton('alpha').click();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#agents/alpha');
      expect(agentShown('alpha')).toBe(true);
    });

    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#agents/bravo');
      expect(agentShown('bravo')).toBe(true);
    });

    window.history.forward();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#agents/alpha');
      expect(agentShown('alpha')).toBe(true);
    });
  });

  it('closes an open dialog on Back and stays at the place', async () => {
    rpcMock.mockImplementation(agentsAppRpc({ agents: [baseAgent()] }));
    mountApp();
    sidebarNavButton('logs').click();
    sidebarNavButton('agents').click();
    await waitForCondition(() => expect(agentShown('alpha')).toBe(true));
    const hash = window.location.hash;

    labelledButton('agents.create').click();
    await waitForCondition(() =>
      expect(document.querySelector('[role="dialog"]')).toBeTruthy(),
    );

    window.history.back();
    await waitForCondition(() => {
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(window.location.hash).toBe(hash);
    });
    expect(isCurrent('agents')).toBe(true);
  });

  it('keeps the Desktop app open at its first entry and moves with Alt+Arrow keys', async () => {
    window.history.replaceState(null, '', '/?accessor=desktop');
    mountApp();

    sidebarNavButton('logs').click();
    await waitForCondition(() => expect(logsShown()).toBe(true));

    window.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowLeft',
        altKey: true,
        cancelable: true,
      }),
    );
    await waitForCondition(() => expect(isCurrent('chat')).toBe(true));

    // Back from the first app entry reaches the floor below it, which sends
    // the WebView forward again.
    const popped = [];
    const recordPop = (event) => popped.push(event.state);
    window.addEventListener('popstate', recordPop);
    try {
      window.history.back();
      await waitForCondition(() => expect(popped).toHaveLength(2));
    } finally {
      window.removeEventListener('popstate', recordPop);
    }
    expect(popped[0]?.floor).toBe(true);
    expect(window.history.state?.view).toBe('chat');
    expect(isCurrent('chat')).toBe(true);
  });

  it('retains shared defaults when their transition save fails', async () => {
    rpcMock.mockImplementation(failingSettingsRpc(Infinity));
    mountApp();
    sidebarNavButton('agents').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('.agent-list-defaults button'),
      ).toBeTruthy(),
    );
    document.querySelector('.agent-list-defaults button').click();
    await waitForCondition(() =>
      expect(
        document.querySelector('#settings-defaults-temperature'),
      ).toBeTruthy(),
    );
    const input = document.querySelector('#settings-defaults-temperature');
    typeInto(input, '0.73');
    document.querySelector('.agent-shared-title button').click();
    await waitForCondition(() =>
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeTruthy(),
    );
    expect(document.querySelector('.agent-shared-pane').hidden).toBe(false);
    expect(input.value).toBe('0.73');
  });

  it('persists the selected Agent and restores it after remount', async () => {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [
          { id: 'alpha', name: 'Alpha', current_session_id: 'session-alpha' },
          { id: 'beta', name: 'Beta', current_session_id: 'session-beta' },
        ],
      }),
    );
    mountApp();

    await selectPersonalAgent('Beta');
    await waitForCondition(() => {
      expect(localStorage.getItem('vbot.selectedAgentId')).toBe('beta');
    });

    await unmount(mountedComponent);
    rpcMock.mockClear();
    mountApp();

    await waitForCondition(() => {
      expect(selectedAgentName()).toBe('Beta');
      expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
        agent_id: 'beta',
        session_id: 'session-beta',
        limit: 100,
        child_limit: 100,
      });
    });
  });

  it('opens an Extension page deep link once the page catalog has loaded', async () => {
    const pages = Promise.withResolvers();
    const declarations = Promise.withResolvers();
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'extensions.pages': () => pages.promise,
          'extensions.contributions': () => declarations.promise,
        },
      }),
    );
    window.history.replaceState(null, '', `#${FIXTURE_EXTENSION_ROUTE}`);
    mountApp();
    sendConnectionReady();

    // Startup cannot know the route yet; the link stays in the URL meanwhile.
    expect(window.location.hash).toBe(`#${FIXTURE_EXTENSION_ROUTE}`);
    expect(document.querySelector('iframe')).toBeFalsy();

    pages.resolve({ pages: [FIXTURE_EXTENSION_PAGE] });

    await waitForCondition(() => {
      expect(document.querySelector('iframe')).toBeTruthy();
      expect(isCurrent('Fixture page')).toBe(true);
      expect(window.location.hash).toBe(`#${FIXTURE_EXTENSION_ROUTE}`);
      expect(window.history.state?.view).toBe(FIXTURE_EXTENSION_ROUTE);
    });
    declarations.resolve({ contributions: [] });
  });

  it('saves a team profile in its Extension page and closes back to the same frame and personal selection', async () => {
    const personal = baseAgent();
    const reviewer = {
      ...personal,
      id: 'reviewer',
      name: 'Reviewer',
      extension_owner: 'factory',
    };
    const stamp = reviewer.created_at;
    const agentsRpc = createAgentsRpcMock({ agents: [personal] });
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [personal],
        methods: {
          'extensions.pages': () => ({ pages: [FIXTURE_EXTENSION_PAGE] }),
          'agent.get': () => reviewer,
          'agent.soul.get': (params) => agentsRpc('agent.soul.get', params),
          'agent.update': (params) => ({ ...reviewer, ...params }),
        },
      }),
    );
    const hash = `#${FIXTURE_EXTENSION_ROUTE}/team`;
    window.history.replaceState(null, '', hash);
    mountApp();
    sendConnectionReady();
    await waitForCondition(() =>
      expect(document.querySelector('iframe')).toBeTruthy(),
    );
    const frame = document.querySelector('iframe');
    const win = frame.contentWindow;
    const sent = vi.spyOn(win, 'postMessage');
    frame.dispatchEvent(new Event('load'));
    const init = sent.mock.calls.find(
      ([data]) => data.type === 'vbot.extension.init',
    )[0];
    function fromPage(type, fields = {}) {
      const event = new MessageEvent('message', {
        data: { ...init, type, ...fields },
      });
      Object.defineProperties(event, {
        source: { value: win },
        origin: { value: 'null' },
      });
      window.dispatchEvent(event);
    }
    fromPage('vbot.extension.ready');
    fromPage('vbot.extension.call', {
      id: 'edit',
      method: 'agent.open',
      params: { agent_id: 'reviewer', expected_incarnation: stamp },
    });
    await waitForCondition(() =>
      expect(document.querySelector('[role="dialog"] #agent-name')?.value).toBe(
        'Reviewer',
      ),
    );
    typeInto(document.querySelector('#agent-name'), 'My reviewer');
    document.querySelector('[role="dialog"] .modal-close').click();
    await waitForCondition(() => {
      expect(rpcMock).toHaveBeenCalledWith('agent.update', {
        id: 'reviewer',
        expected_incarnation: stamp,
        name: 'My reviewer',
      });
      expect(document.querySelector('[role="dialog"]')).toBeNull();
    });
    expect(document.querySelector('iframe')).toBe(frame);
    expect(window.location.hash).toBe(hash);
    expect(localStorage.getItem('vbot.selectedAgentId')).toBe(personal.id);
  });

  describe('Session links from outside the page', () => {
    const ALPHA = {
      id: 'alpha',
      name: 'Alpha',
      current_session_id: 'session-alpha',
    };
    const olderSessionRpc = () =>
      createAppRpcMock({
        agents: [ALPHA],
        history: (params) =>
          params.session_id === 'session-old'
            ? assistantHistory('old-reply', 'Older reply')
            : {},
      });

    it.each([
      ['alpha', 'session-alpha'],
      ['beta', ''],
      ['beta', 'session-beta'],
      ['builder@work', ''],
      ['builder@work', 'session-builder'],
    ])(
      'adopts the Agent context from copied #chat/%s/%s',
      async (address, sessionId) => {
        const projectAgent = address.includes('@');
        const scan = {
          team: [{ agent_id: 'builder', display_name: 'Builder' }],
          report: { clean: true, findings: [] },
        };
        let releaseProjects;
        const projectCatalog = new Promise((resolve) => {
          releaseProjects = () =>
            resolve({
              projects: [{ project_id: 'work', display_name: 'Work', scan }],
            });
        });
        if (!projectAgent) releaseProjects();
        let releaseRoster;
        const roster = new Promise((resolve) => {
          releaseRoster = () =>
            resolve({
              agents: [
                ALPHA,
                {
                  id: 'beta',
                  name: 'Beta',
                  current_session_id: 'session-beta',
                },
              ],
            });
        });
        if (!sessionId) releaseRoster();
        rpcMock.mockImplementation(
          createAppRpcMock({
            history: (params) =>
              assistantHistory(
                `reply-${params.session_id}`,
                `Reply in ${params.session_id}`,
              ),
            methods: {
              'agent.list': () => roster,
              'project.list': () => projectCatalog,
              'project.show': () => ({ project: { project_id: 'work' }, scan }),
            },
          }),
        );
        localStorage.setItem('vbot.selectedAgentId', 'alpha');
        const copiedHash = `#chat/${address}${sessionId ? `/${sessionId}` : ''}`;
        window.history.replaceState(null, '', copiedHash);
        mountApp();
        if (sessionId) {
          await waitForCondition(() =>
            expect(rpcMock).toHaveBeenCalledWith(
              'chat.timeline',
              expect.objectContaining({
                agent_id: address,
                session_id: sessionId,
              }),
            ),
          );
          expect(document.body.textContent).not.toContain(
            `Reply in ${sessionId}`,
          );
          releaseRoster();
        }
        if (projectAgent) {
          await waitForCondition(() =>
            expect(rpcMock).toHaveBeenCalledWith(
              'chat.timeline',
              expect.objectContaining({
                agent_id: sessionId ? address : 'alpha',
              }),
            ),
          );
          expect(window.location.hash).toBe(copiedHash);
          if (sessionId) {
            expect(document.body.textContent).not.toContain(
              `Reply in ${sessionId}`,
            );
            expect(localStorage.getItem('vbot.selectedProjectId')).toBeNull();
          }
          releaseProjects();
        }

        const expectContext = (expectedSessionId) => {
          expect(window.location.hash).toBe(
            `#chat/${address}${expectedSessionId ? `/${expectedSessionId}` : ''}`,
          );
          expect(window.history.state?.extra?.selection).toMatchObject(
            projectAgent
              ? {
                  agentId: 'alpha',
                  projectId: 'work',
                  projectAgentId: 'builder',
                }
              : { agentId: address, projectId: '' },
          );
          expect(document.querySelector('.msg-input')).toBeTruthy();
        };
        await waitForCondition(() => expectContext(sessionId));
        if (sessionId) {
          await waitForCondition(() =>
            expect(document.body.textContent).toContain(
              `Reply in ${sessionId}`,
            ),
          );
          expect(
            rpcMock.mock.calls.filter(
              ([method, params]) =>
                method === 'chat.timeline' &&
                params.agent_id === address &&
                params.session_id === sessionId,
            ),
          ).toHaveLength(1);
          labelledButton('chat.newSession').click();
          await waitForCondition(() => expectContext(''));
          window.history.back();
          await waitForCondition(() => expectContext(sessionId));
          window.history.forward();
          await waitForCondition(() => expectContext(''));
        }

        const nextAgent = address === 'alpha' ? 'Beta' : 'Alpha';
        await selectPersonalAgent(nextAgent);
        await waitForCondition(() =>
          expect(selectedAgentName()).toBe(nextAgent),
        );
        window.history.back();
        await waitForCondition(() => expectContext(''));
      },
    );

    it.each([
      'librarian',
      'factory-leader',
      'hidden@work',
      'builder@unavailable',
      'builder@work',
    ])(
      'keeps an unselectable copied Session owner %s as an override',
      async (address) => {
        rpcMock.mockImplementation(
          createAppRpcMock({
            agents: [ALPHA],
            history: () => assistantHistory('linked-reply', 'Linked reply'),
            methods: {
              'agent.get': ({ id }) => ({ id, name: 'Factory Leader' }),
              'project.list': () => {
                if (address.endsWith('@unavailable'))
                  throw new Error('Catalog unavailable');
                return {
                  projects: [
                    {
                      project_id: 'work',
                      scan: {
                        team: [
                          { agent_id: 'builder', display_name: 'Builder' },
                        ],
                      },
                    },
                  ],
                };
              },
              'project.show': () => ({
                project: { project_id: 'work' },
                scan: { team: [] },
              }),
            },
          }),
        );
        localStorage.setItem(
          'vbot.selectedAgentId',
          address === 'factory-leader' ? address : 'alpha',
        );
        window.history.replaceState(
          null,
          '',
          `#chat/${address}/linked-session`,
        );
        mountApp();
        const linkedSessionShown = () => {
          expect(window.location.hash).toBe(`#chat/${address}/linked-session`);
          expect(document.body.textContent).toContain('Linked reply');
          expect(document.querySelector('.msg-input')).toBeTruthy();
          if (address === 'factory-leader') {
            expect(
              document
                .querySelector('.agent-pill--current')
                ?.textContent.trim(),
            ).toBe('Factory Leader');
            expect(localStorage.getItem('vbot.selectedAgentId')).toBe('alpha');
          }
          expect(window.history.state?.extra?.selection).toMatchObject({
            agentId: 'alpha',
            projectId: '',
          });
        };
        await waitForCondition(linkedSessionShown);
        labelledButton('chat.newSession').click();
        await waitForCondition(() =>
          expect(window.location.hash).toBe('#chat/alpha'),
        );
        window.history.back();
        await waitForCondition(linkedSessionShown);
      },
    );

    it('opens an explicitly addressed Session with an empty personal roster', async () => {
      rpcMock.mockImplementation(
        createAppRpcMock({
          agents: [],
          history: () => assistantHistory('review', 'Factory review'),
          methods: { 'agent.get': ({ id }) => ({ id, name: 'Reviewer' }) },
        }),
      );
      window.history.replaceState(
        null,
        '',
        '#chat/factory-reviewer/review-session',
      );
      mountApp();
      await waitForCondition(() => {
        expect(document.body.textContent).toContain('Factory review');
        expect(
          document.querySelector('.agent-pill--current')?.textContent.trim(),
        ).toBe('Reviewer');
        expect(document.querySelector('.msg-input')).toBeTruthy();
        expect(localStorage.getItem('vbot.selectedAgentId')).toBeNull();
        expect(labelledButton('chat.newSession').disabled).toBe(true);
      });
    });

    it.each(['Agent', 'view'])(
      'does not resume a copied Project link after a newer %s choice',
      async (choice) => {
        const scan = {
          team: [{ agent_id: 'builder', display_name: 'Builder' }],
        };
        let releaseProject;
        const readingProject = new Promise((resolve) => {
          releaseProject = () =>
            resolve({ project: { project_id: 'work' }, scan });
        });
        rpcMock.mockImplementation(
          createAppRpcMock({
            agents: [
              ALPHA,
              { id: 'beta', name: 'Beta', current_session_id: 'session-beta' },
            ],
            history: (params) =>
              params.session_id === 'linked-session'
                ? assistantHistory('linked', 'Reply from the pending link')
                : {},
            methods: {
              'project.list': () => ({
                projects: [{ project_id: 'work', scan }],
              }),
              'project.show': () => readingProject,
            },
          }),
        );
        window.history.replaceState(
          null,
          '',
          '#chat/builder@work/linked-session',
        );
        mountApp();
        await waitForCondition(() =>
          expect(rpcMock).toHaveBeenCalledWith('project.show', {
            project_id: 'work',
          }),
        );
        if (choice === 'Agent') await selectPersonalAgent('Beta');
        else sidebarNavButton('logs').click();
        await waitForCondition(() =>
          expect(window.location.hash).toMatch(
            choice === 'Agent'
              ? /^#chat\/beta\/session-beta$/
              : /^#logs(?:\/|$)/,
          ),
        );
        releaseProject();
        await readingProject;
        await tick();
        await tick();
        expect(window.location.hash).toMatch(
          choice === 'Agent' ? /^#chat\/beta\/session-beta$/ : /^#logs(?:\/|$)/,
        );
        if (choice === 'Agent') {
          expect(selectedAgentName()).toBe('Beta');
          expect(localStorage.getItem('vbot.selectedProjectId')).toBeNull();
        }
        expect(rpcMock).toHaveBeenCalledWith(
          'chat.timeline',
          expect.objectContaining({
            agent_id: 'builder@work',
            session_id: 'linked-session',
          }),
        );
        expect(document.body.textContent).not.toContain(
          'Reply from the pending link',
        );
        if (choice === 'view') {
          window.history.back();
          await waitForCondition(() => {
            expect(window.location.hash).toBe(
              '#chat/builder@work/linked-session',
            );
            expect(rpcMock).toHaveBeenCalledWith(
              'chat.timeline',
              expect.objectContaining({
                agent_id: 'builder@work',
                session_id: 'linked-session',
              }),
            );
            expect(document.querySelector('.msg-input')).toBeTruthy();
          });
        }
      },
    );

    it('restores the current Session selection when Back overtakes a Project link', async () => {
      const scan = {
        team: [{ agent_id: 'builder', display_name: 'Builder' }],
      };
      const readingProject = Promise.withResolvers();
      rpcMock.mockImplementation(
        createAppRpcMock({
          agents: [ALPHA],
          history: ({ session_id }) =>
            assistantHistory(session_id, `Reply in ${session_id}`),
          methods: {
            'project.list': () => ({
              projects: [{ project_id: 'work', scan }],
            }),
            'project.show': () => readingProject.promise,
          },
        }),
      );
      mountApp();
      await waitForCondition(() => {
        expect(window.location.hash).toBe('#chat/alpha/session-alpha');
        expect(document.body.textContent).toContain('Reply in session-alpha');
      });

      window.location.hash = '#chat/builder@work/linked-session';
      await waitForCondition(() => {
        expect(localStorage.getItem('vbot.selectedProjectId')).toBe('work');
        expect(document.body.textContent).toContain('Reply in session-alpha');
      });
      window.history.back();
      await waitForCondition(() =>
        expect(window.location.hash).toBe('#chat/alpha/session-alpha'),
      );
      readingProject.resolve({ project: { project_id: 'work' }, scan });
      await tick();
      await tick();
      expect(localStorage.getItem('vbot.selectedProjectId')).toBeNull();
      expect(document.body.textContent).toContain('Reply in session-alpha');

      window.history.forward();
      await waitForCondition(() => {
        expect(window.location.hash).toBe('#chat/builder@work/linked-session');
        expect(document.body.textContent).toContain('Reply in linked-session');
      });
    });

    it('opens the Session a startup link names and takes the link out of the address bar', async () => {
      rpcMock.mockImplementation(olderSessionRpc());
      window.history.replaceState(
        null,
        '',
        '/?desktop_session=launch-1&open_agent=alpha&open_session=session-old',
      );
      mountApp();

      expect(window.location.search).toBe('?desktop_session=launch-1');
      await waitForCondition(() => {
        expect(document.body.textContent).toContain('Older reply');
        expect(window.location.hash).toBe('#chat/alpha/session-old');
      });

      // Back returns to the Agent's current Session; no entry keeps the link.
      window.history.back();
      await waitForCondition(() => {
        expect(document.body.textContent).not.toContain('Older reply');
        expect(window.location.hash).toBe('#chat/alpha/session-alpha');
      });
      expect(window.location.search).toBe('?desktop_session=launch-1');
    });

    it('keeps the app usable when a startup link names an unknown Session', async () => {
      const appRpc = createAppRpcMock({
        agents: [
          ALPHA,
          { id: 'beta', name: 'Beta', current_session_id: 'session-beta' },
        ],
      });
      rpcMock.mockImplementation((method, params) =>
        method === 'chat.timeline' && params?.session_id === 'missing'
          ? Promise.reject(new Error('Session not found'))
          : appRpc(method, params),
      );
      window.history.replaceState(
        null,
        '',
        '/?open_agent=alpha&open_session=missing',
      );
      mountApp();

      await waitForCondition(() => {
        expect(document.body.textContent).toContain('Session not found');
      });
      expect(window.location.search).toBe('');

      await selectPersonalAgent('Beta');
      await waitForCondition(() => {
        expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
          agent_id: 'beta',
          session_id: 'session-beta',
          limit: 100,
          child_limit: 100,
        });
        expect(document.body.textContent).not.toContain('Session not found');
      });
    });

    it('opens the Session the Desktop app asks for', async () => {
      rpcMock.mockImplementation(olderSessionRpc());
      window.history.replaceState(null, '', '/?accessor=desktop');
      mountApp();
      await waitForCondition(() => {
        expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
          agent_id: 'alpha',
          session_id: 'session-alpha',
          limit: 100,
          child_limit: 100,
        });
      });

      const handled = !window.dispatchEvent(
        new CustomEvent('vbot-desktop-open-session', {
          cancelable: true,
          detail: { agent: 'alpha', session: 'session-old' },
        }),
      );

      expect(handled).toBe(true);
      await waitForCondition(() => {
        expect(document.body.textContent).toContain('Older reply');
        expect(window.location.hash).toBe('#chat/alpha/session-old');
      });
    });
  });

  it('treats tab switches as history entries so browser back returns to the previous tab', async () => {
    mountApp();

    sidebarNavButton('logs').click();
    flushSync();
    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
      expect(window.location.hash).toMatch(/^#logs(\/|$)/);
    });

    window.history.back();

    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat');
      expect(logsShown()).toBe(false);
      expect(isCurrent('chat')).toBe(true);
    });
  });

  it('keeps Chat initialized while another main view is active', async () => {
    const loadComponent = lazyContent.loadComponent;
    let requestedView;
    let finishImport;
    const loading = new Promise((resolve) => {
      finishImport = resolve;
    });
    const load = vi
      .spyOn(lazyContent, 'loadComponent')
      .mockImplementationOnce((loader) => {
        requestedView = loader;
        return loading;
      });
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [
          { id: 'alpha', name: 'Alpha', current_session_id: 'session-alpha' },
        ],
      }),
    );
    listSessionActivityMock.mockResolvedValue({
      agents: [{ agent_id: 'alpha', project_id: null, sessions: [] }],
    });
    mountApp();

    await waitForCondition(() => {
      expect(document.querySelector('.msg-input')).toBeTruthy();
      expect(rpcMock).toHaveBeenCalledWith('chat.timeline', expect.anything());
      expect(listSessionActivityMock).toHaveBeenCalled();
    });

    const composer = document.querySelector('.msg-input');
    const chatView = document.querySelector('.chat-view');
    const timeline = document.querySelector('.messages');
    typeInto(composer, 'Draft kept across views');
    timeline.scrollTop = 137;

    const readsBeforeSwitch = rpcMock.mock.calls.length;
    const activityReadsBeforeSwitch = listSessionActivityMock.mock.calls.length;
    expect(load).not.toHaveBeenCalled();

    // The first requested view is still loading when Chat becomes active
    // again. Finishing that request must not replace or remount Chat.
    sidebarNavButton('logs').click();
    flushSync();
    expect(
      document.querySelector('.lazy-content-feedback[aria-busy]'),
    ).toBeTruthy();
    expect(chatView.hidden).toBe(true);
    sidebarNavButton('chat').click();
    flushSync();
    finishImport(await loadComponent(requestedView));
    await tick();
    expect(logsShown()).toBe(false);
    expect(chatView.hidden).toBe(false);
    expect(document.querySelector('.msg-input')).toBe(composer);
    load.mockRestore();

    sidebarNavButton('logs').click();
    flushSync();
    // A loaded view is available immediately when requested again.
    expect(logsShown()).toBe(true);
    expect(document.querySelector('.lazy-content-feedback')).toBeNull();
    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
      expect(document.querySelector('.chat-view')).toBe(chatView);
      expect(chatView.hidden).toBe(true);
      expect(document.querySelector('.msg-input')).toBe(composer);
    });

    sidebarNavButton('chat').click();
    await waitForCondition(() => {
      expect(document.querySelector('.chat-view')).toBe(chatView);
      expect(chatView.hidden).toBe(false);
      expect(document.querySelector('.msg-input')).toBe(composer);
      expect(composer.value).toBe('Draft kept across views');
      expect(document.querySelector('.messages')?.scrollTop).toBe(137);
    });

    const readsAfterSwitch = rpcMock.mock.calls
      .slice(readsBeforeSwitch)
      .map(([method]) => method);
    expect(readsAfterSwitch).not.toContain('agent.list');
    expect(readsAfterSwitch).not.toContain('chat.timeline');
    expect(listSessionActivityMock).toHaveBeenCalledTimes(
      activityReadsBeforeSwitch,
    );
    expect(document.querySelector('.chat-view__state-banner')).toBeNull();
  });

  it('waits for the Terminals view before applying its first Live layout request', async () => {
    const createActions = vi.spyOn(liveUiActions, 'createLiveUiActions');
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'terminal.list': () => ({ groups: [], terminals: [] }),
        },
      }),
    );
    mountApp();
    const actions = createActions.mock.results[0].value;

    await expect(
      actions.terminalView({ op: 'context' }, { isCurrent: () => true }),
    ).resolves.toMatchObject({ visible_order: [] });
    expect(isCurrent('terminals')).toBe(true);
    expect(document.querySelector('.terminals-view')).toBeTruthy();
  });

  it('names the shown Session when Chat is first opened after loading in the background', async () => {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [
          { id: 'alpha', name: 'Alpha', current_session_id: 'session-alpha' },
        ],
      }),
    );
    window.history.replaceState(null, '', '#logs');
    mountApp();

    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
      expect(rpcMock).toHaveBeenCalledWith('chat.timeline', expect.anything());
    });

    sidebarNavButton('chat').click();
    await waitForCondition(() => {
      expect(isCurrent('chat')).toBe(true);
      expect(window.location.hash).toBe('#chat/alpha/session-alpha');
    });
  });

  it('keeps an unsaved new Cron job across other views and guards unloading the page', async () => {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [{ id: 'alpha', name: 'Alpha' }],
        methods: {
          'cron.list': () => ({ jobs: [] }),
          'project.list': () => ({ projects: [] }),
        },
      }),
    );
    mountApp();
    const prompt = () => document.getElementById('cron-job-prompt');
    sidebarNavButton('cron').click();
    await waitForCondition(() =>
      expect(labelledButton('cron.detail.createTitle')?.disabled).toBe(false),
    );
    labelledButton('cron.detail.createTitle').click();
    await waitForCondition(() => expect(prompt()).toBeTruthy());
    typeInto(prompt(), 'Unfinished prompt');

    sidebarNavButton('logs').click();
    await waitForCondition(() => expect(logsShown()).toBe(true));
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);

    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#cron/schedules/new');
      expect(prompt()?.value).toBe('Unfinished prompt');
    });
  });

  it('retains Settings input after a failed topic change and retries the same navigation', async () => {
    rpcMock.mockImplementation(failingSettingsRpc(1));
    mountApp();
    await openSettingsTools();
    await waitForCondition(() =>
      expect(
        document.querySelector('[data-settings-section="subagents"]').hidden,
      ).toBe(false),
    );
    const input = depthInput();
    typeInto(input, '6');
    settingsPanelButton('general').click();
    await waitForCondition(() =>
      expect(document.querySelector('[role="dialog"]')).toBeTruthy(),
    );
    expect(
      document.querySelector('[data-settings-section="subagents"]').hidden,
    ).toBe(false);
    expect(input.value).toBe('6');

    buttonWithText('[role="dialog"] button', t('common.retry')).click();
    await waitForCondition(() =>
      expect(
        document.querySelector('[data-settings-section="appearance"]').hidden,
      ).toBe(false),
    );
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      subagents: expect.objectContaining({ max_subagent_depth: 6 }),
      base: { subagents: expect.objectContaining({ max_subagent_depth: 4 }) },
    });
  });

  it('restores the Settings topic and its reading position after switching to another tab', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountApp();
    await openSettingsTools();
    // The topic change ends by focusing the page heading.
    await waitForCondition(() => {
      expect(document.activeElement?.id).toBe('settings-page-tools');
    });
    const firstScrollContainer = document.querySelector('.settings-content');
    firstScrollContainer.scrollTop = 640;

    sidebarNavButton('logs').click();
    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
    });

    sidebarNavButton('settings').click();
    await waitForCondition(() => {
      const restoredContainer = document.querySelector('.settings-content');
      expect(restoredContainer).not.toBe(firstScrollContainer);
      expect(restoredContainer.scrollTop).toBe(640);
      expect(
        document.querySelector('[data-settings-section="media_models"]').hidden,
      ).toBe(false);
    });
  });

  it('flushes edits before the latest requested tab and guards closing while pending', async () => {
    const settingsRpc = createSettingsRpcMock();
    let resolveUpdate;
    rpcMock.mockImplementation((method, params) =>
      method === 'settings.update'
        ? new Promise((resolve) => {
            resolveUpdate = () => resolve(settingsRpc(method, params));
          })
        : settingsRpc(method, params),
    );
    mountApp();
    await openSettingsTools();
    typeInto(depthInput(), '5');

    sidebarNavButton('logs').click();
    await waitForCondition(() => {
      expect(rpcMock).toHaveBeenCalledWith('settings.update', {
        subagents: {
          max_subagent_depth: 5,
          max_active_subagents: 8,
          max_active_subagents_total: 50,
        },
        base: {
          subagents: {
            max_subagent_depth: 4,
            max_active_subagents: 8,
            max_active_subagents_total: 50,
          },
        },
      });
    });
    expect(isCurrent('settings')).toBe(true);
    expect(logsShown()).toBe(false);

    const pendingUnload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(pendingUnload);
    expect(pendingUnload.defaultPrevented).toBe(true);
    sidebarNavButton('projects').click();
    flushSync();
    resolveUpdate();
    await waitForCondition(() => {
      expect(isCurrent('projects')).toBe(true);
    });
    const savedUnload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(savedUnload);
    expect(savedUnload.defaultPrevented).toBe(false);
  });

  it('saves an Agent tool change before leaving the Agents tab', async () => {
    let resolveAgentUpdate;
    rpcMock.mockImplementation(
      agentsAppRpc({
        agents: [baseAgent(), { ...baseAgent(), id: 'bravo', name: 'Bravo' }],
        tools: [
          { name: 'bash', description: 'Run shell commands.' },
          { name: 'write', description: 'Write files.' },
        ],
        agentUpdate: (params) =>
          new Promise((resolve) => {
            resolveAgentUpdate = () =>
              resolve({
                ...baseAgent(),
                ...params,
                current_session_id: 'session-1',
              });
          }),
      }),
    );
    mountApp();

    const writeToggle = () =>
      document.querySelector(
        '[data-tool-name="write"][data-tool-access-toggle]',
      );
    sidebarNavButton('agents').click();
    await waitForCondition(() => {
      expect(writeToggle()).toBeTruthy();
    });
    writeToggle().click();
    flushSync();
    sidebarNavButton('chat').click();

    await waitForCondition(() => {
      expect(rpcMock).toHaveBeenCalledWith('agent.update', {
        id: 'alpha',
        tool_access: { mode: 'all', denied: ['write'] },
      });
    });
    expect(isCurrent('agents')).toBe(true);

    resolveAgentUpdate();
    await waitForCondition(() => {
      expect(isCurrent('chat')).toBe(true);
    });
  });

  it.each([
    ['common.retry', 1, 2],
    ['autosave.discardAndContinue', Infinity, 1],
  ])(
    'leaves an editor after a failed transition save through %s',
    async (actionKey, failures, saves) => {
      rpcMock.mockImplementation(failingSettingsRpc(failures));
      mountApp();
      await openSettingsTools();
      typeInto(depthInput(), '5');
      sidebarNavButton('logs').click();

      await waitForCondition(() => {
        expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeTruthy();
      });
      expect(isCurrent('settings')).toBe(true);

      buttonWithText(
        `${TRANSITION_FAILURE_DIALOG} button`,
        t(actionKey),
      ).click();

      await waitForCondition(() => {
        expect(logsShown()).toBe(true);
        expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeFalsy();
      });
      expect(settingsUpdates()).toHaveLength(saves);
    },
  );

  it('offers to leave while a transition save still runs, then stops waiting for that save', async () => {
    const settingsRpc = createSettingsRpcMock();
    rpcMock.mockImplementation((method, params) =>
      method === 'settings.update'
        ? new Promise(() => {})
        : settingsRpc(method, params),
    );
    mountApp();
    await openSettingsTools();
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    typeInto(depthInput(), '5');
    const sectionShown = (id) =>
      document.querySelector(`[data-settings-section="${id}"]`)?.hidden ===
      false;

    settingsPanelButton('general').click();
    await vi.advanceTimersByTimeAsync(AUTOSAVE_STILL_SAVING_MS - 1);
    flushSync();
    expect(settingsUpdates()).toHaveLength(1);
    expect(document.querySelector(STILL_SAVING_DIALOG)).toBeNull();
    await vi.advanceTimersByTimeAsync(1);
    flushSync();
    expect(document.querySelector(STILL_SAVING_DIALOG)).toBeTruthy();

    // Keep waiting closes the prompt; the next navigation asks again at once
    // and becomes the one that runs.
    buttonWithText(
      `${STILL_SAVING_DIALOG} button`,
      t('autosave.keepWaiting'),
    ).click();
    flushSync();
    expect(document.querySelector(STILL_SAVING_DIALOG)).toBeNull();
    settingsPanelButton('memory').click();
    flushSync();
    expect(sectionShown('subagents')).toBe(true);

    buttonWithText(
      `${STILL_SAVING_DIALOG} button`,
      t('autosave.leaveAnyway'),
    ).click();
    flushSync();
    expect(document.querySelector(STILL_SAVING_DIALOG)).toBeNull();
    expect(sectionShown('recall')).toBe(true);

    // The editor stays mounted, but its left-behind save holds nothing.
    settingsPanelButton('system').click();
    flushSync();
    expect(sectionShown('server')).toBe(true);
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(false);
    expect(settingsUpdates()).toHaveLength(1);
  });

  it('never locks on a failed transition save: closing stays, Discard works while Retry runs', async () => {
    const settingsRpc = createSettingsRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method !== 'settings.update') return settingsRpc(method, params);
      return settingsUpdates().length <= 2
        ? Promise.reject(new Error('save unavailable'))
        : new Promise(() => {});
    });
    mountApp();
    await openSettingsTools();
    await waitForCondition(() =>
      expect(window.location.hash).toBe('#settings/tools'),
    );
    typeInto(depthInput(), '5');

    // Closing the prompt keeps the edits and returns the pending Back.
    window.history.back();
    await waitForCondition(() =>
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeTruthy(),
    );
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    flushSync();
    await waitForCondition(() => {
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeNull();
      expect(window.location.hash).toBe('#settings/tools');
    });
    expect(depthInput().value).toBe('5');

    // A Retry that hangs still leaves Discard and continue available.
    sidebarNavButton('logs').click();
    await waitForCondition(() =>
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeTruthy(),
    );
    buttonWithText(
      `${TRANSITION_FAILURE_DIALOG} button`,
      t('common.retry'),
    ).click();
    flushSync();
    expect(
      buttonWithText(`${TRANSITION_FAILURE_DIALOG} button`, t('common.saving'))
        .disabled,
    ).toBe(true);
    const discard = buttonWithText(
      `${TRANSITION_FAILURE_DIALOG} button`,
      t('autosave.discardAndContinue'),
    );
    expect(discard.disabled).toBe(false);
    discard.click();
    await waitForCondition(() => {
      expect(logsShown()).toBe(true);
      expect(document.querySelector(TRANSITION_FAILURE_DIALOG)).toBeNull();
    });
    expect(settingsUpdates()).toHaveLength(3);
  });

  it('returns from a sub-agent Session to its parent and opens the same child again', async () => {
    rpcMock.mockImplementation(
      createSubAgentNavigationRpcMock([
        { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
      ]),
    );
    mountApp();
    const parentShown = () => {
      expect(document.body.textContent).toContain('Inspect again');
      // The child's response also surfaces in the parent tool block, so only
      // the missing return notice tells the parent Session is displayed.
      expect(returnToCurrentSessionButton()).toBeFalsy();
    };

    await openSubAgentSession();
    returnToCurrentSessionButton().click();
    flushSync();
    await waitForCondition(parentShown);

    viewSessionButton().click();
    flushSync();
    await waitForCondition(() => {
      // Two navigation loads (limit 100); the deduplicated result fetch for
      // the parent tool block (limit 20) is not one.
      expect(
        rpcMock.mock.calls.filter(
          ([method, params]) =>
            method === 'chat.timeline' &&
            params?.session_id === 'sub-session-repeat' &&
            params?.limit === 100,
        ),
      ).toHaveLength(2);
      expect(returnToCurrentSessionButton()).toBeTruthy();
    });

    window.history.back();
    await waitForCondition(parentShown);
  });

  it('records New session as a step to a draft place that Back and Forward restore', async () => {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [
          { id: 'alpha', name: 'Alpha', current_session_id: 'session-one' },
        ],
        history: (params) =>
          params.session_id === 'session-one'
            ? assistantHistory('reply', 'First reply')
            : {},
      }),
    );
    mountApp();
    const sessionShown = (shown) => {
      expect(window.location.hash).toBe(
        shown ? '#chat/alpha/session-one' : '#chat/alpha',
      );
      expect(document.body.textContent.includes('First reply')).toBe(shown);
    };
    await waitForCondition(() => sessionShown(true));

    labelledButton('chat.newSession').click();
    flushSync();
    await waitForCondition(() => sessionShown(false));

    window.history.back();
    await waitForCondition(() => sessionShown(true));

    window.history.forward();
    await waitForCondition(() => sessionShown(false));
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'session.create'),
    ).toEqual([]);
  });

  it('restores the selected Agent together with the Session override on browser back', async () => {
    rpcMock.mockImplementation(
      createSubAgentNavigationRpcMock([
        { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
        { id: 'beta', name: 'Beta', current_session_id: 'session-beta' },
      ]),
    );
    mountApp();
    await openSubAgentSession();

    // Selecting Beta clears the override in a new history entry.
    await selectPersonalAgent('Beta');
    await waitForCondition(() => {
      expect(selectedAgentName()).toBe('Beta');
      expect(document.body.textContent).not.toContain('Sub-agent response');
    });

    window.history.back();

    // Back restores the entry's whole Chat context: the sub-agent Session and
    // the Agent it belongs to.
    await waitForCondition(() => {
      expect(document.body.textContent).toContain('Sub-agent response');
      expect(selectedAgentName()).toBe('Alpha');
    });
  });

  it('keeps display and history entry in sync after tab-away, tab-back and a double back', async () => {
    rpcMock.mockImplementation(
      createSubAgentNavigationRpcMock([
        { id: 'alpha', name: 'Alpha', current_session_id: 'session-parent' },
      ]),
    );
    mountApp();
    await openSubAgentSession();

    // Tab away and back: the Main menu returns to Chat's last place, the
    // child Session the persistent Chat owner still shows.
    sidebarNavButton('logs').click();
    flushSync();
    await waitForCondition(() => {
      expect(window.location.hash).toMatch(/^#logs(\/|$)/);
    });
    sidebarNavButton('chat').click();
    flushSync();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat/alpha/sub-session-repeat');
      expect(document.body.textContent).toContain('Sub-agent response');
    });

    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toMatch(/^#logs(\/|$)/);
    });

    // The second back pops the earlier Chat entry with the child Session:
    // restoring it must not replace that entry with a place without it.
    window.history.back();
    await waitForCondition(() => {
      expect(window.location.hash).toBe('#chat/alpha/sub-session-repeat');
      expect(document.body.textContent).toContain('Sub-agent response');
      expect(window.history.state?.extra?.subAgent).toBe(true);
    });
  });
});
