// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';
import {
  createAgent,
  createChatRpcMock,
  findButtonByText,
  findCancelRunButton,
  findNewSessionButton,
  flushSync,
  hoveredContextRingCard,
  listSessionActivityMock,
  rpcCalls,
  rpcMock,
  runEventSource,
  runningRun,
  selectedAgentName,
  sendComposerMessage,
  setInputValue,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  testChatStateRefs,
  testRunStreamRefs,
  waitForCondition,
  waitForText,
} from './ChatView.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const alphaProps = () => ({
  sharedAgents: [createAgent()],
  sharedSelectedAgentId: 'alpha',
});

const RING_CIRCUMFERENCE = 2 * Math.PI * 6;

function ringOffset() {
  const fill = document.querySelector('.context-ring .context-ring__fill');
  return fill ? Number(fill.getAttribute('stroke-dashoffset')) : null;
}

function footerNotices() {
  return Array.from(
    document.querySelectorAll('.chat-view__footer-banner strong'),
  ).map((notice) => notice.textContent.trim());
}

describe('ChatView', () => {
  const chat = setupChatViewTestSuite();

  describe('layout and visibility', () => {
    it.each([
      [undefined, 'comfortable'],
      ['full', 'full'],
    ])(
      'renders chat width %s as %s with the Session controls in the header',
      async (chatWidth, rendered) => {
        rpcMock.mockImplementation(createChatRpcMock());
        await chat.mountChat({ ...alphaProps(), chatWidth });

        // `full` is the opt-out hook: the CSS removes the reading-width cap.
        expect(
          document.querySelector('.chat-view').getAttribute('data-chat-width'),
        ).toBe(rendered);
        expect(findButtonByText(t('sessions.title'))).toBeTruthy();
        expect(findNewSessionButton()).toBeTruthy();
        expect(document.querySelector('.chat-refresh')).toBeNull();
      },
    );

    it('hides presentation while inactive without recreating Chat state or DOM', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      const props = reactiveProps({ active: true, ...alphaProps() });
      await chat.mountChat(props);
      const loads = () => [
        rpcCalls('agent.list').length,
        rpcCalls('chat.history').length,
        listSessionActivityMock.mock.calls.length,
      ];
      const loadsBefore = loads();
      const chatView = document.querySelector('.chat-view');
      const timeline = document.querySelector('.messages');
      timeline.scrollTop = 93;

      props.active = false;
      flushSync();
      expect(document.querySelector('.chat-view')).toBe(chatView);
      expect(chatView.hidden).toBe(true);

      props.active = true;
      flushSync();
      expect(chatView.hidden).toBe(false);
      expect(document.querySelector('.messages')).toBe(timeline);
      expect(timeline.scrollTop).toBe(93);
      expect(document.body.textContent).toContain('Hello');
      expect(testChatStateRefs).toHaveLength(1);
      expect(loads()).toEqual(loadsBefore);
    });

    it('loads the History of an Agent selected elsewhere before Chat was first shown', async () => {
      const beta = createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'session-beta',
      });
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents: [createAgent(), beta],
          sessionMessages: {
            'session-beta': [
              { id: 'beta-message', role: 'user', content: 'Beta history' },
            ],
          },
        }),
      );
      const props = reactiveProps({
        active: false,
        sharedAgents: [createAgent(), beta],
        sharedSelectedAgentId: 'alpha',
      });
      await chat.mountChat(props, { ready: null });
      await waitForCondition(() =>
        rpcCalls('chat.history').some(
          (params) => params.session_id === 'session-1',
        ),
      );

      // Another view chose Beta while the mounted Chat stayed hidden.
      props.sharedSelectedAgentId = 'beta';
      props.active = true;
      flushSync();

      await waitForText('Beta history');
      expect(selectedAgentName()).toBe('Beta');
    });

    it('shows the no-Agents state once a roster refresh removes the last Agent', async () => {
      let agents = [createAgent()];
      const baseRpc = createChatRpcMock();
      rpcMock.mockImplementation(async (method, params) =>
        method === 'agent.list' ? { agents } : baseRpc(method, params),
      );
      const props = reactiveProps({ ...alphaProps(), agentsRefreshToken: 0 });
      await chat.mountChat(props);
      expect(document.body.textContent).not.toContain(t('chat.noAgents'));

      agents = [];
      props.sharedAgents = [];
      props.agentsRefreshToken += 1;
      await waitForCondition(() =>
        document.body.textContent.includes(t('chat.noAgents')),
      );
      expect(document.querySelector('.msg-input')).toBeNull();
      expect(selectedAgentName()).toBe('');
    });

    it('waits before showing initial History feedback', async () => {
      const HISTORY_LOADING_FEEDBACK_DELAY_MS = 300;
      const loading = t('loading.history');
      let resolveHistory;
      const defaultRpc = createChatRpcMock();
      rpcMock.mockImplementation((method, params) =>
        method === 'chat.history'
          ? new Promise((resolve) => {
              resolveHistory = resolve;
            })
          : defaultRpc(method, params),
      );
      vi.useFakeTimers();

      await chat.mountChat({}, { ready: null });
      await vi.advanceTimersByTimeAsync(HISTORY_LOADING_FEEDBACK_DELAY_MS - 1);
      flushSync();
      expect(document.body.textContent).not.toContain(loading);

      await vi.advanceTimersByTimeAsync(1);
      flushSync();
      expect(document.body.textContent).toContain(loading);

      resolveHistory({
        active_run: null,
        has_more: false,
        messages: [],
        session_id: 'session-1',
      });
      await vi.advanceTimersByTimeAsync(0);
      flushSync();
      expect(document.body.textContent).not.toContain(loading);
    });

    it.each([
      [
        'no provider',
        { hasConnectedProvider: false, model: '' },
        ['chat.noProvider.title', 'Connect a provider to start'],
        ['chat.noProvider.action', 'onConnectProvider'],
      ],
      [
        'no model',
        { hasConnectedProvider: true, model: '' },
        ['chat.noModel.title', 'Pick a model to start'],
        ['chat.noModel.action', 'onPickModel'],
      ],
      ['unknown provider state', { hasConnectedProvider: null, model: '' }],
      ['a model', { hasConnectedProvider: true }],
    ])(
      'shows the setup notice for %s',
      async (_case, { hasConnectedProvider, model }, notice, action) => {
        const agent = createAgent(model === undefined ? {} : { model });
        const callbacks = { onConnectProvider: vi.fn(), onPickModel: vi.fn() };
        rpcMock.mockImplementation(createChatRpcMock({ agents: [agent] }));
        await chat.mountChat({
          sharedAgents: [agent],
          sharedSelectedAgentId: 'alpha',
          hasConnectedProvider,
          ...callbacks,
        });

        // Provider setup comes before model selection; without provider
        // state Chat does not guess which prerequisite is missing.
        const expected = notice ? [t(...notice)] : [];
        await waitForCondition(
          () => footerNotices().length === expected.length,
        );
        expect(footerNotices()).toEqual(expected);
        if (!action) return;
        const [actionKey, callback] = action;
        findButtonByText(t(actionKey)).click();
        flushSync();
        expect(callbacks[callback]).toHaveBeenCalledTimes(1);
        expect(
          Object.values(callbacks).filter((fn) => fn.mock.calls.length),
        ).toHaveLength(1);
      },
    );
  });

  describe('context usage', () => {
    it.each([
      ['measured', { input_tokens: 3886, output_tokens: 92 }],
      [
        // The estimated flag only changes the hover text.
        'estimated',
        {
          input_tokens: 3886,
          input_tokens_estimated: true,
          output_tokens: 92,
          output_tokens_estimated: true,
          estimated: true,
        },
      ],
    ])(
      'renders the context ring fill for %s Context Usage',
      async (_case, usage) => {
        rpcMock.mockImplementation(createChatRpcMock({ usage }));
        await chat.mountChat();

        await waitForCondition(() => ringOffset() !== null);
        expect(ringOffset()).toBeCloseTo(
          RING_CIRCUMFERENCE * (1 - 3978 / 262144),
          1,
        );
      },
    );

    it('does not render the context ring without a context window', async () => {
      // A Session whose Context names no window has no fill ratio, never a
      // NaN arc, even though its Agent's Model has a window.
      rpcMock.mockImplementation(
        createChatRpcMock({
          usage: { input_tokens: 3886, output_tokens: 92 },
          contextWindow: null,
        }),
      );
      await chat.mountChat();

      expect(document.body.querySelector('.context-ring')).toBeNull();
    });

    it('refreshes Context Usage after a model step while the Run continues', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({ usage: { input_tokens: 1000, output_tokens: 50 } }),
      );
      await chat.mountChat();
      const run = {
        run_id: 'run-one',
        agent_id: 'alpha',
        project_id: null,
        session_id: 'session-1',
      };
      testRunStreamRefs[0].handleServerEvents({
        type: 'run_started',
        payload: {
          ...run,
          run_event_type: 'run_started',
          run_event_sequence: 1,
          output: { status: 'running' },
        },
      });
      testRunStreamRefs[0].handleServerEvents({
        type: 'run_output',
        payload: {
          ...run,
          run_event_type: 'model_step_usage',
          run_event_sequence: 2,
          output: {
            usage: { input_tokens: 3886, output_tokens: 92 },
            session_usage: {
              input_tokens: 4886,
              output_tokens: 142,
            },
            context_usage: {
              tokens: 4050,
              estimated: true,
              provider_input_tokens: 3886,
              provider_output_tokens: 92,
              estimated_delta_tokens: 72,
            },
          },
        },
      });
      flushSync();

      const expectedOffset = RING_CIRCUMFERENCE * (1 - 4050 / 262144);
      await waitForCondition(
        () => Math.abs(ringOffset() - expectedOffset) < 0.5,
      );
      expect(findCancelRunButton()).toBeTruthy();
    });

    it.each([
      [
        'with cache usage',
        {
          usage: {
            input_tokens: 3886,
            output_tokens: 92,
            cache_read_tokens: 3000,
            cache_write_tokens: 200,
          },
          sessionUsage: {
            input_tokens: 40000,
            output_tokens: 1500,
            cache_read_tokens: 32000,
            cache_write_tokens: 900,
          },
        },
        [
          {
            title: '',
            rows: [
              'Cache hit rate: 80%',
              'Total input: 40,000',
              'Total output: 1,500',
            ],
          },
          {
            title: 'Last turn',
            rows: [
              'Input: 3,886',
              '· Read from cache: 3,000 (77%)',
              '· Written to cache: 200',
              '· Uncached: 686',
              'Output: 92',
            ],
          },
        ],
      ],
      [
        'without cache usage',
        { usage: { input_tokens: 3886, output_tokens: 92 } },
        [
          {
            title: 'Last turn',
            rows: [
              'Input: 3,886',
              '· Read from cache: 0 (0%)',
              '· Written to cache: 0',
              '· Uncached: 3,886',
              'Output: 92',
            ],
          },
        ],
      ],
    ])(
      'breaks down last-turn and Session usage %s in the ring hover card',
      async (_case, usage, sections) => {
        rpcMock.mockImplementation(createChatRpcMock(usage));
        await chat.mountChat();

        expect(await hoveredContextRingCard()).toEqual({
          summary: '3,978 / 262,144',
          sections,
        });
      },
    );
  });

  describe('composer feedback', () => {
    it.each([
      ['global', 'historyError'],
      ['global', 'actionError'],
      ['global', 'commandsError'],
      ['session', 'actionError'],
      ['session', 'streamError'],
    ])(
      'keeps a %s %s beside the composer until cleared',
      async (scope, field) => {
        rpcMock.mockImplementation(createChatRpcMock());
        await chat.mountChat();
        const state = testChatStateRefs[0];
        const owner =
          scope === 'global' ? state : Object.values(state.sessions)[0];
        owner[field] = `test-${scope}-${field}`;
        flushSync();
        const notice = document.querySelector('.chat-view__composer-feedback');
        expect(notice.closest('.chat-view__footer-stack')).not.toBeNull();
        expect(notice.textContent).toContain(owner[field]);
        expect(document.querySelector('.chat-view__notice-stack')).toBeNull();
        owner[field] = '';
        flushSync();
        expect(
          document.querySelector('.chat-view__composer-feedback'),
        ).toBeNull();
      },
    );

    it('keeps a rejected send visible beside the unchanged draft', async () => {
      const failure = 'test-admission-failure';
      rpcMock.mockImplementation(
        createChatRpcMock({
          streamHandler: () => {
            throw new Error(failure);
          },
        }),
      );
      await chat.mountChat();
      sendComposerMessage('test-unsent-draft');
      await waitForText(failure);
      expect(
        document.querySelector('.chat-view__composer-feedback').textContent,
      ).toContain(failure);
      expect(document.querySelector('textarea').value).toBe(
        'test-unsent-draft',
      );
      expect(document.querySelector('.msg--error')).toBeNull();
    });

    it.each([false, true])(
      'shows a Run failure once in the timeline, persisted error: %s',
      async (persisted) => {
        const body = {
          error: {
            message: 'test-provider-failure',
            metadata: { remedy_hint: 'test-remedy' },
          },
        };
        const error = `Rate limited: 429 ${JSON.stringify(body)}`;
        rpcMock.mockImplementation(
          createChatRpcMock({
            sessionMessages: { 'session-1': [] },
            streamResponse: runningRun('failed-run'),
          }),
        );
        await chat.mountChat({}, { ready: null });
        await waitForCondition(
          () => document.querySelector('textarea')?.disabled === false,
        );
        sendComposerMessage('test-failing-request');
        await waitForCondition(
          () => subscribeRunEventsMock.mock.calls.length === 1,
        );
        const events = runEventSource('failed-run');
        if (persisted)
          events.emit('error_message_persisted', {
            message: { id: 'test-error', role: 'error', content: error },
          });
        events.emit('run_failed', { status: 'failed', error });
        await waitForCondition(() => document.querySelector('.error-details'));
        expect(document.querySelectorAll('.error-details')).toHaveLength(1);
        const details = document.querySelector('.error-details');
        expect(details.closest('.chat-view__timeline-shell')).not.toBeNull();
        details.querySelector('summary').click();
        expect(details.open).toBe(true);
        expect(JSON.parse(details.querySelector('pre').textContent)).toEqual(
          body,
        );
        expect(document.querySelector('.chat-view__notice-stack')).toBeNull();
        expect(
          document.querySelector('.chat-view__composer-feedback'),
        ).toBeNull();
      },
    );
  });

  describe('composer suggestions', () => {
    const commands = ['stop', 'help', 'status', 'reset', 'retry'];
    const skills = [
      'debugging',
      'ctx7',
      'refactoring',
      'playwright-cli',
      'frontend-design',
      'glossary',
      'debug',
    ];
    const commandItems = [
      ...commands.map((name) => ({
        name,
        description: `${name} command`,
        type: 'command',
      })),
      ...skills.map((name) => ({
        name,
        description: `${name} skill`,
        type: 'skill',
      })),
    ];

    it.each([
      [
        'every command and Skill for an empty slash query',
        '/',
        [...commands, ...skills],
        ['skillAutocomplete.eyebrow.commandsAndSkills', 'Commands & skills'],
      ],
      [
        'only Skills for an inline dollar query',
        'Use $',
        skills,
        ['skillAutocomplete.eyebrow.skills', 'Skills'],
      ],
    ])('suggests %s', async (_case, text, names, eyebrow) => {
      rpcMock.mockImplementation(createChatRpcMock({ commandItems }));
      await chat.mountChat(alphaProps());
      // The catalog is requested for the active Agent address, so the server
      // returns that Agent's effective (Project-scoped) Skills.
      expect(rpcCalls('chat.commands')).toContainEqual(
        expect.objectContaining({ agent_id: 'alpha' }),
      );

      const input = document.querySelector('.msg-input');
      setInputValue(input, text);
      input.setSelectionRange(text.length, text.length);
      input.dispatchEvent(new Event('keyup', { bubbles: true }));
      const optionNames = () =>
        Array.from(document.querySelectorAll('.skill-autocomplete__name')).map(
          (element) => element.textContent.trim(),
        );
      await waitForCondition(() => optionNames().length === names.length);

      expect(optionNames()).toEqual(names);
      expect(
        document.querySelector('.skill-autocomplete__eyebrow').textContent,
      ).toContain(t(...eyebrow));
    });
  });
});
