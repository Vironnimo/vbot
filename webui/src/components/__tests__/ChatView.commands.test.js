// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';
import {
  contextCompactionButton,
  createAgent,
  createChatRpcMock,
  findCancelRunButton,
  flushSync,
  handledCommand,
  historyReads,
  message,
  rpcCalls,
  rpcMock,
  runEventSource,
  runningRun,
  selectAgentFromPicker,
  selectedAgentName,
  sendComposerMessage,
  settle,
  setupChatViewTestSuite,
  streamResponses,
  subscribeRunEventsMock,
  testChatStateRefs,
  waitForCondition,
  waitForText,
} from './ChatView.support.js';
import { createChatViewParentHarness } from './ChatView.parent.support.svelte.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const toastText = () =>
  document.querySelector('.chat-view__command-toast')?.textContent.trim();
const queuedTexts = () =>
  Array.from(document.querySelectorAll('.queued-messages__content')).map(
    (item) => item.textContent.trim(),
  );
const streamedContents = () =>
  rpcCalls('chat.stream').map((params) => params.content);

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return [promise, resolve];
}

function queuedItem(id, content) {
  return {
    queued: true,
    item: { id, content, created_at: '2026-05-22T10:00:00+00:00' },
  };
}

describe('ChatView slash commands', () => {
  const chat = setupChatViewTestSuite();

  describe('handled command output', () => {
    it('shows a toast reply without subscribing to a Run', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          streamResponse: handledCommand('Run cancelled.', { output: 'toast' }),
        }),
      );
      await chat.mountChat();

      sendComposerMessage('/stop');
      await waitForCondition(() => toastText() === 'Run cancelled.');

      expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
        agent_id: 'alpha',
        session_id: 'session-1',
        content: '/stop',
      });
      expect(subscribeRunEventsMock).not.toHaveBeenCalled();
    });

    it('stacks transient cards so successive snapshots can be compared', async () => {
      const statusReply =
        'Agent: Alpha\nModel: claude-sonnet-4\nSession started: 2026-05-19';
      rpcMock.mockImplementation(
        createChatRpcMock({
          commandItems: [
            {
              name: 'status',
              description: 'Show current agent and session status.',
              type: 'command',
              argument: 'none',
              output: 'transient',
            },
          ],
          streamHandler: streamResponses({
            '/status': handledCommand(statusReply, { output: 'transient' }),
          }),
        }),
      );
      await chat.mountChat();

      sendComposerMessage('/status');
      await waitForCondition(
        () => document.querySelectorAll('.transient-card').length === 1,
      );
      const card = document.querySelector('.transient-card__body');
      expect(card.textContent).toContain(statusReply);
      sendComposerMessage('/status');
      await waitForCondition(
        () => document.querySelectorAll('.transient-card').length === 2,
      );

      // Transient output is never echoed into the bottom toast.
      expect(toastText()).toBeUndefined();
      expect(streamedContents()).toEqual(['/status', '/status']);
      expect(subscribeRunEventsMock).not.toHaveBeenCalled();
    });

    it('drops a stale transient command result after navigating away and back', async () => {
      const [statusResponse, resolveStatus] = deferred();
      const agents = [
        createAgent(),
        createAgent({
          id: 'beta',
          name: 'Beta',
          current_session_id: 'session-beta',
        }),
      ];
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents,
          sessionMessages: {
            'session-beta': [message('beta-reply', 'Beta reply')],
          },
          commandItems: [
            {
              name: 'status',
              description: 'Show status.',
              type: 'command',
              argument: 'none',
              output: 'transient',
            },
          ],
          streamHandler: streamResponses({ '/status': statusResponse }),
        }),
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(parent.props(['agent'], { sharedAgents: agents }));

      sendComposerMessage('/status');
      await selectAgentFromPicker('Beta');
      await waitForText('Beta reply');
      await selectAgentFromPicker('Alpha');
      await waitForCondition(() => selectedAgentName() === 'Alpha');

      resolveStatus(
        handledCommand('Stale Alpha status', { output: 'transient' }),
      );
      await settle();

      expect(selectedAgentName()).toBe('Alpha');
      expect(document.querySelector('.transient-card')).toBeNull();
    });
  });

  describe('navigation commands', () => {
    it.each([
      [
        '/handoff',
        {
          command: 'handoff',
          session_id: 'session-handoff',
          agent_id: 'alpha',
        },
        ['alpha', 'Alpha'],
      ],
      [
        '/handoff beta',
        { command: 'handoff', session_id: 'session-handoff', agent_id: 'beta' },
        ['beta', 'Beta'],
      ],
      [
        // `/agent` moves the current Session: the same Session id opens under
        // the target Agent.
        '/agent beta',
        { command: 'agent', session_id: 'session-1', agent_id: 'beta' },
        ['beta', 'Beta'],
      ],
    ])(
      '%s opens the Session its result names',
      async (content, data, [agentId, agentName]) => {
        const agents = [
          createAgent(),
          createAgent({
            id: 'beta',
            name: 'Beta',
            current_session_id: 'beta-current',
          }),
        ];
        rpcMock.mockImplementation(
          createChatRpcMock({
            agents,
            sessionMessages: {
              'session-handoff': [message('handoff-reply', 'Handoff reply')],
              'beta-current': [message('beta-reply', 'Beta current reply')],
            },
            streamHandler: streamResponses({
              [content]: handledCommand('Switched.', {
                output: data.command === 'agent' ? 'action' : undefined,
                data,
              }),
            }),
          }),
        );
        // App's flow: `onAgentSelected` returns as `sharedSelectedAgentId`.
        const parent = createChatViewParentHarness();
        await chat.mountChat(parent.props(['agent'], { sharedAgents: agents }));

        sendComposerMessage(content);
        await waitForCondition(
          () =>
            historyReads(data.session_id, agentId) > 0 &&
            selectedAgentName() === agentName,
        );

        // An action command neither toasts nor shows a transient card.
        expect(toastText()).toBeUndefined();
        expect(document.querySelector('.transient-card')).toBeNull();
        expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
          agent_id: 'alpha',
          session_id: 'session-1',
          content,
        });
        expect(rpcMock).toHaveBeenCalledWith('chat.history', {
          agent_id: agentId,
          session_id: data.session_id,
          limit: 100,
        });
        expect(historyReads('beta-current')).toBe(0);
        expect(subscribeRunEventsMock).not.toHaveBeenCalled();
      },
    );

    it('shows a draft for /new, keeps it for a command without a Session and creates the Session with the next message', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          streamHandler: streamResponses({
            '/new': handledCommand('', { data: { command: 'new' } }),
            '/status': handledCommand('Draft status', {
              output: 'transient',
              data: { command: 'status', session_id: null },
            }),
            'First draft message': {
              ...runningRun('run-new'),
              session_id: 'created-alpha',
            },
          }),
        }),
      );
      await chat.mountChat();
      const alphaSessionId = () =>
        testChatStateRefs[0].agents[0].current_session_id;

      sendComposerMessage('/new');
      await waitForCondition(() => alphaSessionId() === '');
      expect(document.body.textContent).not.toContain('Hello');
      expect(toastText()).toBeUndefined();

      sendComposerMessage('/status');
      await waitForCondition(() =>
        document
          .querySelector('.transient-card')
          ?.textContent.includes('Draft status'),
      );
      expect(alphaSessionId()).toBe('');

      sendComposerMessage('First draft message');
      await waitForCondition(() => alphaSessionId() === 'created-alpha');
      expect(rpcCalls('chat.stream')).toEqual([
        { agent_id: 'alpha', session_id: 'session-1', content: '/new' },
        { agent_id: 'alpha', new_session: {}, content: '/status' },
        { agent_id: 'alpha', new_session: {}, content: 'First draft message' },
      ]);
    });

    it('does not apply stale command navigation after the user selects another Agent', async () => {
      const [moveResponse, resolveMove] = deferred();
      const agents = [
        createAgent({ current_session_id: 'shared-session' }),
        createAgent({
          id: 'beta',
          name: 'Beta',
          current_session_id: 'beta-session',
        }),
        createAgent({
          id: 'gamma',
          name: 'Gamma',
          current_session_id: 'gamma-session',
        }),
      ];
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents,
          sessionMessages: {
            'shared-session': [message('alpha-reply', 'Alpha reply')],
            'gamma-session': [message('gamma-reply', 'Gamma reply')],
          },
          streamHandler: streamResponses({ '/agent beta': moveResponse }),
        }),
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(parent.props(['agent'], { sharedAgents: agents }), {
        ready: 'Alpha reply',
      });

      sendComposerMessage('/agent beta');
      await selectAgentFromPicker('Gamma');
      await waitForText('Gamma reply');
      resolveMove(
        handledCommand('Moved to beta.', {
          output: 'action',
          data: {
            command: 'agent',
            session_id: 'shared-session',
            agent_id: 'beta',
          },
        }),
      );
      await settle();

      expect(selectedAgentName()).toBe('Gamma');
      expect(historyReads('shared-session', 'beta')).toBe(0);
    });

    it('does not publish delayed Agent navigation after Chat becomes inactive', async () => {
      const beta = createAgent({ id: 'beta', name: 'Beta' });
      const [moveResponse, resolveMove] = deferred();
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents: [createAgent(), beta],
          streamHandler: streamResponses({ '/agent beta': moveResponse }),
        }),
      );
      const onAgentSelected = vi.fn();
      const props = reactiveProps({
        active: true,
        sharedAgents: [createAgent(), beta],
        sharedSelectedAgentId: 'alpha',
        onAgentSelected,
      });
      await chat.mountChat(props);
      onAgentSelected.mockClear();

      sendComposerMessage('/agent beta');
      await waitForCondition(() => streamedContents().includes('/agent beta'));
      props.active = false;
      flushSync();
      resolveMove(
        handledCommand('Moved to beta.', {
          output: 'action',
          data: { command: 'agent', session_id: 'session-1', agent_id: 'beta' },
        }),
      );
      await waitForCondition(() => selectedAgentName() === 'Beta');
      expect(onAgentSelected).not.toHaveBeenCalled();

      // Becoming visible again restores the selection App still holds.
      props.active = true;
      flushSync();
      await waitForCondition(() => selectedAgentName() === 'Alpha');
      expect(onAgentSelected).toHaveBeenCalledTimes(1);
      expect(onAgentSelected).toHaveBeenCalledWith('alpha');
    });
  });

  describe('during an active Run', () => {
    it.each([false, true])(
      'queues Skill triggers and messages while /stop bypasses the Queue (command catalog unavailable: %s)',
      async (commandsError) => {
        const skillTrigger = '/debugging investigate this run';
        rpcMock.mockImplementation(
          createChatRpcMock({
            commandsError,
            streamHandler: streamResponses({
              'Start a long run': runningRun('run-1'),
              [skillTrigger]: queuedItem('queued-skill', skillTrigger),
              'Queue this while running': queuedItem(
                'queued-message',
                'Queue this while running',
              ),
              '/stop': handledCommand('Run cancelled.'),
            }),
          }),
        );
        await chat.mountChat();

        sendComposerMessage('Start a long run');
        await waitForCondition(() => Boolean(findCancelRunButton()));
        sendComposerMessage(skillTrigger);
        await waitForCondition(() => queuedTexts().length === 1);
        sendComposerMessage('Queue this while running');
        await waitForCondition(() => queuedTexts().length === 2);
        sendComposerMessage('/stop');
        await waitForCondition(() => toastText() === 'Run cancelled.');

        expect(streamedContents()).toEqual([
          'Start a long run',
          skillTrigger,
          'Queue this while running',
          '/stop',
        ]);
        expect(queuedTexts()).toEqual([
          skillTrigger,
          'Queue this while running',
        ]);
        expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
      },
    );

    it('recognizes /compact when command metadata includes a leading slash', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          commandItems: [
            {
              name: '/compact',
              description: 'Compact the current session context.',
              type: 'command',
            },
            {
              name: 'debugging',
              description: 'Investigate unclear bugs.',
              type: 'skill',
            },
          ],
          streamHandler: streamResponses({
            'Start a long run': runningRun('run-compact-1'),
            '/compact': handledCommand('Context compacted.'),
          }),
        }),
      );
      await chat.mountChat();

      sendComposerMessage('Start a long run');
      await waitForCondition(() => Boolean(findCancelRunButton()));
      sendComposerMessage('/compact');
      await waitForCondition(() => toastText() === 'Context compacted.');

      expect(streamedContents()).toEqual(['Start a long run', '/compact']);
      expect(queuedTexts()).toEqual([]);
      expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
    });

    it('explains when a server restart discards a locally shown queued message', async () => {
      const parent = createChatViewParentHarness();
      rpcMock.mockImplementation(
        createChatRpcMock({
          streamHandler: streamResponses({
            'Start before restart': runningRun('run-before-restart'),
            'Queue before restart': queuedItem(
              'queued-before-restart',
              'Queue before restart',
            ),
          }),
        }),
      );
      await chat.mountChat(parent.props(['connection']));

      sendComposerMessage('Start before restart');
      await waitForCondition(() => Boolean(findCancelRunButton()));
      sendComposerMessage('Queue before restart');
      await waitForCondition(() => queuedTexts().length === 1);
      parent.setConnectionSnapshot({
        type: 'connection_ready',
        epoch: 'epoch-after-restart',
        replay_status: 'epoch_changed',
        active_runs: [],
        queues: [],
      });
      flushSync();

      await waitForCondition(
        () => toastText() === t('queue.restartDiscardedOne'),
      );
      expect(queuedTexts()).toEqual([]);
    });
  });

  describe('Compaction from the context card', () => {
    const usage = { input_tokens: 3886, output_tokens: 92 };

    it('starts a manual Compaction Run that renders the live checkpoint lifecycle', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          usage,
          streamHandler: streamResponses({
            '/compact': runningRun('run-manual-compaction', [
              {
                type: 'run_started',
                run_id: 'run-manual-compaction',
                sequence: 1,
                payload: { status: 'running' },
              },
              {
                type: 'compaction_started',
                run_id: 'run-manual-compaction',
                sequence: 2,
                payload: {},
              },
            ]),
          }),
        }),
      );
      await chat.mountChat();
      await waitForCondition(() =>
        document.querySelector('.context-ring-trigger'),
      );
      const historyReadsBefore = historyReads('session-1');

      const action = contextCompactionButton();
      expect(action.textContent.trim()).toBe(t('chat.compactNow'));
      expect(action.disabled).toBe(false);
      action.click();
      await waitForCondition(
        () => action.textContent.trim() === t('chat.compactionRunning'),
      );
      expect(action.disabled).toBe(true);
      expect(streamedContents()).toEqual(['/compact']);
      expect(rpcCalls('chat.control_run')).toEqual([]);

      // Manual Compaction renders bare, as the same checkpoint divider as
      // Auto-Compaction, from the Run's lifecycle events and without
      // reloading History.
      await waitForCondition(
        () =>
          document
            .querySelector('.compaction-sep--running')
            ?.textContent.trim() === t('chat.compactingCurrentConversation'),
      );
      expect(historyReads('session-1')).toBe(historyReadsBefore);
      expect(subscribeRunEventsMock).toHaveBeenCalledWith(
        '/api/runs/run-manual-compaction/events',
        expect.any(Object),
        { afterSequence: 0 },
      );
      const events = runEventSource('run-manual-compaction');
      events.emit(
        'compaction_completed',
        {
          message: {
            id: 'checkpoint-manual',
            role: 'compaction_checkpoint',
            content: 'Exact manual compaction context',
            usage: {
              context_tokens_before: 69_030,
              context_tokens_after: 26_835,
            },
          },
          checkpoint: 1,
          checkpoint_id: 'checkpoint-manual',
          context_tokens_before: 69_030,
          context_tokens_after: 26_835,
        },
        3,
      );
      events.emit('run_completed', { status: 'completed' });

      await waitForCondition(() =>
        document
          .querySelector('.compaction-sep')
          ?.textContent.includes('~69k → ~27k'),
      );
      expect(document.querySelector('.compaction-sep--running')).toBeNull();
      const disclosure = document.querySelector('.compaction-disclosure');
      disclosure.open = true;
      flushSync();
      expect(
        disclosure.querySelector('.compaction-detail__text').textContent,
      ).toBe('Exact manual compaction context');
    });

    it('requests Compaction from the active Run and follows its control state', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          usage,
          streamResponse: runningRun('run-card-control'),
        }),
      );
      await chat.mountChat();
      sendComposerMessage('Start a long run');
      await waitForCondition(
        () => subscribeRunEventsMock.mock.calls.length === 1,
      );
      const events = runEventSource('run-card-control');
      const controls = (compaction) =>
        events.emit('run_controls_changed', {
          compaction,
          background_tool_call_ids: [],
        });

      controls('unavailable');
      flushSync();
      const action = contextCompactionButton();
      expect(action.disabled).toBe(true);

      controls('idle');
      flushSync();
      expect(action.textContent.trim()).toBe(t('chat.compactNow'));
      expect(action.disabled).toBe(false);
      action.click();
      await waitForCondition(() => rpcCalls('chat.control_run').length === 1);
      expect(rpcMock).toHaveBeenCalledWith('chat.control_run', {
        agent_id: 'alpha',
        session_id: 'session-1',
        run_id: 'run-card-control',
        action: 'compact',
      });

      for (const [state, label] of [
        ['pending', 'chat.compactionPending'],
        ['running', 'chat.compactionRunning'],
      ]) {
        controls(state);
        flushSync();
        expect(action.textContent.trim()).toBe(t(label));
        expect(action.disabled).toBe(true);
      }
    });
  });
});
