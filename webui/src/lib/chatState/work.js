import { createCoalescedRefresh } from '../sessionInvalidation.js';

// Internal to the Chat controller. The server owns which work belongs to the
// complete Session subtree; loaded Timeline rows are only a presentation.
export function createChatWork({ operations }) {
  let selected = null;

  function release() {
    if (!selected) return;
    clearTimeout(selected.retryTimer);
    selected.refresh.destroy();
    selected = null;
  }

  function invalidate(entry) {
    entry.version += 1;
    entry.retryAttempt = 0;
    clearTimeout(entry.retryTimer);
  }

  function select(sessionState) {
    if (!sessionState?.agentId || !sessionState?.sessionId) {
      release();
      return;
    }
    if (selected?.key === sessionState.key) {
      invalidate(selected);
      selected.refresh.schedule();
      return;
    }
    release();
    const entry = {
      key: sessionState.key,
      sessionState,
      version: 0,
      retryAttempt: 0,
      retryTimer: null,
      refresh: createCoalescedRefresh(read),
    };
    selected = entry;
    void entry.refresh.run();

    async function read() {
      const version = entry.version;
      const current = () => selected === entry && version === entry.version;
      try {
        const work = await operations.getSessionWork(
          sessionState.agentId,
          sessionState.sessionId,
        );
        if (!current()) return;
        if (typeof work?.background_work_running !== 'boolean') return;
        sessionState.workProjection = work;
        entry.retryAttempt = 0;
      } catch {
        // Keep the last accepted projection on an unavailable read, and retry
        // even if idle work produces no further lifecycle event to wake us.
        if (!current()) return;
        const delay = Math.min(10000, 1000 * 2 ** entry.retryAttempt);
        entry.retryAttempt += 1;
        entry.retryTimer = setTimeout(() => {
          entry.retryTimer = null;
          if (current()) entry.refresh.schedule();
        }, delay);
      }
    }
  }

  return {
    sync: select,
    refresh(sessionState) {
      if (!selected || selected.key !== sessionState?.key) return;
      invalidate(selected);
      void selected.refresh.run();
    },
    destroy: release,
  };
}
