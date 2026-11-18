// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { createRunEventFeed } from '../../lib/runEventFeed.svelte.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import {
  createChatRpcMock,
  flushSync,
  message,
  rpcCalls,
  rpcMock,
  runningRun,
  runServerEvent,
  subscribeRunEventsMock,
  sendComposerMessage,
  setInputValue,
  setupChatViewTestSuite,
  testChatStateRefs,
  waitForCondition,
  waitForText,
} from './ChatView.support.js';
import { t } from '../../lib/i18n.js';

const { mount, unmount } = await import('svelte');
const { default: SessionView } = await import('../SessionView.svelte');

describe('SessionView', () => {
  const chat = setupChatViewTestSuite();

  it('inspects the addressed Project Session without selecting an Agent and opens that exact address in Chat', async () => {
    const runEvents = createRunEventFeed();
    const open = vi.fn();
    const context = new Map([
      ['sessionViewEvents', () => ({ runEvents, onNavigateToSession: open })],
    ]);
    const address = {
      agent_id: 'participant',
      project_id: 'docs',
      session_id: 'session-1',
    };
    const props = reactiveProps({
      address,
      readOnly: true,
      presentation: 'full',
    });
    const agentRpc = createChatRpcMock({
      activeRuns: { 'session-1': runningRun('live-participant') },
    });
    rpcMock.mockImplementation(async (method, params) => {
      const result = await agentRpc(method, params);
      return method === 'chat.timeline' &&
        params.agent_id === 'participant@docs'
        ? { ...result, agent_name: 'Release reviewer' }
        : result;
    });
    chat.mount({ target: document.body, context, props }, SessionView);
    await waitForText('Hello');
    expect(
      document.querySelector('.chat-view').getAttribute('aria-label'),
    ).toBe('Release reviewer');
    expect(rpcCalls('agent.list')).toHaveLength(0);
    expect(rpcCalls('chat.timeline')).toEqual([
      expect.objectContaining({
        agent_id: 'participant@docs',
        session_id: 'session-1',
      }),
    ]);
    expect(subscribeRunEventsMock.mock.calls[0][0]).toContain(
      '/live-participant/timeline',
    );
    const stream = subscribeRunEventsMock.mock.results[0].value;
    expect(document.querySelector('.msg-input')).toBeNull();
    expect(open).not.toHaveBeenCalled();
    const button = [...document.querySelectorAll('button')].find((b) =>
      b.textContent.includes(t('appChat.openFull')),
    );
    button.click();
    expect(open).toHaveBeenCalledWith(address);
    runEvents.append(
      runServerEvent('run_started', {
        sequence: 1,
        agent_id: 'other',
        session_id: 'other',
        run_id: 'unrelated',
      }),
    );
    flushSync();
    expect(Object.keys(testChatStateRefs[0].sessions)).toEqual([
      'participant@docs::session-1',
    ]);
    props.address = { agent_id: 'child', session_id: 'sub-session-1' };
    await waitForText('Sub-agent response');
    expect(
      document.querySelector('.chat-view').getAttribute('aria-label'),
    ).toBe('child');
    expect(stream.close).toHaveBeenCalled();
    expect(document.body.textContent).not.toContain('Hello');
  });

  it('keeps an already-mounted full Chat in sync with the draft typed in a popup', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    await chat.mountChat();
    const full = document.querySelector('.msg-input');
    const host = document.createElement('div');
    document.body.append(host);
    const pane = mount(SessionView, {
      target: host,
      props: { address: { agent_id: 'alpha', session_id: 'session-1' } },
    });
    try {
      await waitForText('Hello', host);
      const popup = host.querySelector('.msg-input');
      full.focus();
      setInputValue(full, 'Draft from full chat');
      flushSync();
      expect(popup.value).toBe('Draft from full chat');
      setInputValue(popup, 'Continue in full chat');
      flushSync();
      expect(full.value).toBe('Continue in full chat');
      setInputValue(popup, '/secret example -- a-local-only-value');
      flushSync();
      expect(full.value).toBe('');
      expect(popup.value).toContain('a-local-only-value');
    } finally {
      await unmount(pane);
    }
  });

  it('captures the addressed Session and current UI context once per submission', async () => {
    const address = {
      agent_id: 'settings-specialist',
      session_id: 'session-1',
    };
    const location = { view: 'settings', field: 'first' };
    const getUIContext = vi.fn(() => location);
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: { 'session-1': [message('prompt', 'Welcome')] },
        streamResponse: { accepted: true, session_available: false },
      }),
    );
    chat.mount(
      {
        target: document.body,
        props: { address, getUIContext, presentation: 'full' },
      },
      SessionView,
    );
    await waitForText('Welcome');
    expect(getUIContext).not.toHaveBeenCalled();
    getUIContext.mockImplementationOnce(() => {
      throw new Error('Page closed');
    });
    sendComposerMessage('What does this do?');
    await waitForText('Page closed');
    expect(document.querySelector('.msg-input').value).toBe(
      'What does this do?',
    );
    expect(rpcCalls('chat.stream')).toHaveLength(0);
    location.field = 'timezone';
    sendComposerMessage('What does this do?');
    location.field = 'another-field';
    await waitForCondition(() => rpcCalls('chat.stream').length > 0);
    expect(getUIContext).toHaveBeenCalledTimes(2);
    expect(rpcCalls('chat.stream')[0]).toMatchObject({
      ...address,
      ui_context: { view: 'settings', field: 'timezone' },
    });
  });
});
