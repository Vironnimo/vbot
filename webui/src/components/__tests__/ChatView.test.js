// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
const subscribeRunEventsMock = vi.fn(() => ({ close: vi.fn(), source: null }));
const listSessionsMock = vi.fn(async () => ({ sessions: [] }));
const linkSessionToChannelMock = vi.fn(async () => ({ ok: true }));

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
  subscribeRunEvents: (...args) => subscribeRunEventsMock(...args),
  listSessions: (...args) => listSessionsMock(...args),
  linkSessionToChannel: (...args) => linkSessionToChannelMock(...args),
}));

const { default: ChatView } = await import('../ChatView.svelte');

describe('ChatView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    subscribeRunEventsMock.mockClear();
    listSessionsMock.mockReset();
    listSessionsMock.mockResolvedValue({ sessions: [] });
    linkSessionToChannelMock.mockReset();
    linkSessionToChannelMock.mockResolvedValue({ ok: true });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('shows the combined input and output usage in the token badge', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat();
    const expectedBadge = `${numberFormat.format(3978)} / ${numberFormat.format(262144)} tok`;

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.textContent?.trim(),
    ).toBe(expectedBadge);
  });

  it('keeps the estimated marker when combined usage is estimated', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92, estimated: true },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat();
    const expectedBadge = `~${numberFormat.format(3978)} / ${numberFormat.format(262144)} tok`;

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.textContent?.trim(),
    ).toBe(expectedBadge);
  });

  it('loads a sub-agent session override and shows it as read-only', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'sub-session-1',
    });
    expect(document.body.textContent).toContain('Viewing a sub-agent session');
    expect(document.body.textContent).toContain('Return to current session');
    expect(document.querySelector('textarea')?.disabled).toBe(true);
  });

  it('returns from a read-only sub-agent session to the current session', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    const returnButton = Array.from(document.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Return to current session',
    );

    expect(returnButton).toBeTruthy();
    returnButton.click();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Hello') &&
        !document.body.textContent.includes('Viewing a sub-agent session'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
    });
    expect(document.querySelector('textarea')?.disabled).toBe(false);
  });

  it('loads selected session history from the sessions drawer', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-1': [
            {
              id: 'assistant-one',
              role: 'assistant',
              content: 'Current session reply',
            },
          ],
          'ch-tg-assistant-12345': [
            {
              id: 'assistant-two',
              role: 'assistant',
              content: 'Telegram session reply',
            },
          ],
        },
      }),
    );
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'ch-tg-assistant-12345',
          created_at: '2026-05-10T11:00:00+00:00',
          last_active_at: '2026-05-11T09:30:00+00:00',
          source_channel_id: 'tg-assistant',
          platform: 'telegram',
          platform_conv_id: '12345',
        },
        {
          id: 'session-1',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Current session reply'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('telegram/12345'),
      100,
    );

    const telegramSessionButton = findButtonByText('telegram/12345');
    expect(telegramSessionButton).toBeTruthy();
    telegramSessionButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('Telegram session reply'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'ch-tg-assistant-12345',
    });
  });

  it('links an unlinked session to a channel from the sessions drawer', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-legacy',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('session-legacy'),
      100,
    );

    const linkButton = findButtonByText('Link to channel');
    expect(linkButton).toBeTruthy();
    linkButton.click();

    await waitForCondition(
      () => Boolean(document.querySelector('input[name="channel-id"]')),
      100,
    );

    const channelIdInput = document.querySelector('input[name="channel-id"]');
    const platformConvIdInput = document.querySelector(
      'input[name="platform-conv-id"]',
    );

    expect(channelIdInput).toBeTruthy();
    expect(platformConvIdInput).toBeTruthy();

    setInputValue(channelIdInput, 'tg-assistant');
    setInputValue(platformConvIdInput, '12345');

    const confirmLinkButton = findButtonByText('Link session');
    expect(confirmLinkButton).toBeTruthy();
    confirmLinkButton.click();

    await waitForCondition(
      () => linkSessionToChannelMock.mock.calls.length > 0,
      100,
    );

    expect(linkSessionToChannelMock).toHaveBeenCalledWith(
      'alpha',
      'session-legacy',
      'tg-assistant',
      '12345',
    );
    expect(document.body.textContent).toContain('Session linked to channel.');
  });
});

function createChatRpcMock({
  usage,
  contextWindow = 262144,
  sessionMessages,
} = {}) {
  const resolvedSessionMessages = {
    'session-1': [
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Hello',
        usage,
      },
    ],
    'sub-session-1': [
      {
        id: 'sub-assistant-one',
        role: 'assistant',
        content: 'Sub-agent response',
      },
    ],
    ...(sessionMessages ?? {}),
  };

  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents: [createAgent({ context_window: contextWindow })] };
    }

    if (method === 'chat.history') {
      const messages = resolvedSessionMessages[params.session_id];
      if (messages) {
        return {
          session_id: params.session_id,
          messages,
        };
      }

      throw new Error(`Unexpected session id: ${params.session_id}`);
    }

    if (method === 'skill.list') {
      return {
        skills: [
          {
            name: 'debugging',
            description: 'Investigate unclear bugs.',
            valid: true,
            warnings: [],
          },
        ],
        invalid_skills: [],
      };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function createAgent(overrides = {}) {
  return {
    id: 'alpha',
    name: 'Alpha',
    model: 'openrouter/anthropic/claude-sonnet-4',
    fallback_model: '',
    connection: 'openrouter:api-key',
    fallback_connection: '',
    workspace: 'C:/agents/alpha',
    temperature: '',
    thinking_effort: '',
    allowed_tools: ['*'],
    allowed_skills: ['*'],
    current_session_id: 'session-1',
    context_window: 262144,
    created_at: '2026-05-09T00:00:00+00:00',
    updated_at: '2026-05-09T00:00:00+00:00',
    ...overrides,
  };
}

function findButtonByText(text) {
  return Array.from(document.querySelectorAll('button')).find((button) =>
    button.textContent.includes(text),
  );
}

function setInputValue(input, value) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

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
