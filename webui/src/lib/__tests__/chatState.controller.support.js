import { vi } from 'vitest';
import { createChatController, createChatState } from '../chatState.js';

function setup({
  operationOverrides = {},
  isDisplayedSession = () => false,
  shouldLoadCurrentHistory = () => true,
} = {}) {
  const chatState = createChatState();
  const runStream = {
    applyConnectionSnapshot: vi.fn(),
    attachRunStream: vi.fn(),
    closeSubscriptionFor: vi.fn(),
    closeSubscriptions: vi.fn(),
    closeSubscriptionsExcept: vi.fn(),
    handleServerEvents: vi.fn(),
    mergeRunResponse: vi.fn(),
    subscribeToRun: vi.fn(),
  };
  const listQueue = vi.fn().mockResolvedValue({
    items: [{ id: 'queued-one', content: 'Next', editable: true }],
  });
  const onRestartQueueDiscarded = vi.fn();
  const controller = createChatController({
    chatState,
    runStream,
    operations: {
      listQueue,
      loadReflectionRuns: vi.fn().mockResolvedValue({ reflection_runs: [] }),
      ...operationOverrides,
    },
    translate: (_key, fallback) => fallback,
    isDisplayedSession,
    shouldLoadCurrentHistory,
    onRestartQueueDiscarded,
  });
  return {
    chatState,
    controller,
    listQueue,
    onRestartQueueDiscarded,
    runStream,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const review = (status = 'completed', runId = 'review-one') => ({
  run_id: runId,
  session_id: 'review-session',
  run_kind: 'memory_reflection',
  status,
  started_at: '2026-09-05T10:00:00Z',
});

export { setup, deferred, review };
