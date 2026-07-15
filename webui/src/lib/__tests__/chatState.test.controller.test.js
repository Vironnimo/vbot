import { describe, expect, it, vi } from 'vitest';

import {
  createChatController,
  createChatState,
  ensureSessionState,
} from '../chatState.js';

function setup() {
  const chatState = createChatState();
  const runStream = {
    applyConnectionSnapshot: vi.fn(),
    closeSubscriptions: vi.fn(),
    handleServerEvents: vi.fn(),
  };
  const listQueue = vi.fn().mockResolvedValue({
    items: [{ id: 'queued-one', content: 'Next' }],
  });
  const onQueueSyncError = vi.fn();
  const controller = createChatController({
    chatState,
    runStream,
    listQueue,
    onQueueSyncError,
  });
  return {
    chatState,
    controller,
    listQueue,
    onQueueSyncError,
    runStream,
  };
}

describe('chat controller', () => {
  it('applies each connection snapshot object only once', () => {
    const { controller, runStream } = setup();
    const snapshot = { active_runs: [] };

    expect(controller.applyConnectionSnapshot(snapshot)).toBe(true);
    expect(controller.applyConnectionSnapshot(snapshot)).toBe(false);
    expect(runStream.applyConnectionSnapshot).toHaveBeenCalledOnce();
  });

  it('re-syncs only held sessions matching a scoped Queue invalidation', async () => {
    const { chatState, controller, listQueue } = setup();
    const target = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-one',
    );
    ensureSessionState(chatState, 'reviewer@project-one', 'session-two');
    const scope = { agentId: 'builder', sessionId: 'session-one' };

    expect(controller.applyQueueInvalidation(scope)).toBe(true);
    expect(controller.applyQueueInvalidation(scope)).toBe(false);
    await vi.waitFor(() => expect(listQueue).toHaveBeenCalledOnce());

    expect(listQueue).toHaveBeenCalledWith(
      'builder@project-one',
      'session-one',
    );
    expect(target.queue).toEqual([
      { id: 'queued-one', content: 'Next', created_at: null },
    ]);
  });

  it('reports Queue sync failures and closes subscriptions on destroy', async () => {
    const { chatState, controller, listQueue, onQueueSyncError, runStream } =
      setup();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    const error = new Error('offline');
    listQueue.mockRejectedValueOnce(error);

    await controller.syncSessionQueue(sessionState);
    controller.destroy();

    expect(onQueueSyncError).toHaveBeenCalledWith(error);
    expect(runStream.closeSubscriptions).toHaveBeenCalledOnce();
  });
});
