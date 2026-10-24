import { vi } from 'vitest';
import { applyTimelineEvent } from '../chatTimeline.js';
import {
  assistantRun,
  outputItem,
} from '../../components/__tests__/timelineItems.support.js';
import { createChatController, createChatState } from '../chatState.js';

// Chat controller over fake operations and a fake Run stream.
export function setupController({
  operationOverrides = {},
  runEvents = null,
  isDisplayedSession = () => false,
  shouldLoadCurrentHistory = () => true,
  onSessionCreated = () => {},
  submissionScope = '',
} = {}) {
  const chatState = createChatState();
  const runStream = {
    applyConnectionSnapshot: vi.fn(),
    attachRunStream: vi.fn(),
    closeSubscriptionFor: vi.fn(),
    closeSubscriptions: vi.fn(),
    closeSubscriptionsExcept: vi.fn(),
    handleServerEvent: vi.fn(),
    mergeRunResponse: vi.fn(),
    subscribeToRun: vi.fn(),
  };
  const listQueue = vi.fn().mockResolvedValue({
    items: [{ id: 'queued-one', content: 'Next', editable: true }],
  });
  const onRestartQueueDiscarded = vi.fn();
  const onAgentsChanged = vi.fn();
  const onAgentSelected = vi.fn();
  const controller = createChatController({
    chatState,
    runStream,
    runEvents,
    operations: {
      listQueue,
      loadReflectionRuns: vi.fn().mockResolvedValue({ reflection_runs: [] }),
      ...operationOverrides,
    },
    isDisplayedSession,
    shouldLoadCurrentHistory,
    onRestartQueueDiscarded,
    onAgentsChanged,
    onAgentSelected,
    onSessionCreated,
    submissionScope,
  });
  return {
    chatState,
    controller,
    listQueue,
    onRestartQueueDiscarded,
    onAgentsChanged,
    onAgentSelected,
    runStream,
  };
}

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

// A WebSocket Run server event as App's feed holds it.
export function runServerEvent(type, payload) {
  return {
    type,
    payload: {
      project_id: null,
      run_event_type: type,
      run_event_sequence: 1,
      ...payload,
    },
  };
}

// A Reflection Run row as History and reflection reads report it.
export function reflectionRun(status = 'completed', runId = 'review-one') {
  return {
    run_id: runId,
    session_id: 'review-session',
    run_kind: 'memory_reflection',
    status,
    started_at: '2026-09-05T10:00:00Z',
  };
}

export function countTimelineTextOccurrences(timelineItems, text) {
  let count = 0;
  for (const item of timelineItems) {
    if (item.type === 'message' && item.message?.content === text) {
      count += 1;
      continue;
    }
    if (item.type === 'assistant_run') {
      count += (item.outputs ?? []).filter(
        (output) => output.content === text,
      ).length;
    }
  }
  return count;
}

// Explicit server snapshot for controller tests that receive visible output
// while an RPC is in flight. No raw-event reduction is involved.
export function applyOutputSnapshot(session, runId, sequence, content) {
  return applyTimelineEvent(session, 'timeline_snapshot', {
    run_id: runId,
    sequence,
    items: [assistantRun(runId, [outputItem(content)])],
  });
}
