import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { setupController } from './chatState.support.js';
import {
  assistantRun,
  messageItem,
  outputItem,
} from '../../components/__tests__/timelineItems.support.js';
const saved = (sequence, run, role, content) => ({
  id: 'message-' + sequence,
  history_sequence: sequence,
  history_run_id: run,
  role,
  content,
});

describe('Session client state', () => {
  it('does not expose internal continuation data as client state', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-continuation',
    );
    const continuation = {
      checkpoint_id: 'checkpoint-one',
      cause: 'network',
    };

    loadHistory(sessionState, [], { continuation });
    expect(sessionState).not.toHaveProperty('continuation');

    startRun(sessionState, {
      run_id: 'run-two',
      sse_url: '/runs/run-two',
      status: 'running',
    });
    appendRunEvent(sessionState, {
      type: 'run_failed',
      run_id: 'run-two',
      sequence: 1,
      payload: { status: 'failed', continuation },
    });
    expect(sessionState).not.toHaveProperty('continuation');
  });

  it('prepends older history without duplicating loaded messages', async () => {
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [
          { id: 'message-three', role: 'user', content: 'Three' },
          { id: 'message-four', role: 'assistant', content: 'Four' },
        ],
        has_more: true,
      })
      .mockResolvedValueOnce({
        messages: [
          { id: 'message-one', role: 'user', content: 'One' },
          { id: 'message-two', role: 'assistant', content: 'Two' },
          { id: 'message-three', role: 'user', content: 'Three duplicate' },
          { id: 'note-one', role: 'note', content: 'Internal note' },
        ],
        has_more: false,
      });
    const { chatState, controller } = setupController({
      operationOverrides: { loadChatHistory },
    });
    await controller.loadHistoryForSession('alpha', 'session-one');
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    expect(await controller.loadOlderHistory(sessionState)).toBe(true);

    expect(sessionState.messages.map((message) => message.id)).toEqual([
      'message-one',
      'message-two',
      'message-three',
      'message-four',
    ]);
    expect(sessionState.hasOlderHistory).toBe(false);
  });

  it('filters internal notes from loaded history and visible timeline', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(
      sessionState,
      [
        { id: 'message-one', role: 'user', content: 'Hi' },
        { id: 'note-one', role: 'note', content: 'Internal reminder' },
        { id: 'unknown-one', role: 'debug', content: 'Internal debug data' },
        { id: 'message-two', role: 'assistant', content: 'Hello' },
      ],
      {
        items: [
          messageItem({ id: 'message-one', role: 'user', content: 'Hi' }),
          assistantRun('one', [outputItem('Hello')]),
        ],
      },
    );

    expect(sessionState.messages.map((message) => message.role)).toEqual([
      'user',
      'assistant',
    ]);
    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
      expect.objectContaining({
        id: 'message-one',
        type: 'message',
        message: expect.objectContaining({ content: 'Hi' }),
      }),
      expect.objectContaining({
        type: 'assistant_run',
        outputs: [expect.objectContaining({ content: 'Hello' })],
      }),
    ]);
    expect(
      JSON.stringify(visibleTimelineItemsForRender(sessionState)),
    ).not.toContain('Internal reminder');
  });

  it('keeps durable Background Bash statuses separate from visible history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(
      sessionState,
      [
        { id: 'message-one', role: 'user', content: 'Hi' },
        { id: 'message-two', role: 'assistant', content: 'Hello' },
      ],
      {
        backgroundCommandStatuses: {
          cmd_running: 'running',
          cmd_finished: 'completed',
          cmd_lingering: 'completed',
        },
        backgroundCommandsStillRunning: ['cmd_lingering'],
      },
    );

    expect(sessionState.backgroundCommandStatuses).toEqual({
      cmd_running: { status: 'running', stillRunning: false },
      cmd_finished: { status: 'completed', stillRunning: false },
      cmd_lingering: { status: 'completed', stillRunning: true },
    });
    expect(sessionState.messages).toHaveLength(2);
  });

  it('retains older pages and distinct occurrences of the same checkpoint on an incremental read', async () => {
    const checkpoint = {
      id: 'same-checkpoint',
      role: 'compaction_checkpoint',
      content: 'Summary',
    };
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [{ ...checkpoint, history_sequence: 2 }],
        items: [
          {
            id: 'checkpoint-occurrence-2',
            type: 'compaction_separator',
            message: checkpoint,
          },
        ],
        history_generation: 'g',
        next_after: 'cursor-3',
        has_more: true,
        next_before: 'before-2',
      })
      .mockResolvedValueOnce({
        messages: [
          saved(0, null, 'user', 'Earlier'),
          { ...checkpoint, history_sequence: 1 },
        ],
        items: [
          messageItem(saved(0, null, 'user', 'Earlier')),
          {
            id: 'checkpoint-occurrence-1',
            type: 'compaction_separator',
            message: checkpoint,
          },
        ],
      })
      .mockResolvedValueOnce({
        messages: [saved(3, 'new', 'user', 'New')],
        items: [messageItem(saved(3, 'new', 'user', 'New'))],
        history_generation: 'g',
        incremental: true,
        next_after: 'cursor-4',
      });
    const { chatState, controller } = setupController({
      operationOverrides: { loadChatHistory },
    });
    await controller.loadHistoryForSession('agent@project', 'session');
    const session = ensureSessionState(chatState, 'agent@project', 'session');
    expect(await controller.loadOlderHistory(session)).toBe(true);
    await controller.loadHistoryForSession('agent@project', 'session');
    expect(loadChatHistory.mock.calls[2][0]).toMatchObject({
      after: 'cursor-3',
    });
    expect(session.messages.map((message) => message.history_sequence)).toEqual(
      [0, 1, 2, 3],
    );
    const ids = visibleTimelineItemsForRender(session).map((item) => item.id);
    expect(ids).toEqual([
      'message-0',
      'checkpoint-occurrence-1',
      'checkpoint-occurrence-2',
      'message-3',
    ]);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
