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
    root.querySelector(
      `.chat-workspace__body:not([hidden]) button[aria-label="${label}"]`,
    );

  function action(index, label) {
    button(pane(index), 'Area actions').click();
    flushSync();
    const item = [...document.querySelectorAll('[role="menuitem"]')].find(
      (element) => element.textContent.trim() === label,
    );
    expect(item).toBeTruthy();
    item.click();
    flushSync();
  }

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
    action(0, 'Split view');
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
    expect(pane(1).querySelector('.chat-workspace__toolbar')).toBeNull();
    expect(pane(1).querySelector('[role=tablist]')).toBeNull();
    const leftInput = pane(0).querySelector('.msg-input');
    const rightInput = pane(1).querySelector('.msg-input');
    setInputValue(leftInput, 'Left draft sentinel');
    setInputValue(rightInput, 'Right draft sentinel');
    flushSync();
    action(1, 'Close area');
    flushSync();
    expect(pane(1).hidden).toBe(true);
    expect(pane(0).querySelector('.msg-input')).toBe(leftInput);
    action(0, 'Split view');
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
    action(0, 'Split view');
    await waitForCondition(() => testChatStateRefs.length === 2, 100);
    expect(pane(1).querySelector('.msg-input')).toBeNull();
    action(0, 'Close area');
    await waitForCondition(() => pane(1).querySelector('.msg-input'), 100);
    const secondInput = pane(1).querySelector('.msg-input');
    expect(secondInput.value).toBe('Shared Session draft');
    setInputValue(secondInput, 'Continued in the right');
    flushSync();
    action(1, 'Split view');
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
    action(0, 'Split view');
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

  it('keeps area actions inside existing controls and preserves Chat through manual Preview switching', async () => {
    await start();
    const chat = pane(1).querySelector('.chat-view');
    const input = chat.querySelector('.msg-input');
    setInputValue(input, 'Draft survives the content menu');
    flushSync();
    for (const index of [0, 1]) {
      expect(
        pane(index).firstElementChild.classList.contains(
          'chat-workspace__body',
        ),
      ).toBe(true);
      expect(pane(index).querySelector('.chat-workspace__toolbar')).toBeNull();
      expect(pane(index).querySelector('[role="tablist"]')).toBeNull();
      expect(
        button(pane(index), 'Area actions').closest('.chat-view__session-bar'),
      ).not.toBeNull();
    }
    action(1, 'Show preview');
    expect(chat.hidden).toBe(true);
    expect(
      button(pane(1), 'Area actions').closest('.html-preview__address'),
    ).not.toBeNull();
    action(1, 'Back to chat');
    expect(chat.hidden).toBe(false);
    expect(chat.querySelector('.msg-input')).toBe(input);
    expect(input.value).toBe('Draft survives the content menu');
  });

  it('supports menu keyboard navigation, dismissal and focus restoration', async () => {
    await start();
    const trigger = button(pane(0), 'Area actions');
    trigger.focus();
    trigger.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    await waitForCondition(
      () => document.activeElement?.getAttribute('role') === 'menuitem',
      100,
    );
    const menu = document.querySelector('[role="menu"]');
    const items = menu.querySelectorAll('button');
    expect(document.activeElement).toBe(items[0]);
    items[0].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'End', bubbles: true }),
    );
    expect(document.activeElement).toBe(items[items.length - 1]);
    menu.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    flushSync();
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    trigger.click();
    flushSync();
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    flushSync();
    expect(document.querySelector('[role="menu"]')).toBeNull();
    action(0, 'Close area');
    await waitForCondition(
      () => document.activeElement === button(pane(1), 'Area actions'),
      100,
    );
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
    action(1, 'Back to chat');
    flushSync();
    expect(pane(1).querySelector('.chat-view')).toBe(rightChat);
    expect(rightChat.textContent).toContain('Second conversation sentinel');
    expect(testChatStateRefs).toHaveLength(2);
  });
});
