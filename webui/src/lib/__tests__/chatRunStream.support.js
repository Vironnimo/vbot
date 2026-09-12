import { vi } from 'vitest';
import { createChatRunStream } from '../chatRunStream.js';

function makeStreamHarness({
  chatState,
  displayedAgentId,
  displayedSessionId,
  subscribeRunEvents,
  reconcileRunSession = vi.fn(async () => true),
  reportStreamDiagnostic = vi.fn(),
} = {}) {
  const subAgentRunStatuses = {};
  const isDisplayedSession = vi.fn(
    (agentId, sessionId) =>
      agentId === displayedAgentId && sessionId === displayedSessionId,
  );
  const syncSessionQueue = vi.fn(async () => {});

  const stream = createChatRunStream({
    chatState,
    subscribeRunEvents:
      subscribeRunEvents ??
      vi.fn(() => ({
        close: vi.fn(),
      })),
    syncSessionQueue,
    reconcileRunSession,
    reportStreamDiagnostic,
    isDisplayedSession,
    updateSubAgentRunStatuses: (updates, { replaceActive = false } = {}) => {
      if (replaceActive) {
        for (const [key, value] of Object.entries(subAgentRunStatuses)) {
          if (
            (key.startsWith('run:') || key.startsWith('session:')) &&
            (value === 'running' || value === 'queued')
          ) {
            delete subAgentRunStatuses[key];
          }
        }
      }
      Object.assign(subAgentRunStatuses, updates);
    },
  });

  return {
    stream,
    subAgentRunStatuses,
    isDisplayedSession,
    reconcileRunSession,
    reportStreamDiagnostic,
    syncSessionQueue,
  };
}

export { makeStreamHarness };
