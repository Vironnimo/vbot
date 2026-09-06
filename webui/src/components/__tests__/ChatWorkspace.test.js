// @vitest-environment jsdom
import { beforeEach } from 'vitest';
import {
  describe,
  expect,
  it,
  rpcMock,
  createChatRpcMock,
  createAgent,
  listSessionsMock,
  setInputValue,
  flushSync,
  waitForCondition,
  setupChatViewTestSuite,
  testChatStateRefs,
} from './ChatView.support.js';
import { resetComposerMemory } from '../../lib/composerMemory.js';
import ChatWorkspace from '../ChatWorkspace.svelte';

describe('ChatWorkspace', () => {
  const harness = setupChatViewTestSuite();
  beforeEach(() => {
    localStorage.clear();
    resetComposerMemory();
  });

  const pane = (index) =>
    document.querySelectorAll('.chat-workspace__pane')[index];
  const button = (root, label) =>
    root.querySelector(`button[aria-label="${label}"]`);

  async function start() {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-2': [
            {
              id: 'second-answer',
              role: 'assistant',
              content: 'Second conversation sentinel',
            },
          ],
        },
      }),
    );
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-2',
          title: 'Second topic',
          created_at: '2026-05-10T00:00:00+00:00',
          last_active_at: '2026-05-10T01:00:00+00:00',
        },
      ],
    });
    harness.mount(
      {
        target: document.body,
        props: {
          sharedAgents: [createAgent()],
          sharedSelectedAgentId: 'alpha',
        },
      },
      ChatWorkspace,
    );
    await waitForCondition(() => pane(0)?.textContent.includes('Hello'), 100);
    button(pane(0), 'Split view').click();
    await waitForCondition(
      () => pane(1)?.textContent.includes('Second topic'),
      100,
    );
    Array.from(pane(1).querySelectorAll('button'))
      .find((el) => el.textContent.includes('Second topic'))
      .click();
    await waitForCondition(
      () => pane(1)?.textContent.includes('Second conversation sentinel'),
      100,
    );
  }

  it('keeps two real Chat owners independent, retains drafts, and restores closed areas', async () => {
    await start();
    expect(pane(0).textContent).toContain('Hello');
    expect(pane(0).textContent).not.toContain('Second conversation sentinel');
    expect(pane(1).querySelector('.chat-workspace__context').textContent).toBe(
      'Second topic',
    );
    const leftInput = pane(0).querySelector('.msg-input');
    const rightInput = pane(1).querySelector('.msg-input');
    setInputValue(leftInput, 'Left draft sentinel');
    setInputValue(rightInput, 'Right draft sentinel');
    flushSync();
    button(pane(1), 'Close area').click();
    flushSync();
    expect(pane(1).hidden).toBe(true);
    expect(pane(0).querySelector('.msg-input')).toBe(leftInput);
    button(pane(0), 'Split view').click();
    flushSync();
    expect(pane(1).querySelector('.msg-input')).toBe(rightInput);
    expect(leftInput.value).toBe('Left draft sentinel');
    expect(rightInput.value).toBe('Right draft sentinel');
    expect(testChatStateRefs).toHaveLength(2);
    // A new Session in the second pane cannot re-aim the first pane's landing.
    button(pane(1), 'New session').click();
    await waitForCondition(
      () => rpcMock.mock.calls.some(([method]) => method === 'session.create'),
      100,
    );
    expect(testChatStateRefs[0].agents[0].current_session_id).toBe('session-1');
    expect(leftInput.value).toBe('Left draft sentinel');
  });

  it('routes messages to the concrete Session in each area', async () => {
    await start();
    for (const [index, content] of [
      [0, 'Left request'],
      [1, 'Right request'],
    ]) {
      setInputValue(pane(index).querySelector('.msg-input'), content);
      flushSync();
      pane(index).querySelector('.btn-primary.btn-icon').click();
      await waitForCondition(
        () =>
          rpcMock.mock.calls.some(
            ([method, params]) =>
              method === 'chat.stream' && params.content === content,
          ),
        100,
      );
    }
    const requests = rpcMock.mock.calls
      .filter(([method]) => method === 'chat.stream')
      .map(([, params]) => params);
    expect(requests).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          session_id: 'session-1',
          content: 'Left request',
        }),
        expect.objectContaining({
          session_id: 'session-2',
          content: 'Right request',
        }),
      ]),
    );
  });

  it('keeps one composer for a duplicated Session and transfers its draft on close', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    harness.mount({ target: document.body }, ChatWorkspace);
    await waitForCondition(() => pane(0)?.querySelector('.msg-input'), 100);
    const firstInput = pane(0).querySelector('.msg-input');
    setInputValue(firstInput, 'Shared Session draft');
    flushSync();
    button(pane(0), 'Split view').click();
    await waitForCondition(() => testChatStateRefs.length === 2, 100);
    expect(pane(1).querySelector('.msg-input')).toBeNull();
    button(pane(0), 'Close area').click();
    await waitForCondition(() => pane(1).querySelector('.msg-input'), 100);
    const secondInput = pane(1).querySelector('.msg-input');
    expect(secondInput.value).toBe('Shared Session draft');
    setInputValue(secondInput, 'Continued in the right');
    flushSync();
    button(pane(1), 'Split view').click();
    await waitForCondition(() => pane(0).querySelector('.msg-input'), 100);
    expect(pane(0).querySelector('.msg-input').value).toBe(
      'Continued in the right',
    );
    expect(pane(1).querySelector('.msg-input')).toBeNull();
  });

  it('can create a second Session even when the first Session is empty', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: { 'session-1': [], 'created-alpha': [] },
      }),
    );
    harness.mount({ target: document.body }, ChatWorkspace);
    await waitForCondition(() => pane(0)?.querySelector('.msg-input'), 100);
    button(pane(0), 'Split view').click();
    await waitForCondition(() => pane(1)?.querySelector('.chat-view'), 100);
    await waitForCondition(() => button(pane(1), 'New session'), 100);
    button(pane(1), 'New session').click();
    await waitForCondition(() => pane(1).querySelector('.msg-input'), 100);
    expect(testChatStateRefs[0].agents[0].current_session_id).toBe('session-1');
    expect(testChatStateRefs[1].agents[0].current_session_id).toBe(
      'created-alpha',
    );
  });

  it('resizes with keyboard, clamps widths and restores equal sizes', async () => {
    await start();
    const divider = document.querySelector('[role="separator"]');
    const key = (value) => {
      divider.dispatchEvent(
        new KeyboardEvent('keydown', { key: value, bubbles: true }),
      );
      flushSync();
    };
    key('ArrowRight');
    expect(divider.getAttribute('aria-valuenow')).toBe('52');
    key('End');
    expect(divider.getAttribute('aria-valuenow')).toBe(
      divider.getAttribute('aria-valuemax'),
    );
    key('Home');
    expect(divider.getAttribute('aria-valuenow')).toBe(
      divider.getAttribute('aria-valuemin'),
    );
    key('Enter');
    expect(divider.getAttribute('aria-valuenow')).toBe('50');
    expect(localStorage.getItem('vbot.chat.splitRatio')).toBe('50');
  });

  it('opens an HTML output in the other area without replacing its Chat', async () => {
    await start();
    const baseRpc = rpcMock.getMockImplementation();
    rpcMock.mockImplementation((method, params) =>
      method === 'file.preview_open'
        ? Promise.resolve({
            token: 'site-token',
            url: '/api/preview-assets/site-token/index.html',
            source: '/site/index.html',
            root: '/site',
            filename: 'index.html',
            revision: 'one',
          })
        : baseRpc(method, params),
    );
    const markdown = pane(0).querySelector('.msg-markdown');
    const anchor = document.createElement('a');
    anchor.href = '/api/files/file-token';
    anchor.textContent = 'index.html';
    markdown.append(anchor);
    anchor.click();
    await waitForCondition(() => pane(1).querySelector('iframe'), 100);
    expect(rpcMock).toHaveBeenCalledWith('file.preview_open', {
      source: '/api/files/file-token',
    });
    expect(pane(1).querySelector('iframe').getAttribute('sandbox')).toBe(
      'allow-scripts',
    );
    const rightChat = pane(1).querySelector('.chat-view');
    pane(1).querySelector('[role="tab"]').click();
    flushSync();
    expect(pane(1).querySelector('.chat-view')).toBe(rightChat);
    expect(rightChat.textContent).toContain('Second conversation sentinel');
    expect(testChatStateRefs).toHaveLength(2);
  });
});
